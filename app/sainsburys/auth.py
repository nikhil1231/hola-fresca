"""Signing in to Sainsbury's, over HTTP and without a browser.

Ocado's ladder drives a real Chrome because its SSO runs reCAPTCHA on the
password form and no HTTP client can clear that. Sainsbury's does not: its
identity service is an ordinary OAuth2/OIDC provider, the password form is a
plain ``POST`` of form fields, and the reCAPTCHA on that domain guards the
*forgotten password* flow rather than the login. So the whole ladder here is
:mod:`curl_cffi` requests on the same browser-fingerprinted transport the
catalogue scrape uses — no display, no profile directory, about a second.

The flow, which is standard once you know which of the two front-ends you are
talking to (see :mod:`app.scraper.products.sainsburys` for that story):

1. ``GET account.sainsburys.co.uk/oauth2/auth`` with PKCE. The provider parks
   the request and redirects to its own login UI carrying a ``login_challenge``.
2. ``POST /gol/login`` with the challenge, the email and the password.
3. The provider redirects to ``/gol/login/mfa``. **Nothing has been sent yet**
   — a separate ``POST /gol/login/send-mfa`` is what dispatches the six-digit
   code, and the site makes that call from the page's JavaScript rather than
   the server. Then ``POST`` the code back to the MFA path.
4. The provider redirects back through ``/oauth2/auth`` to the app's redirect
   URI carrying an authorization ``code``.
5. ``POST /oauth2/token`` exchanges that for an access token, an id token and —
   because the scope asks for ``offline`` — a **refresh token**.
6. ``POST`` the access token to the groceries API's ``login-access-token``,
   which is what turns an identity into a shopping session with a basket.

Step 5 is the one that makes this pleasant. Ocado has no refresh token, so every
expiry is a fresh login and another emailed code, which is why it needs a
mailbox to read codes from and a heartbeat to keep sessions warm. Here the code
is asked for once; from then on step 5 runs against the stored refresh token and
steps 1-4 never happen again. Reading the code out of a mailbox
(:mod:`app.ocado.otp_mail`, which is retailer-agnostic despite where it lives)
is therefore a convenience for the first login rather than a load-bearing part
of the design — and if no mailbox is configured, the ladder parks at
:attr:`AuthState.AWAITING_OTP` and waits to be handed the code by hand.

A browser that has signed in before is not challenged at all, which is worth
knowing when comparing this against signing in by hand: the challenge is for
the device, not the account, and a fresh HTTP client is always a new device.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

log = logging.getLogger("holafresca.sainsburys")

IDENTITY_URL = "https://account.sainsburys.co.uk"
GOL_URL = "https://www.sainsburys.co.uk"

#: The OAuth client the groceries site itself uses. ``missionId`` is Sainsbury's
#: word for which brand's login UI to show (``gol`` is groceries; Nectar and
#: Argos are the other tenants of the same provider), and it is what decides the
#: path the login form posts to.
MISSION_ID = "gol"
CLIENT_ID = "gol"
AUDIENCE = "gol.sainsburys.co.uk"
REDIRECT_URI = f"{GOL_URL}/gol-ui/oauth/redirect"
#: ``offline`` is the important one: it is what makes the provider issue a
#: refresh token, and so what keeps the emailed code a one-time cost.
SCOPE = "openid offline gol-session"

AUTHORIZE_PATH = "/oauth2/auth"
TOKEN_PATH = "/oauth2/token"
LOGIN_PATH = f"/{MISSION_ID}/login"
MFA_PATH = f"/{MISSION_ID}/login/mfa"
#: Asks Sainsbury's to actually send the code. See :func:`request_code` — the
#: redirect to the MFA page does not send one.
SEND_MFA_PATH = f"/{MISSION_ID}/login/send-mfa"

#: Where an identity is traded for a shopping session. The API also exposes
#: ``/v1/login-start``, which the site's own client defines and never calls; it
#: is left alone here rather than sent on the guess that it does something.
LOGIN_API = "/groceries-api/gol-services/login"
LOGIN_ACCESS_TOKEN_PATH = f"{LOGIN_API}/v1/login-access-token"

#: Cap on redirect chasing. The real chain is four hops; anything much longer is
#: a loop, and following it forever would hammer the provider.
MAX_REDIRECTS = 10

#: Refresh this long before the access token actually expires, so a request is
#: never issued with a token that dies in flight.
EXPIRY_MARGIN_S = 120.0

REQUEST_TIMEOUT_S = 30.0


class AuthState(StrEnum):
    LOGGED_OUT = "logged_out"
    AWAITING_OTP = "awaiting_otp"
    READY = "ready"


class AuthError(RuntimeError):
    """A login that failed for a reason worth telling the caller about."""


@dataclass(frozen=True, slots=True)
class Tokens:
    """What the token endpoint hands back, plus when it stops being true."""

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        """Whether the access token is spent, counting the safety margin.

        A token with no stated expiry is treated as live: the provider always
        states one, so the only way to get here is a stored session written by
        an older build, and guessing "expired" would throw away a working
        refresh token for nothing.
        """
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(seconds=EXPIRY_MARGIN_S)

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "Tokens | None":
        if not isinstance(payload, dict):
            return None
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        raw_expiry = payload.get("expires_at")
        expires_at = None
        if isinstance(raw_expiry, str) and raw_expiry:
            try:
                expires_at = datetime.fromisoformat(raw_expiry)
            except ValueError:
                expires_at = None
        return cls(
            access_token=access,
            refresh_token=payload.get("refresh_token") or None,
            id_token=payload.get("id_token") or None,
            expires_at=expires_at,
        )


@dataclass(slots=True)
class PendingLogin:
    """A login parked between the password and the code.

    The PKCE verifier and the provider's challenge have to survive from the
    credential POST to the OTP submit, which in the API's case are two separate
    HTTP requests minutes apart. Losing it means starting the ladder again, and
    Sainsbury's emailing a second code.
    """

    login_challenge: str
    code_verifier: str
    state: str
    started_at: float = field(default_factory=time.monotonic)


def _pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge, both base64url without padding."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _query(url: str, key: str) -> str | None:
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


def _follow(http: Any, url: str, *, stop_on: str = "code") -> tuple[str, str | None]:
    """Chase redirects until the chain ends or a query parameter appears.

    Stops *before* fetching the URL carrying ``stop_on``, which matters: that URL
    is the SPA's redirect page, and loading it would spend a request rendering
    HTML we have no use for — and, worse, hand the one-time authorization code to
    a page that would try to redeem it first.
    """
    current = url
    for _ in range(MAX_REDIRECTS):
        found = _query(current, stop_on)
        if found:
            return current, found
        response = http.get(current, allow_redirects=False, timeout=REQUEST_TIMEOUT_S)
        location = response.headers.get("location")
        if not location:
            return current, None
        current = urljoin(current, location)
    raise AuthError(f"Sainsbury's redirected more than {MAX_REDIRECTS} times during login")


def begin_login(http: Any) -> PendingLogin:
    """Ask the provider to start an authorization, and take its challenge.

    Nothing here is account-specific — no credential is sent — so a failure at
    this step is the provider or the network, never a bad password.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "missionId": MISSION_ID,
        "audience": AUDIENCE,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "nonce": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{IDENTITY_URL}{AUTHORIZE_PATH}?{urlencode(params)}"
    final, _ = _follow(http, url, stop_on="login_challenge")
    challenge_id = _query(final, "login_challenge")
    if not challenge_id:
        raise AuthError("Sainsbury's did not offer a login challenge")
    return PendingLogin(login_challenge=challenge_id, code_verifier=verifier, state=state)


def submit_credentials(http: Any, pending: PendingLogin, email: str, password: str) -> str | None:
    """Post the password. Returns an authorization code, or ``None`` for "code sent".

    The two outcomes are told apart by where the provider sends us next, because
    it answers both with a 302. A redirect to the MFA path means the password was
    right and a code is in the post; a redirect back to the login form means it
    was not.
    """
    response = http.post(
        f"{IDENTITY_URL}{LOGIN_PATH}",
        data={
            "login_challenge": pending.login_challenge,
            "username": email,
            "password": password,
            # The site sends both. ``web_authn_device`` says this browser has no
            # passkey to offer; ``is_remember_me`` is the "stay logged in" tick,
            # which is what makes the provider's own session outlive the day and
            # so keeps re-authentication down to a token refresh.
            "web_authn_device": "0",
            "is_remember_me": "1",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{IDENTITY_URL}{LOGIN_PATH}?login_challenge={pending.login_challenge}",
        },
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT_S,
    )
    location = response.headers.get("location")
    if response.status_code != 302 or not location:
        # A 200 is the login form rendered again, which it only does to show an
        # error. There is no machine-readable reason on it worth parsing: the
        # useful distinction is "we are not signed in", which the caller has.
        raise AuthError("Sainsbury's rejected the email or password")

    target = urljoin(f"{IDENTITY_URL}{LOGIN_PATH}", location)
    if MFA_PATH in urlparse(target).path:
        return None

    _, code = _follow(http, target)
    if not code:
        raise AuthError("Sainsbury's accepted the password but issued no authorization code")
    return code


def request_code(http: Any) -> None:
    """Ask Sainsbury's to send the one-time code.

    This is easy to miss and fails silently when missed. Being redirected to the
    MFA page does **not** send anything: the login UI fires this from a
    ``useEffect`` when the code form mounts, guarded by a ``sessionStorage``
    flag. A client that does not run their JavaScript therefore lands on a page
    that looks exactly right, and then waits forever for a code nobody asked
    for.

    The GET is what the browser does first and is kept for the same reason the
    ``Referer`` is: it costs one request and makes this look like the flow it
    actually is.
    """
    http.get(f"{IDENTITY_URL}{MFA_PATH}", timeout=REQUEST_TIMEOUT_S)
    response = http.post(
        f"{IDENTITY_URL}{SEND_MFA_PATH}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{IDENTITY_URL}{MFA_PATH}",
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise AuthError(
            f"Sainsbury's would not send a login code ({response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    # Answers {"success": true}. A false here means the code was not sent, and
    # waiting on it would be the very failure this function exists to prevent.
    if isinstance(payload, dict) and payload.get("success") is False:
        raise AuthError("Sainsbury's declined to send a login code")


def submit_code(http: Any, pending: PendingLogin, code: str) -> str:
    """Post the one-time code and come back with an authorization code."""
    response = http.post(
        f"{IDENTITY_URL}{MFA_PATH}",
        data={"code": code.strip()},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{IDENTITY_URL}{MFA_PATH}",
        },
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT_S,
    )
    location = response.headers.get("location")
    if response.status_code != 302 or not location:
        raise AuthError("Sainsbury's would not accept that code")

    target = urljoin(f"{IDENTITY_URL}{MFA_PATH}", location)
    if MFA_PATH in urlparse(target).path:
        # Back to the code form: wrong, expired, or one attempt too many.
        raise AuthError("Sainsbury's would not accept that code")

    _, authorization = _follow(http, target)
    if not authorization:
        raise AuthError("Sainsbury's accepted the code but issued no authorization code")
    return authorization


def exchange_code(http: Any, authorization_code: str, verifier: str) -> Tokens:
    return _tokens_from(
        http.post(
            f"{IDENTITY_URL}{TOKEN_PATH}",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": authorization_code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            timeout=REQUEST_TIMEOUT_S,
        ),
        what="authorization code",
    )


def refresh_tokens(http: Any, refresh_token: str) -> Tokens:
    """Trade a refresh token for a live access token.

    The provider rotates the refresh token on every use, so the answer replaces
    the stored pair wholesale rather than updating the access token in place —
    keeping the old refresh token would leave a session that dies at the next
    refresh with no way back but another emailed code.
    """
    return _tokens_from(
        http.post(
            f"{IDENTITY_URL}{TOKEN_PATH}",
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
                "scope": SCOPE,
            },
            timeout=REQUEST_TIMEOUT_S,
        ),
        what="refresh token",
    )


def _tokens_from(response: Any, *, what: str) -> Tokens:
    if response.status_code != 200:
        raise AuthError(f"Sainsbury's refused the {what} ({response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(f"Sainsbury's answered the {what} with non-JSON") from exc
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError(f"Sainsbury's returned no access token for the {what}")
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    return Tokens(
        access_token=access,
        refresh_token=payload.get("refresh_token") or None,
        id_token=payload.get("id_token") or None,
        expires_at=expires_at,
    )


def establish_gol_session(http: Any, tokens: Tokens) -> None:
    """Turn an identity into a shopping session.

    An access token alone buys nothing from the groceries API: the basket
    endpoints are cookie-authenticated, and this is the call that mints the
    cookie. It is also the step that has to be repeated after every token
    refresh, which is why it lives here rather than in the login path.

    ``food_profile_create`` is sent because the site sends it — it asks the API
    not to start building a dietary profile off the back of the sign-in, and
    false is what a returning shopper's own client says.

    A 200 is not enough on its own. The answer carries ``wc_token``, the
    commerce session the basket is really keyed on, and an *empty* one is how
    this endpoint reports a token it would not trade — the site's own client
    checks exactly that and treats the blank as a failed login.
    """
    response = http.post(
        f"{GOL_URL}{LOGIN_ACCESS_TOKEN_PATH}",
        json={"access_token": tokens.access_token, "food_profile_create": False},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{GOL_URL}/gol-ui/groceries",
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if response.status_code >= 400:
        raise AuthError(
            f"Sainsbury's would not open a shopping session ({response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and not payload.get("wc_token"):
        raise AuthError("Sainsbury's accepted the token but issued no shopping session")


def otp_query() -> Any:
    """What a Sainsbury's code email looks like in the shared mailbox.

    Reuses Ocado's mailbox reader, which is retailer-agnostic in everything but
    the module it lives in: the sender substring and the account markers are the
    only parts that were ever Ocado-specific.
    """
    from app import config
    from app.ocado.otp_mail import OtpQuery, plus_address

    markers: list[str] = []
    if config.SAINSBURYS_EMAIL:
        markers.append(config.SAINSBURYS_EMAIL)
    if config.SAINSBURYS_OTP_IMAP_USER:
        markers.append(plus_address(config.SAINSBURYS_OTP_IMAP_USER, "sainsburys"))
    return OtpQuery(markers=tuple(markers), sender_contains="sainsbury")


def read_emailed_code(*, since: float) -> str | None:
    """The code, if a mailbox is configured and one turns up in time.

    ``None`` covers both "no mailbox configured" and "nothing arrived", because
    the caller does the same thing either way: park and ask for it by hand.
    """
    from app import config
    from app.ocado.otp_mail import MailboxConfig, fetch_code

    if not (config.SAINSBURYS_OTP_IMAP_USER and config.SAINSBURYS_OTP_IMAP_PASSWORD):
        return None
    mailbox = MailboxConfig(
        host=config.SAINSBURYS_OTP_IMAP_HOST,
        port=config.SAINSBURYS_OTP_IMAP_PORT,
        user=config.SAINSBURYS_OTP_IMAP_USER,
        password=config.SAINSBURYS_OTP_IMAP_PASSWORD,
        folder=config.SAINSBURYS_OTP_IMAP_FOLDER,
    )
    try:
        return fetch_code(
            mailbox,
            otp_query(),
            since=since,
            wait_s=config.SAINSBURYS_OTP_WAIT_S,
            poll_s=config.SAINSBURYS_OTP_POLL_S,
        )
    except Exception:  # noqa: BLE001 - a mailbox that will not open is not a login failure
        log.warning("Sainsbury's OTP mailbox could not be read; asking for the code", exc_info=True)
        return None
