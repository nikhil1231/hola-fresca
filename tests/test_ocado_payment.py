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
        payment._drive(None, [], ['Ocado rejected payment completion (HTTP 422)'])


@pytest.mark.parametrize('kind', ['checkout-form-response', 'payment-session-response'])
def test_both_handoff_types_use_browser_continuation(monkeypatch, kind):
    seen = []
    expected = {'type': payment.DONE, 'orderId': 'o1'}
    monkeypatch.setattr(payment, '_browser_result', lambda client, result: (seen.append(result), expected)[1])
    assert payment.complete(object(), {'type': kind}) == expected
    assert seen == [{'type': kind}]


def test_payment_tokens_are_not_exposed_in_browser_error_messages(monkeypatch):
    def fail(*args):
        raise RuntimeError('https://pspweb.ocado.com/?parameterToken=secret-value')
    monkeypatch.setattr(payment, '_browser_result', fail)
    with pytest.raises(payment.PaymentFlowError) as exc:
        payment.complete(object(), {'type': 'checkout-form-response'})
    assert 'secret-value' not in str(exc.value)
    assert 'RuntimeError' in str(exc.value)


def test_unrecognised_response_is_not_reported_as_authentication_required():
    with pytest.raises(payment.PaymentFlowError, match='unsupported checkout response'):
        payment.complete(None, {'type': 'new-protocol-response'})


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


def test_form_protocol_in_a_real_browser():
    """The PSP can post JSON strings; duplicate events must not validate twice."""
    from playwright.sync_api import sync_playwright
    class Client:
        calls = 0
        def validate_checkout(self, token):
            assert token == 'fixture-token'
            self.calls += 1
            return {'type': payment.DONE, 'orderId': 'fixture-order'}
    client = Client()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        except Exception as exc:
            if 'Executable doesn' in str(exc):
                pytest.skip('Chromium is available in the production image')
            raise
        try:
            page = browser.new_page()
            page.set_default_timeout(1000)
            html = '''<button onclick="parent.postMessage(JSON.stringify({type:'CHECKOUT_RESULT_TOKEN',resultToken:'fixture-token'}),'https://www.ocado.com');parent.postMessage({type:'CHECKOUT_RESULT_TOKEN',resultToken:'fixture-token'},'https://www.ocado.com')">Pay</button>'''
            page.route('https://pspweb.ocado.com/fixture', lambda route: route.fulfill(content_type='text/html', body=html))
            completed, failures = [], []
            payment._open_form(page, client, {'url': 'https://pspweb.ocado.com/fixture'}, completed, failures)
            result = payment._drive(page, completed, failures)
            assert result['orderId'] == 'fixture-order'
            assert client.calls == 1
        finally:
            browser.close()



def test_provider_http_error_is_preserved_without_requesting_card_confirmation():
    completed, failures = [], []
    response = SimpleNamespace(url='https://pspweb.ocado.com/v1/checkout/complete', status=422,
                               json=lambda: {'code': 'INVALID_NONCE'})
    payment._completion_response(response, completed, failures)
    assert completed == []
    assert failures == ['The payment provider rejected payment completion (HTTP 422; code INVALID_NONCE)']


def test_payment_result_from_an_unrelated_frame_is_ignored():
    completed, failures = [], []
    response = SimpleNamespace(url='https://example.com/api/orderpayment/v1/checkout/validate', status=200,
                               json=lambda: {'type': payment.DONE, 'orderId': 'o1'})
    payment._completion_response(response, completed, failures)
    assert completed == failures == []
