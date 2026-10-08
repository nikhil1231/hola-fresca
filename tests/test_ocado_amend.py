"""Moving a coming Ocado order to another slot, and who may. The client is faked."""
from __future__ import annotations

import pytest

from app import config, household
from app.db import retailer_accounts
from app.db.models import User
from app.ocado import amend

ORDER = {
    "orderId": 1629420692934, "regionId": "region", "deliveryDestinationId": "ddid", "slotId": "tue-10",
    "editStatus": "EDITABLE", "recurringShoppingDefinition": {"definitionId": "weekly"},
    "dates": {"deliveryStartDate": "2026-10-13T10:00:00+01:00", "deliveryEndDate": "2026-10-13T11:00:00+01:00"},
}
MONDAY = {"deliveryStartDate": "2026-10-12T10:30:00+01:00", "deliveryEndDate": "2026-10-12T11:30:00+01:00"}


class FakeOcado:
    def __init__(self, checkout_type="checkout-result-response", fail_on=None):
        self.calls, self.order = [], dict(ORDER)
        self.checkout_type, self.fail_on, self.checkout_body = checkout_type, fail_on, None

    def _log(self, name, *args):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f"{name} failed")

    def orders(self, pending=False):
        return [self.order]

    def order_payments(self, order_id):
        return {"payments": [{"paymentMethod": {"type": "CARDS", "group": "PAY_NOW"}}]}

    def wallet_items(self):
        return [{"walletItemId": "old", "paymentMethodType": "CARDS", "expired": True},
                {"walletItemId": "card", "paymentMethodType": "CARDS", "expired": False}]

    def default_billing_address_id(self):
        return None

    def start_order_edit(self, order_id, region):
        self._log("start")

    def cancel_order_edit(self, order_id, region):
        self._log("cancel")

    def reserve(self, slot_id, ddid=None, region=None):
        self._log("reserve")
        self.reserved = slot_id

    def checkout_start(self):
        self._log("checkout_start")

    def checkout_summary(self):
        self._log("summary")

    def checkout(self, body):
        self._log("checkout")
        self.checkout_body = body
        if self.checkout_type == "checkout-result-response":
            self.order = {**self.order, "slotId": self.reserved, "dates": MONDAY}
        return {"type": self.checkout_type}

    def slots(self, ddid=None, region=None, days=7):
        self._log("slots")
        return ["a slot"]


def test_moving_confirms_the_change_for_this_delivery_only_with_the_same_card():
    fake = FakeOcado()
    moved = amend.move_slot(fake, "1629420692934", "mon-1030")
    assert fake.calls == ["start", "reserve", "checkout_start", "summary", "checkout"]
    assert fake.checkout_body["paymentMethod"] == {"group": "PAY_NOW", "type": "CARDS", "toSave": False,
                                                   "walletItemId": "card"}
    assert fake.checkout_body["recurringOrder"] == {"edit": {"replaceItems": False}}
    assert "billingAddressId" not in fake.checkout_body  # the account has none: sent without, as ocado.com does
    assert (moved.slot_id, moved.delivery_start) == ("mon-1030", MONDAY["deliveryStartDate"])


@pytest.mark.parametrize("fake", [FakeOcado(checkout_type="payment-session-response"), FakeOcado(fail_on="reserve"),
                                  FakeOcado(fail_on="checkout")], ids=["card-check", "slot-gone", "refused"])
def test_any_failure_cancels_the_edit_and_leaves_the_order(fake):
    with pytest.raises(amend.OrderChangeError):
        amend.move_slot(fake, "1629420692934", "mon-1030")
    assert fake.calls[0] == "start" and fake.calls[-1] == "cancel" and "checkout" not in fake.calls[fake.calls.index("cancel"):]
    assert fake.order["slotId"] == "tue-10"


def test_a_card_check_is_left_to_the_customer():
    with pytest.raises(amend.OrderChangeError, match="ocado.com"):
        amend.move_slot(FakeOcado(checkout_type="payment-session-response"), "1629420692934", "mon-1030")


def test_an_order_past_its_cutoff_isnt_touched():
    fake = FakeOcado()
    fake.order["editStatus"] = "NOT_EDITABLE"
    with pytest.raises(amend.OrderChangeError):
        amend.move_slot(fake, "1629420692934", "mon-1030")
    assert fake.calls == []


def test_slots_are_asked_inside_an_edit_that_is_then_dropped():
    fake = FakeOcado()
    assert amend.slots_for(fake, ORDER) == ["a slot"]
    assert fake.calls == ["start", "slots", "cancel"]


def test_household_members_manage_each_others_accounts_and_nobody_else_does(factory, monkeypatch):
    with factory() as session:
        owner = session.query(User).order_by(User.id).first()
        owner.email = "owner@x.com"
        partner, friend = User(email="partner@x.com", name="P"), User(email="friend@x.com", name="F")
        session.add_all([partner, friend])
        session.commit()
        for user in (owner, partner, friend):
            retailer_accounts.connect(session, user.id, "ocado", email=f"{user.id}@shop.com")
        monkeypatch.setattr(config, "HOUSEHOLD", {"owner@x.com", "partner@x.com"})
        mine = [a.user_id for a in household.accounts(session, owner, "ocado")]
        assert mine == [owner.id, partner.id]  # own account first
        assert [a.user_id for a in household.accounts(session, friend, "ocado")] == [friend.id]
        assert household.member_ids(session) == [owner.id, partner.id]
