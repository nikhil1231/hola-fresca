"""Cloudflare Access identity for an incoming request.

The app is published through a Cloudflare Tunnel. Access does the Google
sign-in and the email allowlist at the edge, then forwards the request carrying
a signed assertion of who the person is. This module turns that assertion into
an email address, or refuses the request.

Two decisions are worth stating, because both are the difference between this
being authentication and being decoration:

* The ``Cf-Access-Authenticated-User-Email`` header is never read. Cloudflare
  sets it, but nothing stops anyone else setting it too, and this box also
  answers on the LAN — so a header is a claim, not a proof. Only the JWT is
  trusted, and only once its signature, audience, issuer and expiry check out
  against the team's published keys.
* A request that asks for the public hostname without a valid assertion is
  refused rather than falling back. That is the case that matters if the tunnel
  is ever reachable with the Access policy switched off or misconfigured: the
  fallback would hand a stranger the owner's account, so there isn't one.

LAN and Tailscale requests — which address the laptop by IP, not by the public
name — get ``None`` and the caller's existing single-user behaviour. That is a
deliberate choice about a trusted home network, not an oversight.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from app import config

log = logging.getLogger(__name__)

# The assertion travels as a header on API calls and as a cookie on the initial
# page load, so both are checked. Names are Cloudflare's.
_HEADER = "Cf-Access-Jwt-Assertion"
_COOKIE = "CF_Authorization"

# How long the signing keys are held before refetching. Cloudflare rotates them
# on the order of weeks; PyJWKClient refetches early anyway when it sees a token
# signed by a key id it does not know.
_JWKS_LIFESPAN_S = 3600


@dataclass(frozen=True, slots=True)
class _Settings:
    team_domain: str
    aud: str
    hostname: str | None

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"


def _settings() -> _Settings | None:
    """Access settings, or ``None`` when it is not configured.

    Read from :mod:`app.config` on every call rather than captured at import, so
    a test can monkeypatch the module attributes and so the deploy can set them
    without an import-order rule.
    """
    team_domain = (config.ACCESS_TEAM_DOMAIN or "").strip()
    aud = (config.ACCESS_AUD or "").strip()
    if not team_domain or not aud:
        return None
    # Tolerate a full URL being pasted in where a hostname was asked for; the
    # team domain shows up both ways in Cloudflare's own dashboard.
    team_domain = team_domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    hostname = (config.ACCESS_HOSTNAME or "").strip().lower() or None
    return _Settings(team_domain=team_domain, aud=aud, hostname=hostname)


@lru_cache(maxsize=4)
def _jwk_client(certs_url: str) -> PyJWKClient:
    """One key client per team, holding the fetched key set between requests."""
    return PyJWKClient(certs_url, cache_jwk_set=True, lifespan=_JWKS_LIFESPAN_S)


def _token(request: Request) -> str | None:
    return request.headers.get(_HEADER) or request.cookies.get(_COOKIE)


def _addressed_to_tunnel(request: Request, settings: _Settings) -> bool:
    """Whether this request asked for the public hostname.

    The port is stripped because the Host header carries one when the client
    used a non-default port, and an IPv6 literal is left alone — it will never
    match a hostname anyway.
    """
    if settings.hostname is None:
        return False
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):
        return False
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host == settings.hostname


def authenticated_email(request: Request) -> str | None:
    """The verified Access identity for ``request``.

    Returns the email address Access signed for, or ``None`` when Access is not
    configured or the request came in over the LAN without an assertion. Raises
    403 when an assertion is present but does not verify, and when one is absent
    from a request addressed to the public hostname.
    """
    settings = _settings()
    if settings is None:
        return None

    token = _token(request)
    if not token:
        if _addressed_to_tunnel(request, settings):
            # Reachable from outside and unauthenticated: refuse rather than
            # fall back to the owner's account.
            log.warning("Access assertion missing on a request for %s", settings.hostname)
            raise HTTPException(status_code=403, detail="Cloudflare Access sign-in required")
        return None

    try:
        signing_key = _jwk_client(settings.certs_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.aud,
            issuer=settings.issuer,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except Exception as exc:  # PyJWT raises a family of these; all mean "no".
        log.warning("Rejected an Access assertion: %s", exc)
        raise HTTPException(status_code=403, detail="Invalid Cloudflare Access token") from exc

    email = (claims.get("email") or "").strip()
    if not email:
        # A service-token assertion authenticates a machine, not a person, and
        # carries common_name instead. Nothing here is machine-facing.
        log.warning("Access assertion carried no email; refusing")
        raise HTTPException(status_code=403, detail="Access token is not a user identity")
    return email
