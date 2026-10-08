"""Who shares whose shopping. See :data:`app.config.HOUSEHOLD`.

A shop account belongs to one person, and most reads stay that way. Two things
reach across: Noodle's feed (the household's coming deliveries, whoever's account
holds them) and managing an order that sits on another member's account. Both ask
here, so the rule lives in one place: members of the household, nobody else.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db.models import RetailerAccount, User


def is_member(user: User) -> bool:
    return not config.HOUSEHOLD or (user.email or "").lower() in config.HOUSEHOLD


def member_ids(session: Session) -> list[int]:
    users = session.scalars(select(User).order_by(User.id)).all()
    return [user.id for user in users if is_member(user)]


def accounts(session: Session, user: User, retailer: str) -> list[RetailerAccount]:
    """The shop accounts ``user`` may act on: their own first, then (if they are in
    the household) the other members'."""
    ids = [user.id] + ([i for i in member_ids(session) if i != user.id] if is_member(user) else [])
    rows = session.scalars(
        select(RetailerAccount).where(RetailerAccount.retailer == retailer, RetailerAccount.user_id.in_(ids))
    ).all()
    return sorted(rows, key=lambda account: ids.index(account.user_id))
