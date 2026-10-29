"""Payment handoffs are continued, and unavailable inputs are reported precisely."""
from types import SimpleNamespace

import pytest

from app.ocado import payment


class Elements:
    def __init__(self, values=()):
        self.values = list(values)

    def all(self):
        return self.values

    def count(self):
        return len(self.values)

    @property
    def first(self):
        return self.values[0]


class Control:
    def __init__(self, text='', attrs=None, value='', checked=False, click=None):
        self.text, self.attrs, self.value, self.checked = text, attrs or {}, value, checked
        self.on_click = click or (lambda: None)

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def is_checked(self):
        return self.checked

    def check(self):
        self.checked = True
        self.on_click()

    def click(self):
        self.on_click()

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.text


class Frame:
    url = 'https://pspweb.ocado.com/payment'

    def __init__(self):
        self.inputs, self.buttons, self.radios, self.cards, self.checkboxes = [], [], [], [], []

    def locator(self, selector):
        if selector == 'body':
            return Control(text='checkout')
        if selector == 'input:visible':
            return Elements(self.inputs)
        return Elements(self.cards if selector == '.braintree-payment-method' else [])

    def get_by_role(self, role, name=None):
        values = {'button': self.buttons, 'radio': self.radios, 'checkbox': self.checkboxes}[role]
        return Elements([v for v in values if name is None or name.search(v.text)])


def test_saved_card_selection_and_pay_button_complete_without_human_confirmation(monkeypatch):
    frame = Frame()
    completed = []
    actions = []
    def select():
        actions.append('select saved card')
        frame.buttons = [Control('Pay now', click=pay)]
    def pay():
        actions.append('confirm payment')
        completed.append({'type': payment.DONE, 'orderId': 'o1'})
    frame.cards = [Control(click=select)]
    page = SimpleNamespace(frames=[frame], wait_for_timeout=lambda _: None)
    out = payment._drive(page, completed, [])
    assert out['orderId'] == 'o1'
    assert actions == ['select saved card', 'confirm payment']


@pytest.mark.parametrize('field', ['cvv', 'one-time-code', 'password', 'card-number'])
def test_unknown_credentials_are_a_real_blocker(field):
    frame = Frame()
    frame.inputs = [Control(attrs={'name': field, 'required': ''})]
    page = SimpleNamespace(frames=[frame])
    with pytest.raises(payment.PaymentFlowError, match='not available'):
        payment._drive(page, [], [])


def test_frictionless_flow_needs_no_control_clicks():
    page = SimpleNamespace(frames=[], wait_for_timeout=lambda _: None)
    expected = {'type': payment.DONE, 'orderId': 'o1'}
    assert payment._drive(page, [expected], []) == expected


def test_provider_error_is_not_relabelled_as_a_card_challenge():
    with pytest.raises(payment.PaymentFlowError, match='HTTP 422'):
        payment._drive(None, [], [(False, 0.0, 'Ocado rejected payment completion (HTTP 422)')])


def test_a_provider_error_waits_for_the_merchant_result(monkeypatch):
    # The live 404 from the provider's /checkout/complete: keep going, and let
    # Ocado's own validated checkout result decide.
    import time as _time
    completed, failures = [], [(True, _time.monotonic(), 'The payment provider rejected payment completion (HTTP 404)')]
    expected = {'type': payment.DONE, 'orderId': 'o1'}
    page = SimpleNamespace(frames=[], wait_for_timeout=lambda _: completed.append(expected))
    monkeypatch.setattr(payment, '_advance', lambda *_: False)
    assert payment._drive(page, completed, failures) == expected


def test_a_provider_error_fails_once_its_grace_runs_out(monkeypatch):
    monkeypatch.setattr(payment, '_advance', lambda *_: False)
    page = SimpleNamespace(frames=[], wait_for_timeout=lambda _: None)
    stale = [(True, -1e9, 'The payment provider rejected payment completion (HTTP 404)')]
    with pytest.raises(payment.PaymentFlowError, match='HTTP 404'):
        payment._drive(page, [], stale)


def test_payment_tokens_are_not_exposed_in_browser_error_messages(monkeypatch):
    def fail(*args):
        raise RuntimeError('https://pspweb.ocado.com/?parameterToken=secret-value')
    monkeypatch.setattr(payment, '_browser_checkout', fail)
    with pytest.raises(payment.PaymentFlowError) as exc:
        payment.checkout(object(), {}, 'o1')
    assert 'secret-value' not in str(exc.value)
    assert 'RuntimeError' in str(exc.value)


def test_cookie_import_obeys_chromium_security_prefixes():
    jar = [SimpleNamespace(name='__Secure-has-sid', value='private', domain='sso.ocado.com', path='/', secure=False),
           SimpleNamespace(name='__Host-session', value='private', domain='www.ocado.com', path='/', secure=False),
           SimpleNamespace(name='unrelated', value='private', domain='example.com', path='/', secure=False)]
    client = SimpleNamespace(session=SimpleNamespace(client=SimpleNamespace(cookies=SimpleNamespace(jar=jar))))
    values = payment._cookies(client)
    assert len(values) == 2
    assert all(value['secure'] for value in values)
    assert values[1]['url'] == 'https://www.ocado.com/'
    assert 'domain' not in values[1]


def test_optional_empty_security_code_does_not_prevent_submitting_saved_card():
    frame = Frame()
    frame.inputs = [Control(attrs={'name': 'cvv'})]
    completed = []
    frame.buttons = [Control('Pay', click=lambda: completed.append({'type': payment.DONE, 'orderId': 'o1'}))]
    page = SimpleNamespace(frames=[frame], wait_for_timeout=lambda _: None)
    assert payment._drive(page, completed, [])['orderId'] == 'o1'


def test_native_checkout_bootstraps_merchant_then_completes_provider_in_real_browser():
    """A real merchant page must own the iframe, callback and validation request."""
    from playwright.sync_api import sync_playwright
    calls = []
    client = SimpleNamespace(checkout_summary=lambda: {'orderId': 'fixture-order'})
    body = {'paymentMethod': {'walletItemId': 'available-card'},
            'recurringOrder': {'edit': {'replaceItems': False}}}
    merchant = """<script>
        document.cookie = 'nativeCheckoutReady=1; path=/; Secure';
        async function checkout() {
            const result = await (await fetch('/api/orderpayment/v2/checkout', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({paymentMethod:{walletItemId:'old-card'},
                    redirectUrl:'https://www.ocado.com/native-return',
                    recurringOrder:{edit:{replaceItems:true}}})})).json();
            const frame = document.createElement('iframe');frame.src=result.url;document.body.appendChild(frame);
        }
        window.addEventListener('message', async e => {
            if(e.origin==='https://pspweb.ocado.com') await fetch('/api/orderpayment/v1/checkout/validate', {
                method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(e.data)});
        });
    </script><button onclick='checkout()'>Place order</button>"""
    provider = """<button onclick="parent.postMessage({resultToken:'fixture-token'},'https://www.ocado.com')">Submit</button>"""
    def serve(route):
        path = route.request.url.split('ocado.com')[-1]
        if path == '/checkout/summary':
            route.fulfill(content_type='text/html', body=merchant)
        elif path == '/api/orderpayment/v2/checkout':
            assert 'nativeCheckoutReady=1' in route.request.headers.get('cookie', '')
            value = route.request.post_data_json
            assert value['paymentMethod'] == body['paymentMethod']
            assert value['recurringOrder'] == body['recurringOrder']
            assert value['redirectUrl'] == 'https://www.ocado.com/native-return'
            calls.append('checkout')
            route.fulfill(json={'type':'checkout-form-response','url':'https://pspweb.ocado.com/fixture'})
        elif path == '/fixture':
            route.fulfill(content_type='text/html', body=provider)
        elif path == '/api/orderpayment/v1/checkout/validate':
            assert route.request.post_data_json == {'resultToken':'fixture-token'}
            calls.append('validate')
            route.fulfill(json={'type':payment.DONE,'orderId':'fixture-order'})
        else:
            route.abort()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        except Exception as exc:
            if 'Executable doesn' in str(exc):
                pytest.skip('Chromium is available in the production image')
            raise
        try:
            context = browser.new_context()
            context.route('**/*', serve)
            page = context.new_page()
            result = payment._native_checkout(page, client, body, 'fixture-order')
            assert result['orderId'] == 'fixture-order'
            assert calls == ['checkout','validate']
        finally:
            browser.close()


@pytest.mark.parametrize('wrong_order,submitted', [(True, []), (False, [True])])
def test_native_checkout_blocks_a_different_order_and_duplicate_submission(wrong_order, submitted):
    aborted, failures = [], []
    route = SimpleNamespace(request=SimpleNamespace(method='POST'), abort=lambda: aborted.append(True),
                            fallback=lambda **kw: pytest.fail('Must not submit checkout'))
    client = SimpleNamespace(checkout_summary=lambda: {'orderId':'different' if wrong_order else 'o1'})
    payment._checkout_request(route, client, {}, 'o1', submitted, failures)
    assert aborted == [True] and failures


def test_provider_http_error_is_preserved_without_requesting_card_confirmation():
    completed, failures = [], []
    response = SimpleNamespace(url='https://pspweb.ocado.com/v1/checkout/complete', status=422,
                               json=lambda: {'code': 'INVALID_NONCE'})
    payment._completion_response(response, completed, failures)
    assert completed == []
    assert [m for _p, _t, m in failures] == ['The payment provider rejected payment completion (HTTP 422; code INVALID_NONCE)']


def test_payment_result_from_an_unrelated_frame_is_ignored():
    completed, failures = [], []
    response = SimpleNamespace(url='https://example.com/api/orderpayment/v1/checkout/validate', status=200,
                               json=lambda: {'type': payment.DONE, 'orderId': 'o1'})
    payment._completion_response(response, completed, failures)
    assert completed == failures == []
