"""The persisted Sainsbury's shopping session, and the ladder that keeps it live.

Two things are stored, and they expire on very different clocks. The **cookie
jar** is the shopping session the basket endpoints actually authenticate
against, and it is short-lived. The **refresh token** is the long-lived half:
while it survives, a dead cookie jar is one round trip away from being a live one
and nobody has to read an email.

So :meth:`SainsburysSession.ensure_authenticated` is a ladder, cheapest first:

1. the jar as it stands, judged locally and for free — a signed-in commerce
   cookie plus an access token that has not expired. This is a guess, not a
   verdict, and it is allowed to be: the only way to be *sure* is to make the
   request the caller wanted, so a wrong guess costs one 401 and drops to the
   next rung (see :meth:`SainsburysSession.request`);
2. the access token we already hold, which mints a fresh shopping session on
   its own. The cookies die well inside the token's hour, so this is the
   common repair and it spends nothing;
3. the refresh token, for when the access token has expired too;
4. a bare authorization request, which the provider answers with a code outright
   if it still has a session for us — no credential sent, nobody emailed;
5. a full login, the only rung that can need a one-time code.

Rung 3 is deliberately below rung 2 rather than beside it, because refresh
tokens **rotate**: every use invalidates the one on disk and issues another, so
a needless refresh is a credential to re-persist and a window in which a crash
leaves nothing usable. Rung 2 avoids the whole question.

Rung 4 is, as of August 2026, **inert against the live provider**, and that is
worth stating plainly rather than leaving as an optimistic comment. It was added
on the theory that a browser skips the emailed code because its provider session
answers rung 4 outright, and that this ladder was asking for codes only because
it never took that rung. The first half looks right; the second turned out not to
help, because Sainsbury's does not issue *this* client a session cookie at all.
Measured after a real, successful, code-and-all login: the jar came back holding
``oauth2_authentication_csrf`` and ``oauth2_consent_csrf`` and nothing else, both
of which an anonymous caller is also given on the way to the login form. So rung
4 is skipped before it costs a request — see
:meth:`SainsburysSession.has_provider_session` — and it is kept because it is the
correct mechanism, cheap while it cannot fire, and would start working on its own
if that ever changed.

What actually keeps the code a one-off is therefore **rung 3**: the refresh token
survives where a provider session was never given, and rungs 2-3 are what carry
a signed-in account between logins. If somebody is being asked for a code
repeatedly, the refresh token is what to look at.

Ocado's equivalent has none of the middle rungs, which is the whole reason it
needs a mailbox and a heartbeat.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

from app import config
from app.sainsburys.auth import (
    GOL_URL,
    AuthError,
    AuthState,
    PendingLogin,
    Tokens,
    authorize,
    establish_gol_session,
    exchange_code,
    read_emailed_code,
    refresh_tokens,
    request_code,
    submit_code,
    submit_credentials,
)

log = logging.getLogger("holafresca.sainsburys")

#: WebSphere Commerce names this cookie after the signed-in customer, so its
#: *presence* is the session. An anonymous caller is given no ``WC_`` cookies at
#: all — not even a guest one — which makes this the one signal that separates
#: the two without asking the network anything.
#:
#: There is deliberately no HTTP probe. The obvious candidates all lie: the
#: basket answers an anonymous caller 200 with an empty ``basket_id``, and
#: ``/product/v1/favourites`` answers 401 whether or not you are signed in. The
#: honest authority is the API call the caller actually wanted, which is why
#: :meth:`SainsburysSession.request` re-authenticates on a 401 rather than
#: trying to predict one.
AUTH_COOKIE_PREFIX = "WC_AUTHENTICATION_"

#: Identity cookies that say nothing about being signed in. The provider hands
#: these to anonymous callers as part of walking them to the login form, so they
#: are present on a jar that has never authenticated and must not be read as a
#: session. See :meth:`SainsburysSession.has_provider_session`.
PROVIDER_FLOW_COOKIES = frozenset(
    {"oauth2_authentication_csrf", "oauth2_consent_csrf"}
)

#: Which browser handshake to present. Sainsbury's edge refuses Python's TLS on
#: every host it fronts, identity included, so the same impersonation the
#: catalogue scrape relies on is needed here — see
#: :mod:`app.scraper.products.http_session` for why this is a handshake question
#: and not a cookie one.
IMPERSONATE = "chrome"

REQUEST_TIMEOUT_S = 30.0


class SainsburysSession:
    """One account's cookies and tokens, with the ladder that refreshes them."""

    def __init__(
        self,
        *,
        jar_path: Path | None = None,
        http: Any | None = None,
    ):
        self.jar_path = jar_path or (config.DATA_DIR / "sainsburys" / "session.json")
        self.tokens: Tokens | None = None
        self.state: AuthState = AuthState.LOGGED_OUT
        #: A login parked between the password and the code. Held in memory only:
        #: it is worthless after a restart anyway, since the code it is waiting
        #: for expires in ten minutes.
        self.pending: PendingLogin | None = None
        self._lock = threading.RLock()
        self._http = http or self._new_http()
        self.load()

    @staticmethod
    def _new_http() -> Any:
        try:
            from curl_cffi import requests
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError("curl_cffi is required to talk to Sainsbury's") from exc
        return requests.Session(impersonate=IMPERSONATE)

    @property
    def http(self) -> Any:
        return self._http

    def close(self) -> None:
        self._http.close()

    # --- persistence ---------------------------------------------------------

    def load(self) -> None:
        if not self.jar_path.exists():
            return
        try:
            payload = json.loads(self.jar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Sainsbury's session file could not be read; starting logged out")
            return
        for item in payload.get("cookies", []):
            try:
                self._http.cookies.jar.set_cookie(_cookie_from_json(item))
            except (KeyError, TypeError):
                continue
        self.tokens = Tokens.from_json(payload.get("tokens"))
        if self.looks_authenticated():
            self.state = AuthState.READY

    def save(self) -> None:
        self.jar_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tokens": self.tokens.to_json() if self.tokens else None,
            "cookies": [_cookie_to_json(cookie) for cookie in self._http.cookies.jar],
        }
        # Written by hand rather than through a helper because the file holds a
        # refresh token: it is a credential, and it should not be world-readable
        # in a directory that also holds the exported catalogue.
        self.jar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            self.jar_path.chmod(0o600)
        except OSError:  # pragma: no cover - filesystems that will not say
            pass

    def forget(self) -> None:
        """Drop everything and go back to logged out. For starting over."""
        with self._lock:
            self._http.cookies.jar.clear()
            self.tokens = None
            self.pending = None
            self.state = AuthState.LOGGED_OUT
            self.save()

    # --- the ladder ----------------------------------------------------------

    def has_auth_cookie(self) -> bool:
        """Whether the jar carries a signed-in commerce session."""
        return self.wc_auth_token() is not None

    def wc_auth_token(self) -> str | None:
        """The commerce session token, taken from the cookie that carries it.

        Sent back as the ``WCAuthToken`` header on every request, which is what
        the site's own client does. Sending the cookie alone is not enough:
        reads are answered either way, but **writes are refused 401** — so
        missing this looks like a working integration right up until the moment
        it tries to put something in the trolley.

        The ``-1002`` suffix is a second store's cookie and is only a fallback;
        a value of ``DEL`` is how WebSphere tombstones a logged-out session, so
        it counts as absent rather than as a token.
        """
        preferred: str | None = None
        fallback: str | None = None
        for cookie in self._http.cookies.jar:
            if not cookie.name.startswith(AUTH_COOKIE_PREFIX):
                continue
            if not cookie.value or cookie.value == "DEL":
                continue
            if cookie.name.endswith("-1002"):
                fallback = cookie.value
            else:
                preferred = cookie.value
        return preferred or fallback

    def _auth_headers(self) -> dict[str, str]:
        """What the site attaches to every call: the commerce token and a bearer."""
        headers: dict[str, str] = {}
        token = self.wc_auth_token()
        if token:
            headers["WCAuthToken"] = token
        if self.tokens is not None and self.tokens.access_token:
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        return headers

    def has_provider_session(self) -> bool:
        """Whether the identity provider might still recognise this client.

        Cheap and local. Any identity cookie counts *except* the CSRF pair, and
        that exception is the whole point: those two are per-flow scaffolding
        that the provider hands out to anonymous callers on the way to the login
        form, so counting them would mean "we have a session" was true of a
        client that had never signed in — and the rung would pay for a redirect
        chase on every page load to be told so.

        Measured against the live provider, a jar this app has *successfully*
        logged in with holds exactly those two and nothing else — see
        :meth:`_log_provider_session`. So today this is reliably false and rung
        four never runs. It is kept because it is the correct mechanism and the
        one a browser uses; if Sainsbury's ever starts issuing this client a
        session cookie, the quiet re-login starts working on its own.
        """
        return any(
            "account.sainsburys" in (cookie.domain or "")
            and cookie.name not in PROVIDER_FLOW_COOKIES
            for cookie in self._http.cookies.jar
        )

    def looks_authenticated(self) -> bool:
        """Whether this session is worth trying, judged without a request.

        Both halves are needed and neither is proof. The cookie says a shopping
        session was opened; the token expiry says we could open another without
        anyone's help. Being wrong here is cheap in the direction it fails —
        a stale cookie costs one 401 and a refresh, which :meth:`request`
        already handles.
        """
        if not self.has_auth_cookie():
            return False
        return self.tokens is not None and not self.tokens.expired

    def ensure_authenticated(
        self,
        *,
        trust_existing: bool = True,
        email: str | None = None,
        password: str | None = None,
        allow_mailbox: bool = True,
    ) -> AuthState:
        """Climb until the session shops, and report where it stopped.

        ``trust_existing=False`` skips the probe, for the caller that has just
        *had* a 401 and knows the answer.

        ``allow_mailbox=False`` skips reading the code out of a mailbox and parks
        at :attr:`AuthState.AWAITING_OTP` straight away. That is what an
        interactive caller wants: waiting two minutes for a forwarded copy of an
        email the person can already see is worse than asking them.
        """
        with self._lock:
            if trust_existing and self.looks_authenticated():
                self.state = AuthState.READY
                return self.state

            if self._reestablish():
                return self.state

            if self._refresh():
                return self.state

            if self._reauthorize():
                return self.state

            if not email or not password:
                self.state = AuthState.NEEDS_PASSWORD
                return self.state

            return self._login(email=email, password=password, allow_mailbox=allow_mailbox)

    def refresh_quietly(self) -> AuthState:
        """Climb the first two rungs only, and never the third.

        The rung this stops short of is the one that submits a password, which
        is what makes Sainsbury's email a code. So this is the call that is safe
        to make on page load: it will happily re-mint a session from the stored
        refresh token, and will never land somebody an email because they opened
        a tab.
        """
        with self._lock:
            if (
                self.looks_authenticated()
                or self._reestablish()
                or self._refresh()
                or self._reauthorize()
            ):
                self.state = AuthState.READY
            else:
                self.state = AuthState.NEEDS_PASSWORD
            return self.state

    def _reestablish(self) -> bool:
        """Rung two: re-open the shopping session with the token already held.

        The two halves of a session die on different clocks, and the commerce
        cookies die first — well inside the access token's hour. When that
        happens nothing needs refreshing: the token we are holding will mint a
        new shopping session on its own.

        Worth a rung of its own because the alternative is spending the refresh
        token, and refresh tokens *rotate* — every needless refresh is a new
        token to persist and an old one that dies. Cheaper, and one less way to
        end up logged out.
        """
        if self.tokens is None or self.tokens.expired:
            return False
        try:
            establish_gol_session(self._http, self.tokens)
        except AuthError as exc:
            log.info("Sainsbury's would not re-open a session from the stored token (%s)", exc)
            return False
        self.state = AuthState.READY
        self.save()
        return True

    def _refresh(self) -> bool:
        """Rung three: trade the refresh token for a new session."""
        token = self.tokens.refresh_token if self.tokens else None
        if not token:
            return False
        try:
            fresh = refresh_tokens(self._http, token)
        except AuthError as exc:
            # Rejected when revoked, or when it has already been spent. Not an
            # error to raise: it is exactly the case the login rung exists for.
            log.info("Sainsbury's refresh token no longer works (%s)", exc)
            # Drop only the dead half. The access token is a separate credential
            # with its own expiry, and a rejected refresh token says nothing
            # about it — discarding both here is how a recoverable session turns
            # into a login prompt.
            self.tokens = replace(self.tokens, refresh_token=None)
            self.save()
            return False

        # Persisted before anything else can fail. The provider rotates on use,
        # so the token that was on disk a moment ago is now dead: if this is not
        # written now, a crash between here and the next save leaves the stored
        # session holding a spent token and no way back but a one-time code.
        self.tokens = fresh
        self.save()

        try:
            establish_gol_session(self._http, self.tokens)
        except AuthError as exc:
            log.info("Sainsbury's refused a session for the refreshed token (%s)", exc)
            return False
        self.state = AuthState.READY
        self.save()
        return True

    def _reauthorize(self) -> bool:
        """Rung four: ask the provider whether it still knows us.

        The rung the browser lives on, and the one that decides whether anybody
        is emailed a code. It costs one redirect chase and sends no credential,
        so it can never itself cause a code to be sent — which is why it is also
        safe on the quiet path that a page load takes.

        Below the token rungs rather than above them: those spend nothing and
        answer locally, while this is a round trip to the identity provider.
        Above the password rung because the password rung is the expensive one —
        it is the step that makes Sainsbury's send a six-digit code, and skipping
        it is the entire point.

        Skipped outright when we hold no identity cookie, because then the answer
        is known: a provider session *is* a cookie, so a jar without one cannot
        have one. Without this guard the rung fires on every page load of an
        account nobody has ever connected — a second of network, on the quiet
        path, to be told something the empty jar already said.
        """
        if not self.has_provider_session():
            return False
        try:
            result = authorize(self._http)
        except AuthError as exc:
            log.info("Sainsbury's would not start an authorization (%s)", exc)
            return False
        if result.code is None:
            return False
        try:
            self._settle_authorization(result.code, result.verifier)
        except AuthError as exc:
            # The provider offered a code and then would not honour it. Nothing
            # is broken that a full login cannot fix, so fall through to one.
            log.info("Sainsbury's session did not survive being redeemed (%s)", exc)
            return False
        return True

    def _login(
        self, *, email: str, password: str, allow_mailbox: bool = True
    ) -> AuthState:
        """Full login with request-local credentials, which may need an OTP."""
        started = time.time()
        log.info("Signing in to Sainsbury's")
        result = authorize(self._http)
        if result.code is not None:
            # The provider answered without wanting the password we were about
            # to send. Nothing to do but take it.
            self._settle_authorization(result.code, result.verifier)
            return self.state
        pending = result.pending
        try:
            code = submit_credentials(self._http, pending, email, password)
        except AuthError:
            self.state = AuthState.LOGGED_OUT
            raise

        if code is None:
            self.pending = pending
            # Being sent to the code form is not the same as a code being sent.
            # See request_code: the site fires this from the page's JavaScript,
            # so nothing has actually gone out until we ask.
            request_code(self._http)
            log.info("Sainsbury's has sent a one-time code")
            emailed = None
            if allow_mailbox:
                log.info("Watching the mailbox for it")
                emailed = read_emailed_code(since=started)
            if emailed is None:
                # Nowhere to read the code from, it never came, or the caller is
                # interactive and would rather be asked. Park: the code goes to
                # submit_otp when someone reads their email.
                self.state = AuthState.AWAITING_OTP
                return self.state
            code = submit_code(self._http, pending, emailed)

        self._settle(pending, code)
        return self.state

    def submit_otp(self, code: str) -> AuthState:
        """Finish a parked login with a code someone read out of their email."""
        with self._lock:
            pending = self.pending
            if pending is None:
                raise AuthError("No Sainsbury's login is waiting for a code")
            authorization = submit_code(self._http, pending, code)
            self._settle(pending, authorization)
            return self.state

    def _settle(self, pending: PendingLogin, authorization_code: str) -> None:
        self._settle_authorization(authorization_code, pending.code_verifier)

    def _settle_authorization(self, authorization_code: str, verifier: str) -> None:
        """Redeem an authorization code for a session, however it was obtained.

        Shared by the two ways one arrives: the provider handing one over
        because it still knows us, and a password (and usually a code) earning
        one. From here they are the same thing.
        """
        self.tokens = exchange_code(self._http, authorization_code, verifier)
        establish_gol_session(self._http, self.tokens)
        self.pending = None
        self.state = AuthState.READY
        self.save()
        self._log_provider_session()

    def _log_provider_session(self) -> None:
        """Record whether the provider left us a session cookie of its own.

        Rung 4 only works if it did, and whether it does is the provider's
        choice, not ours — so this is the one line that says whether the next
        re-login will be quiet or will email somebody a code. Names only: these
        are credentials, and the log is not the place for their values.
        """
        names = sorted(
            cookie.name
            for cookie in self._http.cookies.jar
            if "account.sainsburys" in (cookie.domain or "")
        )
        log.info(
            "Sainsbury's identity cookies now held: %s%s",
            ", ".join(names) or "none",
            ""
            if any(name.endswith("_session") for name in names)
            else " (no provider session — the next login will need a code)",
        )

    # --- requests ------------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One API call, re-authenticating once if the session has died.

        The retry is bounded at one: a second 401 means the credentials are wrong
        or the account is locked, and hammering either is how you get locked out
        properly.
        """
        url = path if path.startswith("http") else f"{GOL_URL}{path}"
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_S)
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Referer", f"{GOL_URL}/gol-ui/groceries")

        def send() -> Any:
            # Rebuilt per attempt rather than once: re-authenticating mints a new
            # commerce token, and retrying with the old one would resend exactly
            # the credential that was just rejected.
            return self._http.request(
                method, url, headers={**self._auth_headers(), **headers}, **kwargs
            )

        response = send()
        if response.status_code in {401, 403}:
            # Deliberately the quiet climb, never the password. A 401 is an
            # ordinary event — the commerce cookies expire well inside an hour —
            # and the recoveries that matter (re-mint, refresh) need nobody.
            # Escalating here would mean any background request could email a
            # one-time code, so browsing the app could quietly spend an OTP.
            # A login that truly needs a person is asked for explicitly.
            if self.refresh_quietly() == AuthState.READY:
                response = send()
        self.save()
        return response


def _cookie_to_json(cookie: Cookie) -> dict[str, Any]:
    return {
        "version": cookie.version,
        "name": cookie.name,
        "value": cookie.value,
        "port": cookie.port,
        "port_specified": cookie.port_specified,
        "domain": cookie.domain,
        "domain_specified": cookie.domain_specified,
        "domain_initial_dot": cookie.domain_initial_dot,
        "path": cookie.path,
        "path_specified": cookie.path_specified,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "discard": cookie.discard,
        "comment": cookie.comment,
        "comment_url": cookie.comment_url,
        "rest": cookie._rest,
        "rfc2109": cookie.rfc2109,
    }


def _cookie_from_json(item: dict[str, Any]) -> Cookie:
    return Cookie(
        version=item.get("version", 0),
        name=item["name"],
        value=item["value"],
        port=item.get("port"),
        port_specified=item.get("port_specified", False),
        domain=item.get("domain") or ".sainsburys.co.uk",
        domain_specified=item.get("domain_specified", True),
        domain_initial_dot=item.get("domain_initial_dot", True),
        path=item.get("path") or "/",
        path_specified=item.get("path_specified", True),
        secure=item.get("secure", True),
        expires=item.get("expires"),
        discard=item.get("discard", False),
        comment=item.get("comment"),
        comment_url=item.get("comment_url"),
        rest=item.get("rest") or {},
        rfc2109=item.get("rfc2109", False),
    )


_SESSIONS: dict[str, SainsburysSession] = {}
_SESSIONS_LOCK = threading.Lock()


def account_dir(account_id: str) -> Path:
    """Where one account's session lives. Mirrors :func:`app.ocado.session.account_dir`.

    Note what is *not* here: the pre-registry jar at ``data/sainsburys/session.json``
    is no longer read. It was written when the app had one Sainsbury's login for
    everybody, so there is no honest answer to whose it is, and adopting it for
    whoever signed in first would hand them somebody else's trolley. It costs one
    sign-in to replace and the file can be deleted.
    """
    return config.DATA_DIR / "sainsburys" / "accounts" / account_id


def get_account_session(account_id: str) -> SainsburysSession:
    """The process-wide session for one Sainsbury's account.

    One per process per account, not per request: the cookie jar, the rotating
    refresh token and the parked login are all shared state, and a per-request
    session would drop the login between the password and the emailed code.
    """
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(account_id)
        if session is None:
            session = SainsburysSession(
                jar_path=account_dir(account_id) / "session.json"
            )
            _SESSIONS[account_id] = session
        return session
