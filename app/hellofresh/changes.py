"""Changing one week of a HelloFresh subscription, with the two rules that hold
whoever is asking -- a request from the app, or the weekly auto-skip.

A week already in the wanted status is left alone and reported unchanged, so
asking twice costs nothing: that is what makes a retried request free, and what
lets the auto-skip meet a week it has already settled without a second thought.
A week past its cutoff is refused rather than attempted, with the cutoff passed
back, because HelloFresh would reject the ``PATCH`` anyway and the reason is the
useful part.

Both rules were confirmed against a real account: the ``PATCH`` moved a live week
to ``PAUSED``, and the same call repeated reported no change without sending one.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.hellofresh import boxes
from app.hellofresh.client import HelloFreshClient, unwrap


@dataclass(frozen=True, slots=True)
class WeekChange:
    week: str
    subscription_id: str
    #: What the week is in now: the wanted status, or the status it is stuck at.
    status: str | None
    changed: bool
    #: Why nothing happened, when the week could not be changed at all.
    refused: str | None = None


def set_week(client: HelloFreshClient, subscription_id: str, week: str, status: str) -> WeekChange:
    """Put ``week`` into ``status``, or say why it could not be."""
    delivery = unwrap(client.delivery(subscription_id, week))
    current = delivery.get("status")
    if current == status:
        return WeekChange(week, subscription_id, status, changed=False)
    if not boxes.changeable(delivery):
        return WeekChange(week, subscription_id, current, changed=False,
                          refused=f"past its cutoff ({delivery.get('cutoffDate')})")
    client.set_delivery_status(subscription_id, week, status,
                              cutoff_date=delivery.get("cutoffDate"),
                              delivery_date=delivery.get("deliveryDate"))
    return WeekChange(week, subscription_id, status, changed=True)


__all__ = ["WeekChange", "set_week"]
