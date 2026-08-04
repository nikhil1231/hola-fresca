"""Standing pack choices belong to a user, not to the ingredient.

The distinction this file exists for: an ingredient mapping says which products
*are* rice, and everyone shares that. Which bag of it you buy is yours, and used
to be stored on the same shared row.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import IngredientMapping, User, UserPackPreference
from app.planner.preferences import pack_preferences, set_pack_preference

KEY = "name:rice"


def _users(session) -> tuple[int, int]:
    mine = session.scalar(select(User.id).order_by(User.id).limit(1))
    other = User(name="Someone Else")
    session.add(other)
    session.commit()
    return mine, other.id


def test_a_preference_is_read_back_per_user(factory):
    with factory() as session:
        mine, yours = _users(session)

        set_pack_preference(session, mine, KEY, "1kg")
        set_pack_preference(session, yours, KEY, "500g")
        session.commit()

        assert pack_preferences(session, mine) == {KEY: "1kg"}
        assert pack_preferences(session, yours) == {KEY: "500g"}


def test_setting_a_preference_twice_replaces_rather_than_duplicates(factory):
    with factory() as session:
        mine, _ = _users(session)

        set_pack_preference(session, mine, KEY, "1kg")
        session.commit()
        set_pack_preference(session, mine, KEY, "500g")
        session.commit()

        rows = session.scalars(
            select(UserPackPreference).where(UserPackPreference.user_id == mine)
        ).all()
        assert [row.sku for row in rows] == ["500g"]


def test_releasing_a_preference_removes_only_that_users_row(factory):
    with factory() as session:
        mine, yours = _users(session)
        set_pack_preference(session, mine, KEY, "1kg")
        set_pack_preference(session, yours, KEY, "1kg")
        session.commit()

        set_pack_preference(session, mine, KEY, None)
        session.commit()

        assert pack_preferences(session, mine) == {}
        assert pack_preferences(session, yours) == {KEY: "1kg"}


def test_the_shared_mapping_row_no_longer_carries_a_preference():
    """A guard on the schema: the column moving back would silently re-share it."""
    assert not hasattr(IngredientMapping, "preferred_sku")
