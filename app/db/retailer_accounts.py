"""The registry of who is connected to which shop, and how it is looked up.

Two halves. The bottom of the file is the one-off backfill from the legacy
environment config, which exists so that existing cookie/profile keys and their
owners survive the deployment. The top is what the API uses every request: given
a user and a retailer, which account is theirs.

Nothing here stores a password. Credentials are an input to an interactive
login; what is persisted is the session it produced, and that lives outside the
database in the account's own directory.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import RetailerAccount

#: What :attr:`RetailerAccount.status` may hold. ``connected`` is the last-known
#: state and never a promise: the upstream cookie can die at any time without
#: anything telling us, which is what the auth ladder is for.
NEVER = "never"
CONNECTED = "connected"
NEEDS_PASSWORD = "needs_password"


def find(session: Session, user_id: int, retailer: str) -> RetailerAccount | None:
    """This user's account at this shop, if they have connected one."""
    return session.scalar(
        select(RetailerAccount).where(
            RetailerAccount.user_id == user_id,
            RetailerAccount.retailer == retailer,
        )
    )


def _new_key(user_id: int) -> str:
    """A stable, opaque id for the account's cookie jar and browser profile.

    The user id is in there to make ``data/ocado/accounts/`` legible to whoever
    is ssh'd into the box wondering whose profile is which, and the random half
    is what keeps it unique across shops — the column is globally unique, and one
    person connecting two retailers would otherwise collide with themselves.

    It is never derived from the email: an account that changes address keeps its
    session, which is the whole reason the key exists rather than the address
    being used directly.
    """
    return f"u{user_id}-{secrets.token_hex(4)}"


def connect(
    session: Session, user_id: int, retailer: str, *, email: str
) -> RetailerAccount:
    """The account row this user shops at ``retailer`` with, creating it if new.

    Called from the login endpoint, before the login is attempted rather than
    after. A row that ends up never signing in successfully is not a failure to
    clean up: it remembers the address that was tried, so the form comes back
    filled in, and it costs one empty directory.
    """
    existing = find(session, user_id, retailer)
    if existing is not None:
        return existing

    account = RetailerAccount(
        user_id=user_id,
        retailer=retailer,
        key=_new_key(user_id),
        email=email or None,
        status=NEVER,
    )
    session.add(account)
    session.commit()
    return account


def record_status(
    session: Session,
    account: RetailerAccount,
    auth_status: str,
    *,
    email: str | None = None,
    after_login: bool = False,
) -> None:
    """Write back what the auth ladder just found out about this account.

    ``after_login`` separates "the quiet climb could not get there" from
    "somebody typed a password and it still did not work". Only the second is
    worth recording as a login attempt; the first happens on every page load.
    """
    now = datetime.now(timezone.utc)
    if email:
        account.email = email
    if auth_status == "ready":
        account.status = CONNECTED
        account.last_ok_at = now
    elif after_login:
        account.status = NEEDS_PASSWORD
    if after_login:
        account.last_login_at = now
    session.commit()


def disconnect(session: Session, account: RetailerAccount) -> None:
    """Forget the session, but not the account.

    The row is deliberately kept. Its key names a directory that Ocado's login
    cares about keeping: :meth:`app.ocado.auth.AuthLadder.forget` removes the
    cookies from the browser profile and leaves the rest of it alone, because
    presenting Ocado with a brand-new browser identity makes its invisible
    reCAPTCHA far more likely to stall the next sign-in. Deleting the row would
    mean a new key, a new profile, and exactly that.
    """
    account.status = NEVER
    account.last_ok_at = None
    session.commit()


class LegacyAccount(Protocol):
    id: str
    email: str | None
    otp_markers: tuple[str, ...]


def seed_legacy_ocado_accounts(
    connection: Connection, accounts: Sequence[LegacyAccount]
) -> None:
    """Insert missing configured Ocado accounts without changing existing rows.

    Email matches win.  Otherwise the first user without an Ocado connection is
    used (the historical bootstrap owner on a single-user database).  If more
    configured accounts remain, a non-admin placeholder user is created for
    each one so the one-account-per-user constraint does not discard a live
    cookie/profile directory.  An Access identity with the same email will find
    that placeholder naturally instead of creating a duplicate user later.
    """
    # Compatibility migrations are also exercised against intentionally partial
    # historical schemas.  The registry can be created there, but there is no
    # owner to backfill until this is a real application database.
    if not accounts or not inspect(connection).has_table("users"):
        return

    existing_keys = set(
        connection.scalars(text("SELECT key FROM retailer_accounts"))
    )
    occupied_user_ids = set(
        connection.scalars(
            text("SELECT user_id FROM retailer_accounts WHERE retailer = 'ocado'")
        )
    )
    users = [
        (int(row.id), row.email)
        for row in connection.execute(
            text("SELECT id, email FROM users ORDER BY id")
        ).all()
    ]

    for account in accounts:
        if account.id in existing_keys:
            continue

        wanted_email = (account.email or "").strip()
        wanted_folded = wanted_email.casefold()
        user_id = next(
            (
                candidate_id
                for candidate_id, candidate_email in users
                if candidate_id not in occupied_user_ids
                and candidate_email
                and str(candidate_email).casefold() == wanted_folded
            ),
            None,
        )
        if user_id is None:
            user_id = next(
                (
                    candidate_id
                    for candidate_id, _ in users
                    if candidate_id not in occupied_user_ids
                ),
                None,
            )

        if user_id is None:
            email_is_free = bool(wanted_email) and all(
                not candidate_email
                or str(candidate_email).casefold() != wanted_folded
                for _, candidate_email in users
            )
            result = connection.execute(
                text(
                    "INSERT INTO users (email, name, is_admin, created_at) "
                    "VALUES (:email, NULL, 0, CURRENT_TIMESTAMP)"
                ),
                {"email": wanted_email if email_is_free else None},
            )
            user_id = result.lastrowid
            if user_id is None:  # pragma: no cover - SQLite always supplies it
                raise RuntimeError("could not create a user for the retailer account")
            users.append((user_id, wanted_email if email_is_free else None))

        markers = json.dumps(list(account.otp_markers)) if account.otp_markers else None
        connection.execute(
            text(
                """
                INSERT INTO retailer_accounts (
                    user_id, retailer, key, email, otp_markers, status,
                    last_ok_at, last_login_at, created_at
                ) VALUES (
                    :user_id, 'ocado', :key, :email, :otp_markers, 'never',
                    NULL, NULL, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "user_id": user_id,
                "key": account.id,
                "email": account.email,
                "otp_markers": markers,
            },
        )
        existing_keys.add(account.id)
        occupied_user_ids.add(user_id)
