"""Central configuration and filesystem paths for HolaFresca.

Everything is overridable via environment variables so tests can point at a
throwaway directory without touching the real data store.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repository root (…/HolaFresca). This file lives at app/config.py.
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load a repo-root .env (gitignored) so CLI jobs and the API both pick up secrets
# such as OPENAI_API_KEY without extra wiring.
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = Path(os.environ.get("HOLAFRESCA_DATA_DIR", ROOT_DIR / "data"))
RAW_DIR = Path(os.environ.get("HOLAFRESCA_RAW_DIR", DATA_DIR / "raw"))
DB_PATH = Path(os.environ.get("HOLAFRESCA_DB_PATH", DATA_DIR / "holafresca.db"))

# HelloFresh CDN base for building absolute image URLs from stored image paths.
# Image paths are stored relative (e.g. "/image/foo.jpg"); the frontend can
# request whatever transformation size it needs.
HELLOFRESH_IMAGE_BASE = "https://img.hellofresh.com/hellofresh_s3"

# Cloudflare Access — who is allowed in, and who this request is. The app is
# published through a Cloudflare Tunnel; Access sits in front of it doing the
# Google sign-in and the email allowlist, and forwards a signed assertion of the
# identity. See app/api/access.py for how that assertion is checked.
#
# Leave ACCESS_TEAM_DOMAIN or ACCESS_AUD unset and none of it is enforced, which
# is what local dev and the test suite run as: every request is the bootstrap
# account, exactly as before this existed.
ACCESS_TEAM_DOMAIN = os.environ.get("HOLAFRESCA_ACCESS_TEAM_DOMAIN")
ACCESS_AUD = os.environ.get("HOLAFRESCA_ACCESS_AUD")
# The public hostname the tunnel serves. Requests arriving *for that name* must
# carry a valid assertion; requests to the LAN address are still let through as
# the bootstrap user, because the laptop keeps answering on 0.0.0.0:8100. Unset
# it and that distinction cannot be drawn — see app/api/access.py.
ACCESS_HOSTNAME = os.environ.get("HOLAFRESCA_ACCESS_HOSTNAME")
# The address that owns the existing data. The bootstrap user predates having an
# email, so the first sign-in by this address claims that row rather than making
# a second account and leaving the plan, hides and preferences behind.
ACCESS_OWNER_EMAIL = os.environ.get("HOLAFRESCA_ACCESS_OWNER_EMAIL")

# OpenAI settings for the ingredient→product mapping proposal pass. The key is
# never committed — it lives in the gitignored .env. The model is overridable so
# a different id can be used without a code change.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("HOLAFRESCA_OPENAI_MODEL", "gpt-5.6-luna")
COOK_MAP_MODEL = os.environ.get("HOLAFRESCA_COOK_MAP_MODEL", OPENAI_MODEL)
# The recipe audit asks one narrow question — per-100g composition for a short
# ingredient list — and does the arithmetic itself, so a small model is enough.
AUDIT_MODEL = os.environ.get("HOLAFRESCA_AUDIT_MODEL", "gpt-5.4-mini")

# Ocado login is deliberately isolated to the auth ladder; normal basket and
# slot calls use persisted account-specific session cookie jars.

# The mailbox the login codes are read from, so re-auth needs nobody at the
# keyboard. A dedicated account read over IMAP with an app-specific password;
# the real Ocado addresses forward their Ocado mail into it. See
# app/ocado/otp_mail.py for why it is not the address registered with Ocado.
# Leave the user or password unset and login falls back to asking for the code.
OCADO_OTP_IMAP_HOST = os.environ.get("OCADO_OTP_IMAP_HOST", "imap.gmail.com")
OCADO_OTP_IMAP_PORT = int(os.environ.get("OCADO_OTP_IMAP_PORT", "993"))
OCADO_OTP_IMAP_USER = os.environ.get("OCADO_OTP_IMAP_USER")
OCADO_OTP_IMAP_PASSWORD = os.environ.get("OCADO_OTP_IMAP_PASSWORD")
OCADO_OTP_IMAP_FOLDER = os.environ.get("OCADO_OTP_IMAP_FOLDER", "INBOX")
# How long a login waits for the code to land before handing back to the manual
# endpoint. Forwarding usually takes seconds; this is the ceiling, not the cost.
OCADO_OTP_WAIT_S = float(os.environ.get("OCADO_OTP_WAIT_S", "120"))
OCADO_OTP_POLL_S = float(os.environ.get("OCADO_OTP_POLL_S", "4"))


@dataclass(frozen=True, slots=True)
class OcadoAccountConfig:
    id: str
    label: str
    email: str | None = None
    password: str | None = None
    #: Strings that identify this account's mail in the shared OTP mailbox.
    otp_markers: tuple[str, ...] = ()


_ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _account_env_prefix(account_id: str) -> str:
    return "OCADO_" + re.sub(r"[^A-Za-z0-9]", "_", account_id).upper()


def _otp_markers(account_id: str, email: str | None) -> tuple[str, ...]:
    """How this account's mail is recognised in the shared OTP mailbox.

    Two accounts forward into one inbox, so a code has to be tied back to an
    account before it is used. Both defaults are things that survive forwarding:
    the address Ocado sent to, and the plus-addressed destination it was
    forwarded to. An explicit override wins if a provider mangles both.
    """
    override = os.environ.get(f"{_account_env_prefix(account_id)}_OTP_MARKERS")
    if override is not None:
        return tuple(item.strip() for item in override.split(",") if item.strip())
    markers = [email] if email else []
    if OCADO_OTP_IMAP_USER:
        from app.ocado.otp_mail import plus_address

        markers.append(plus_address(OCADO_OTP_IMAP_USER, account_id))
    return tuple(markers)


def _configured_ocado_accounts() -> tuple[OcadoAccountConfig, ...]:
    account_ids = [
        item.strip()
        for item in os.environ.get("OCADO_ACCOUNTS", "").split(",")
        if item.strip()
    ]
    if not account_ids:
        email = os.environ.get("OCADO_EMAIL")
        return (
            OcadoAccountConfig(
                id="default",
                label=os.environ.get("OCADO_LABEL") or email or "Ocado",
                email=email,
                password=os.environ.get("OCADO_PASSWORD"),
                otp_markers=_otp_markers("default", email),
            ),
        )

    accounts: list[OcadoAccountConfig] = []
    seen: set[str] = set()
    for account_id in account_ids:
        if not _ACCOUNT_ID_RE.match(account_id):
            raise RuntimeError(
                f"Invalid Ocado account id {account_id!r}; use letters, numbers, underscores or hyphens"
            )
        if account_id in seen:
            raise RuntimeError(f"Duplicate Ocado account id {account_id!r}")
        seen.add(account_id)
        prefix = _account_env_prefix(account_id)
        email = os.environ.get(f"{prefix}_EMAIL")
        accounts.append(
            OcadoAccountConfig(
                id=account_id,
                label=os.environ.get(f"{prefix}_LABEL") or account_id,
                email=email,
                password=os.environ.get(f"{prefix}_PASSWORD"),
                otp_markers=_otp_markers(account_id, email),
            )
        )
    return tuple(accounts)


OCADO_ACCOUNTS = _configured_ocado_accounts()
OCADO_ACCOUNT_IDS = tuple(account.id for account in OCADO_ACCOUNTS)
DEFAULT_OCADO_ACCOUNT_ID = OCADO_ACCOUNTS[0].id

# Legacy names are kept for code/tests that construct the default ladder by hand.
OCADO_EMAIL = OCADO_ACCOUNTS[0].email
OCADO_PASSWORD = OCADO_ACCOUNTS[0].password
# Login drives a real browser (Ocado's SSO runs reCAPTCHA, which no HTTP client
# can clear). Headless by default so it works on a box with no display; if
# reCAPTCHA starts challenging, run the server under `xvfb-run` and set this to 0
# for a headed browser, which is challenged less often.
OCADO_LOGIN_HEADLESS = os.environ.get("OCADO_LOGIN_HEADLESS", "1").lower() not in {
    "0",
    "false",
    "no",
}


def db_url(path: Path | None = None) -> str:
    """Return a SQLAlchemy URL for the SQLite database at ``path``."""
    return f"sqlite:///{(path or DB_PATH)}"


def ensure_dirs() -> None:
    """Create the data directories if they do not already exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
