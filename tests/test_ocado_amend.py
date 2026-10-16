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
        self.quantities = {"sku": 2}
        self.saved_quantities = dict(self.quantities)
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
        self.quantities = dict(self.saved_quantities)

    def set_quantities(self, quantities):
        self._log("quantities")
        self.quantities.update(quantities)

    def get_order(self, order_id):
        return {**self.order, "items": [{"product": sku, "quantity": qty}
                                      for sku, qty in self.saved_quantities.items() if qty]}

    def cancel_order(self, order_id):
        self._log("cancel_order")
        self.order = {**self.order, "status": "CANCELLED"}
        return {"success": True}

    def reserve(self, slot_id, ddid=None, region=None):
        self._log("reserve")
        self.reserved = slot_id

    def checkout_start(self):
        self._log("checkout_start")

    def checkout_summary(self):
        self._log("summary")
        return {}

    def checkout(self, body, *, order_id):
        assert order_id == str(self.order["orderId"])
        self._log("checkout")
        self.checkout_body = body
        if self.checkout_type == "checkout-result-response":
            self.saved_quantities = dict(self.quantities)
            if hasattr(self, "reserved"):
                self.order = {**self.order, "slotId": self.reserved, "dates": MONDAY}
        if self.checkout_type != "checkout-result-response":
            from app.ocado.payment import PaymentFlowError
            raise PaymentFlowError("The payment form requires a verification code on ocado.com")
        return {"type": self.checkout_type}

    def slots(self, ddid=None, region=None, days=7):
        self._log("slots")
        return ["a slot"]


def test_moving_confirms_the_change_for_this_delivery_only_with_a_saved_card():
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


def test_unavailable_verification_input_reports_the_actual_blocker():
    with pytest.raises(amend.OrderChangeError, match="ocado.com"):
        amend.move_slot(FakeOcado(checkout_type="payment-session-response"), "1629420692934", "mon-1030")


def test_an_order_past_its_cutoff_isnt_touched():
    fake = FakeOcado()
    fake.order["editStatus"] = "NOT_EDITABLE"
    with pytest.raises(amend.OrderChangeError):
        amend.move_slot(fake, "1629420692934", "mon-1030")
    assert fake.calls == []


def test_basket_edits_are_checked_out_and_saved_on_the_order():
    fake = FakeOcado()
    after = amend.change_order(fake, str(ORDER["orderId"]), quantities={"sku": 0, "added": 3})
    assert fake.calls == ["start", "quantities", "checkout_start", "summary", "checkout"]
    assert after["items"] == [{"product": "added", "quantity": 3}]
    assert after["slotId"] == ORDER["slotId"]


def test_combined_basket_and_slot_change_uses_one_checkout():
    fake = FakeOcado()
    after = amend.change_order(fake, str(ORDER["orderId"]), quantities={"sku": 1}, slot_id="mon-1030")
    assert after["slotId"] == "mon-1030"
    assert fake.calls.count("checkout") == 1


def test_failed_basket_checkout_discards_quantities():
    fake = FakeOcado(checkout_type="payment-session-response")
    with pytest.raises(amend.OrderChangeError):
        amend.change_order(fake, str(ORDER["orderId"]), quantities={"sku": 0})
    assert fake.calls[-1] == "cancel"
    assert fake.quantities == {"sku": 2}


def test_default_saved_card_is_used_even_when_order_card_is_different():
    fake = FakeOcado()
    fake.order_payments = lambda _: pytest.fail("Original payment is irrelevant")
    fake.wallet_items = lambda: [
        {"walletItemId": "expired", "paymentMethodType": "CARDS", "expired": True, "defaultWalletItem": True},
        {"walletItemId": "first", "paymentMethodType": "CARDS"},
        {"walletItemId": "default", "paymentMethodType": "CARDS", "defaultWalletItem": True}]
    amend.move_slot(fake, str(ORDER["orderId"]), "mon-1030")
    assert fake.checkout_body["paymentMethod"]["walletItemId"] == "default"


def test_no_usable_saved_card_stops_before_editing():
    fake = FakeOcado()
    fake.wallet_items = lambda: [{"walletItemId": "old", "paymentMethodType": "CARDS", "expired": True}]
    with pytest.raises(amend.OrderChangeError, match="No unexpired saved card"):
        amend.move_slot(fake, str(ORDER["orderId"]), "mon-1030")
    assert fake.calls == []


def test_cancel_verifies_saved_status():
    fake = FakeOcado()
    fake.order["cancelable"] = True
    assert amend.cancel_order(fake, str(ORDER["orderId"]))["status"] == "CANCELLED"
    assert fake.calls == ["cancel_order"]


def test_refused_cancellation_does_not_report_success():
    fake = FakeOcado()
    fake.order["cancelable"] = True
    fake.cancel_order = lambda _: {"success": False, "failureReason": "TOO_LATE"}
    with pytest.raises(amend.OrderChangeError, match="TOO_LATE"):
        amend.cancel_order(fake, str(ORDER["orderId"]))


def test_unverified_basket_is_not_reported_as_success():
    fake = FakeOcado()
    fake.get_order = lambda _: {"slotId": ORDER["slotId"], "items": [{"product": "sku", "quantity": 2}]}
    # Keep the pending-order lookup independent of the decorated-order read.
    fake.orders = lambda pending=False: [dict(ORDER)]
    fake.checkout = lambda body, **kwargs: {"type": "checkout-result-response"}
    with pytest.raises(amend.OrderChangeError, match="differs"):
        amend.change_order(fake, str(ORDER["orderId"]), quantities={"sku": 0})


def test_minimum_basket_failure_discards_edit_before_checkout():
    fake = FakeOcado()
    fake.checkout_summary = lambda: {"activeCheckoutGroup": {"canCheckout": False,
                                                            "checkoutRestrictions": ["NOT_REACHED_THRESHOLD"]}}
    with pytest.raises(amend.OrderChangeError, match="NOT_REACHED_THRESHOLD"):
        amend.change_order(fake, str(ORDER["orderId"]), quantities={"sku": 0})
    assert fake.calls[-1] == "cancel"
    assert "checkout" not in fake.calls


@pytest.fixture
def order_api(factory, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import deps, ocado
    import main

    fake = FakeOcado()
    fake.order["cancelable"] = True
    fake.order["status"] = "FUTURE"
    with factory() as session:
        owner = session.query(User).order_by(User.id).first()
        owner.email = "owner@example.com"
        stranger = User(email="stranger@example.com", name="Stranger")
        session.add(stranger)
        session.commit()
        retailer_accounts.connect(session, owner.id, "ocado", email="owner@shop.example")
        owner_id, stranger_id = owner.id, stranger.id
    monkeypatch.setattr(config, "HOUSEHOLD", {"owner@example.com"})
    monkeypatch.setattr(ocado, "get_shared_session", lambda key: fake)
    monkeypatch.setattr(ocado, "OcadoClient", lambda session: session)

    def db_session():
        with factory() as session:
            yield session

    def current_user():
        with factory() as session:
            return session.get(User, current_user.user_id)

    current_user.user_id = owner_id
    main.app.dependency_overrides[deps.get_session] = db_session
    main.app.dependency_overrides[deps.get_current_user] = current_user
    try:
        yield TestClient(main.app), fake, current_user, stranger_id
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("path,body", [("items", {"items": [{"sku": "sku", "quantity": 0}]}),
                                      ("cancel", {}), ("slot", {"slot_id": "mon-1030"})])
def test_order_writes_require_confirmation_and_account_access(order_api, path, body):
    api, fake, current_user, stranger_id = order_api
    url = f'/api/ocado/orders/{ORDER["orderId"]}/{path}'
    assert api.post(url, json=body).status_code == 400
    assert fake.calls == []
    current_user.user_id = stranger_id
    assert api.post(url, json={**body, "confirm": True}).status_code == 404
    assert fake.calls == []


def test_order_api_confirms_basket_then_cancels_delivery(order_api):
    api, fake, _, _ = order_api
    url = f'/api/ocado/orders/{ORDER["orderId"]}'
    response = api.post(f"{url}/items", json={"confirm": True, "items": [{"sku": "sku", "quantity": 0}]})
    assert response.status_code == 200
    assert fake.saved_quantities["sku"] == 0
    response = api.post(f"{url}/cancel", json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


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


def test_checkout_cannot_report_success_for_another_order():
    fake = FakeOcado()
    fake.checkout = lambda body, **kwargs: {"type": amend.CHECKOUT_DONE, "orderId": "different"}
    with pytest.raises(amend.OrderChangeError, match="different order"):
        amend.move_slot(fake, str(ORDER["orderId"]), "mon-1030")
    assert fake.calls[-1] == "cancel"
