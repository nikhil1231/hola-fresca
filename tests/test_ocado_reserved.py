"""Reserved rules use explicit order identities and durable once-only claims."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import OcadoReservedRules, OcadoReservedRun, OcadoSharedQueue
from app.ocado import reserved, shared
from tests.test_ocado_shared import (FakeSharedClient, NOW, booked, setup_shared, shared_api, _login, _post)


class ReservedClient(FakeSharedClient):
    def __init__(self):
        super().__init__()
        self.quantities = {}
        self.amount = '50.00'
        self.manual = booked('manual', 2)
        self.reserved = booked('reserved', 3)
        self.reserved['recurringShoppingDefinition'] = {'definitionId': 'weekly'}
        self.coming = [self.manual, self.reserved]
        self.reserved = None  # FakeSharedClient uses this for a slot change
        self.edit_snapshot = None

    def start_order_edit(self, order_id, region):
        super().start_order_edit(order_id, region)
        self.edit_snapshot = dict(self.quantities)
        self.quantities = {str(item['product']): item['quantity'] for item in self.get_order(order_id)['items']}

    def cancel_order_edit(self, order_id, region):
        super().cancel_order_edit(order_id, region)
        self.quantities = self.edit_snapshot or {}

    def checkout(self, body, order_id=None):
        result = super().checkout(body, order_id)
        self.quantities = {}
        return result

    def cart_view(self):
        result = super().cart_view()
        result['totals']['itemPriceAfterPromos']['amount'] = self.amount
        return result


@pytest.fixture
def enabled(setup_shared):
    factory, people, _ = setup_shared
    with factory() as db:
        account = shared.require_account(db)
        db.add(OcadoReservedRules(account_id=account.id, user_id=people[0], enabled=1,
                                 rules_json=json.dumps([{'sku': 'milk', 'name': 'Milk', 'quantity': 1},
                                                        {'sku': 'absent', 'name': 'Absent', 'quantity': 0}])))
        db.commit()
    return factory, ReservedClient()


def run(factory, fake):
    return reserved.step(factory, client_factory=lambda key: fake, now=NOW)


def records(factory):
    with factory() as db:
        return [(row.order_id, row.status, row.warning) for row in db.scalars(select(OcadoReservedRun))]


def test_once_only_targets_reserved_and_preserves_later_manual_edits(enabled):
    factory, fake = enabled
    assert run(factory, fake)
    assert fake.get_order('manual')['items'][0]['quantity'] == 2
    assert fake.get_order('reserved')['items'] == [{'product': 'milk', 'quantity': 1}]
    assert fake.checkout_body['recurringOrder'] == {'edit': {'replaceItems': False}}
    fake.coming[1]['items'][0]['quantity'] = 7
    with factory() as db:
        db.scalar(select(OcadoReservedRules)).rules_json = '[{"sku":"milk","quantity":3}]'
        db.commit()
    assert not run(factory, fake)
    assert fake.get_order('reserved')['items'][0]['quantity'] == 7
    assert fake.calls.count('checkout') == 1
    assert records(factory) == [('reserved', 'completed', None)]
    future = booked('future', 5)
    future['recurringShoppingDefinition'] = {'definitionId': 'weekly'}
    fake.coming.append(future)
    assert run(factory, fake)
    assert fake.get_order('future')['items'][0]['quantity'] == 3


def test_already_matching_including_absent_zero_is_recorded_without_edit(enabled):
    factory, fake = enabled
    fake.coming[1]['items'][0]['quantity'] = 1
    assert run(factory, fake)
    assert records(factory) == [('reserved', 'completed', None)]
    assert 'start_edit' not in fake.calls
    fake.coming[1]['items'][0]['quantity'] = 9
    assert not run(factory, fake)


@pytest.mark.parametrize('quantity', [0, 1, 4])
def test_absolute_targets_include_additions_reductions_and_removal(enabled, quantity):
    factory, fake = enabled
    with factory() as db:
        db.scalar(select(OcadoReservedRules)).rules_json = json.dumps([{'sku': 'milk', 'quantity': quantity}])
        db.commit()
    assert run(factory, fake)
    assert fake.get_order('reserved')['items'] == ([{'product': 'milk', 'quantity': quantity}] if quantity else [])


def test_manual_basket_and_pending_human_edits_defer_without_claiming(enabled):
    factory, fake = enabled
    fake.quantities = {'bread': 3}
    assert not run(factory, fake)
    assert fake.quantities == {'bread': 3} and not records(factory)
    fake.quantities = {}
    with factory() as db:
        account = shared.require_account(db)
        db.add(OcadoSharedQueue(account_id=account.id, user_id=account.user_id, target_id='reserved', status='pending'))
        db.commit()
    assert not run(factory, fake)
    assert not records(factory) and 'start_edit' not in fake.calls


@pytest.mark.parametrize('amount,restrictions', [('39.99', []), ('50.00', ['NOT_REACHED_THRESHOLD'])])
def test_minimum_warning_is_persistent_and_edit_is_discarded(enabled, amount, restrictions):
    factory, fake = enabled
    fake.amount, fake.restrictions = amount, restrictions
    assert run(factory, fake)
    order_id, status, warning = records(factory)[0]
    assert (order_id, status) == ('reserved', 'warning')
    assert '£40.00 minimum' in warning
    assert fake.get_order('reserved')['items'][0]['quantity'] == 2
    assert 'cancel_edit' in fake.calls and 'checkout' not in fake.calls
    assert not run(factory, fake)


def test_exact_minimum_is_allowed(enabled):
    factory, fake = enabled
    fake.amount = '40.00'
    assert run(factory, fake)
    assert records(factory)[0][1] == 'completed'


def test_closed_manual_and_unknown_recurring_orders_never_touched(enabled):
    factory, fake = enabled
    fake.coming[1]['editStatus'] = 'NOT_EDITABLE'
    assert not run(factory, fake)
    fake.coming[1]['editStatus'] = 'EDITABLE'
    fake.coming[1]['recurringShoppingDefinition'] = {}
    assert not run(factory, fake)
    fake.coming[1]['recurringShoppingDefinition'] = {'name': 'Unverified recurring order'}
    assert not run(factory, fake)
    fake.coming[1]['recurringShoppingDefinition'] = 'maybe'
    assert not run(factory, fake)
    fake.coming[1]['recurringShoppingDefinition'] = {'definitionId': 'weekly'}
    fake.coming[1]['dates']['confirmOrderChangesBy'] = '2029-12-31T23:00:00Z'
    assert not run(factory, fake)
    assert 'start_edit' not in fake.calls


def test_crash_resumes_original_snapshot_not_new_rules(enabled):
    factory, fake = enabled
    with factory() as db:
        settings = db.scalar(select(OcadoReservedRules))
        db.add(OcadoReservedRun(account_id=settings.account_id, user_id=settings.user_id, order_id='reserved',
                               status='running', rules_json=settings.rules_json, updated_at=NOW))
        settings.rules_json = '[{"sku":"milk","quantity":5}]'
        db.commit()
    assert run(factory, fake)
    assert fake.get_order('reserved')['items'][0]['quantity'] == 1
    assert records(factory)[0][1] == 'completed'


def test_disabled_settings_and_other_account_do_not_run(enabled):
    factory, fake = enabled
    with factory() as db:
        db.scalar(select(OcadoReservedRules)).enabled = 0
        db.commit()
    assert not run(factory, fake)
    assert not fake.calls


def test_database_rejects_duplicate_order_claims(enabled):
    factory, fake = enabled
    assert run(factory, fake)
    with factory() as db:
        row = db.scalar(select(OcadoReservedRun))
        db.add(OcadoReservedRun(account_id=row.account_id, user_id=row.user_id, order_id=row.order_id,
                               status='running', rules_json='[]', updated_at=NOW))
        with pytest.raises(IntegrityError):
            db.commit()


def test_api_rules_validate_quantities_scope_and_return_persistent_warnings(shared_api):
    api, fake, _, current, people = shared_api
    _login(api)
    path = '/api/ocado-shared/reserved'
    assert api.get(path).json()['enabled'] is False
    rules = [{'sku': 'milk', 'name': 'Milk', 'quantity': 0}]
    response = _post(api, path, json={'enabled': True, 'rules': rules})
    assert response.status_code == 200 and response.json()['rules'] == rules
    for qty in [-1, 1.5, True, 51]:
        assert _post(api, path, json={'enabled': True, 'rules': [{**rules[0], 'quantity': qty}]}).status_code == 422
    assert _post(api, path, json={'enabled': True, 'rules': rules * 2}).status_code == 400
    assert _post(api, path, json={'enabled': True, 'rules': []}).status_code == 400
    assert api.post(path, json={'account_id': 999, 'account_version': 0, 'enabled': True, 'rules': rules}).status_code == 409
    # Other authenticated household members see the same shared rules.
    current[0] = people[1]
    assert api.get(path).json()['rules'] == rules


def test_existing_database_gets_reserved_tables_through_migration(setup_shared):
    from sqlalchemy import text
    from app.db.session import init_db

    factory, people, _ = setup_shared
    engine = factory.kw['bind']
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE ocado_reserved_runs'))
        connection.execute(text('DROP TABLE ocado_reserved_rules'))
        connection.execute(text("UPDATE alembic_version SET version_num='0020_hellofresh_week_decisions'"))
    init_db(engine)
    with factory() as db:
        account = shared.require_account(db)
        db.add(OcadoReservedRules(account_id=account.id, user_id=people[0], enabled=1,
                                 rules_json='[{"sku":"milk","quantity":1}]'))
        db.commit()
    fake = ReservedClient()
    assert run(factory, fake)
    assert records(factory)[0][1] == 'completed'


def test_warning_surfaces_in_status_and_reserved_history(shared_api, factory):
    api, fake, _, _, _ = shared_api
    _login(api)
    with factory() as db:
        account = shared.require_account(db)
        db.add(OcadoReservedRun(account_id=account.id, user_id=account.user_id, order_id='reserved',
                               status='warning', rules_json='[]', updated_at=NOW,
                               warning='Adjusted basket is below £40.00 minimum'))
        db.commit()
    assert '£40.00' in api.get('/api/ocado-shared/status').json()['reserved_warnings'][0]['warning']
    assert api.get('/api/ocado-shared/reserved').json()['runs'][0]['status'] == 'warning'


def test_other_account_rules_never_used(enabled, monkeypatch):
    from app import config
    factory, fake = enabled
    monkeypatch.setattr(config, 'OCADO_SHARED_ACCOUNT_KEY', 'some-other-account')
    assert not run(factory, fake)
    assert not fake.calls


def test_unruled_products_are_preserved_and_new_product_is_added(enabled):
    factory, fake = enabled
    with factory() as db:
        db.scalar(select(OcadoReservedRules)).rules_json = '[{"sku":"bananas","quantity":2}]'
        db.commit()
    assert run(factory, fake)
    assert fake.get_order('reserved')['items'] == [{'product': 'milk', 'quantity': 2}, {'product': 'bananas', 'quantity': 2}]


def test_crash_after_checkout_does_not_checkout_matching_order_again(enabled):
    factory, fake = enabled
    fake.coming[1]['items'][0]['quantity'] = 1
    with factory() as db:
        settings = db.scalar(select(OcadoReservedRules))
        db.add(OcadoReservedRun(account_id=settings.account_id, user_id=settings.user_id, order_id='reserved',
                               status='running', rules_json=settings.rules_json, updated_at=NOW))
        db.commit()
    assert run(factory, fake)
    assert records(factory)[0][1] == 'completed'
    assert 'checkout' not in fake.calls and 'start_edit' not in fake.calls


def test_account_lock_prevents_two_reserved_executors(enabled):
    factory, fake = enabled
    with shared.account_lock():
        assert not run(factory, fake)
    assert not records(factory) and not fake.calls


@pytest.mark.parametrize('cart_order_id,should_finish', [('reserved', True), ('manual-cart', False)])
def test_interrupted_run_discards_only_its_own_edit_session(enabled, cart_order_id, should_finish):
    factory, fake = enabled
    with factory() as db:
        settings = db.scalar(select(OcadoReservedRules))
        db.add(OcadoReservedRun(account_id=settings.account_id, user_id=settings.user_id, order_id='reserved',
                               status='running', rules_json=settings.rules_json, updated_at=NOW))
        db.commit()
    fake.quantities = {'bread': 7}
    fake.editing = cart_order_id
    original_view = fake.cart_view

    def cart_view():
        cart = original_view()
        cart['activeCheckoutGroup']['orderId'] = fake.editing
        return cart

    fake.cart_view = cart_view
    assert run(factory, fake) is should_finish
    if should_finish:
        assert fake.calls.count('cancel_edit') == 1
        assert fake.get_order('reserved')['items'] == [{'product': 'milk', 'quantity': 1}]
        assert records(factory)[0][1] == 'completed'
    else:
        assert 'cancel_edit' not in fake.calls and 'start_edit' not in fake.calls
        assert fake.quantities == {'bread': 7}
        assert records(factory)[0][1] == 'running'
