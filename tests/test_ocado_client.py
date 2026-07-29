from __future__ import annotations

from app.ocado.client import normalize_slots


def test_slots_without_delivery_price_are_unavailable():
    slots = normalize_slots(
        {
            "days": [
                {
                    "slots": [
                        {
                            "slotId": "available",
                            "startTime": "2026-07-30T10:00:00Z",
                            "endTime": "2026-07-30T11:00:00Z",
                            "deliveryPrice": {"amount": "2.99"},
                            "attributes": {"eco": True},
                        },
                        {
                            "slotId": "missing-price",
                            "startTime": "2026-07-30T12:00:00Z",
                            "endTime": "2026-07-30T13:00:00Z",
                            "attributes": {"available": True},
                        },
                    ]
                }
            ]
        }
    )

    by_id = {slot.slot_id: slot for slot in slots}
    assert by_id["available"].available is True
    assert by_id["available"].eco is True
    assert by_id["available"].price == 2.99
    assert by_id["missing-price"].available is False

