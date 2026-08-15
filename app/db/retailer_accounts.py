"""Backfill the database account registry from the legacy environment config.

This is intentionally transitional.  Step one moves account *discovery* into
the database without changing how a login obtains its password.  Step two will
remove the environment credential registry; until then this seed keeps existing
cookie/profile keys and their owners stable across the deployment.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


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
