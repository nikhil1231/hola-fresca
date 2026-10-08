"""HelloFresh deliveries, reduced to what a person asks about them: which weeks a box
is coming, until when it can still be skipped or changed, and what's in it.

Shapes confirmed against a real account (``tests/fixtures/hellofresh/``):

* ``/api/customers/me/deliveries`` → ``{"items": [delivery]}``; a delivery's ``id`` is
  its ISO week, ``status`` is ``RUNNING``/``PAUSED``/``DELIVERED`` and ``state`` the
  finer ``PREPARING``/``PAUSED``/``DELIVERED``. It carries no meals.
* ``allowedActions.pause`` is HelloFresh's own word on whether the week can still
  be skipped or un-skipped; once the cutoff passes it goes ``false``.
* Meals come from ``/my-deliveries/menu`` for that week, the chosen ones having a
  non-zero ``selection.quantity``.
* Subscriptions carry ``isActive``/``canceledAt`` and the plan id as
  ``customerPlanId``; plans have no status, only a ``finalDeliveryWeek`` once ending.

Anything missing is ``None`` rather than an error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.hellofresh.client import COUNTRY

RETAILER = "hellofresh"


@dataclass(frozen=True, slots=True)
class Box:
    week: str | None
    subscription_id: str | None
    status: str | None
    delivery_date: str | None
    cutoff: str | None
    skipped: bool
    changeable: bool = False
    meals: tuple[str, ...] = field(default_factory=tuple)


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_range(today: date, weeks: int) -> tuple[str, str]:
    return iso_week(today), iso_week(today + timedelta(weeks=max(weeks - 1, 0)))


_ISO_WEEK = re.compile(r"^\d{4}-W\d{2}$")


def week_of(value: str) -> str:
    """An ISO week from either an ISO week (``2026-W42``) or any date in it."""
    value = value.strip()
    if _ISO_WEEK.match(value):
        return value
    return iso_week(date.fromisoformat(value))


def past_cutoff(delivery: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the week's cutoff has gone. An unreadable cutoff reads as not past."""
    raw = delivery.get("cutoffDate") or delivery.get("cutoff")
    if not isinstance(raw, str):
        return False
    try:
        cutoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if cutoff.tzinfo is None:  # a bare date: the whole of that day
        cutoff = cutoff.replace(tzinfo=timezone.utc) + (timedelta(days=1) if len(raw) <= 10 else timedelta())
    return (now or datetime.now(timezone.utc)) >= cutoff


def changeable(delivery: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the week can still be skipped or un-skipped: HelloFresh's own
    ``allowedActions.pause`` where it says, else the cutoff."""
    allowed = delivery.get("allowedActions")
    if isinstance(allowed, dict) and "pause" in allowed:
        return bool(allowed["pause"])
    return not past_cutoff(delivery, now)


def _items(payload: Any) -> list:
    if isinstance(payload, dict):
        return next((payload[k] for k in ("items", "subscriptions", "plans", "data") if isinstance(payload.get(k), list)),
                    [payload] if payload.get("id") is not None else [])
    return payload if isinstance(payload, list) else []


def _live(item: dict[str, Any]) -> bool:
    return (item.get("isActive") is not False and not item.get("canceledAt") and not item.get("finalDeliveryWeek")
            and str(item.get("status") or "").upper() not in {"CANCELED", "CANCELLED", "INACTIVE", "ENDED"})


def live(payload: Any) -> list[dict[str, Any]]:
    """The subscriptions (or plans) in ``payload`` that haven't ended."""
    return [i for i in _items(payload) if isinstance(i, dict) and i.get("id") is not None and _live(i)]


def live_ids(payload: Any) -> list[str]:
    return [str(i["id"]) for i in live(payload)]


def menu_params(subscription: dict[str, Any], delivery: dict[str, Any], week: str) -> dict[str, str]:
    """The query the deliveries page sends for one week's menu, built from the
    subscription and that week's delivery (whose box can differ from the plan's)."""
    product = delivery.get("product") or {}
    sku = product.get("handle") or (subscription.get("product") or {}).get("sku") or ""
    servings = (product.get("specs") or {}).get("size") or (sku.split("-")[3] if sku.count("-") >= 3 else "")
    option = (delivery.get("deliveryOption") or {}).get("handle") or subscription.get("deliveryTime") or ""
    return {
        "customerPlanId": str(subscription.get("customerPlanId") or ""),
        "delivery-option": option,
        "postcode": str((subscription.get("shippingAddress") or {}).get("postcode") or ""),
        "preference": str(subscription.get("preset") or ""),
        "product-sku": sku,
        "servings": str(servings),
        "subscription": str(subscription.get("id") or ""),
        "week": week,
        "exclude": "",
        "exclude-feedback": "true",
        "include-filters": "false",
        "include-future-feedback": "false",
    }


def chosen_meals(menu: Any) -> list[str]:
    meals = menu.get("meals") if isinstance(menu, dict) else None
    return [m["recipe"]["name"].strip() for m in meals or []
            if isinstance(m, dict) and ((m.get("selection") or {}).get("quantity") or 0) > 0
            and isinstance(m.get("recipe"), dict) and m["recipe"].get("name")]


def summarise(payload: Any, meals: dict[str, list[str]] | None = None) -> list[Box]:
    """``meals`` maps a week to its chosen recipes, fetched separately."""
    boxes = []
    for raw in _items(payload):
        if not isinstance(raw, dict):
            continue
        delivery = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else raw
        week = delivery.get("id") or delivery.get("week")
        status = delivery.get("status")
        boxes.append(Box(
            week=week,
            subscription_id=_str(delivery.get("subscriptionId") or raw.get("subscriptionId")),
            status=status,
            delivery_date=delivery.get("deliveryDate"),
            cutoff=delivery.get("cutoffDate") or delivery.get("cutoff"),
            skipped=status == "PAUSED",
            changeable=changeable(delivery),
            meals=tuple((meals or {}).get(week) or ()),
        ))
    return sorted(boxes, key=lambda b: b.week or "")


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["Box", "RETAILER", "COUNTRY", "changeable", "chosen_meals", "iso_week", "live", "live_ids", "menu_params",
           "past_cutoff", "summarise", "week_of", "week_range"]
