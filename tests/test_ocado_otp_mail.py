"""Reading login codes out of the shared OTP mailbox. No IMAP server involved."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.ocado import otp_mail
from app.ocado.otp_mail import MailboxConfig, OtpQuery, extract_code, find_code, plus_address

#: The two accounts sharing one OTP mailbox. These match the names in the
#: fixture below, which is the same message a real login produces.
MAIN = "shopper@example.com"
OTHER = "other-shopper@example.com"
DESTINATION = "otp-mailbox+main@example.com"

#: A real Ocado code mail, inline-forwarded by an Outlook rule, with the people
#: renamed. Kept verbatim otherwise: the Salesforce tracking pixel and the
#: quoted original headers are both load-bearing, and no synthetic sample would
#: have reproduced either.
REAL_MAIL = Path(__file__).parent / "fixtures" / "ocado" / "otp_email_forwarded.eml"
REAL_CODE = "380059"


def mail(*, to: str = MAIN, body: str = "Your verification code is 481920", html: str = "") -> bytes:
    payload = (
        f"From: Ocado <noreply@ocado.com>\r\n"
        f"To: {to}\r\n"
        f"Subject: Your Ocado login code\r\n"
        f"Content-Type: text/{'html' if html else 'plain'}; charset=utf-8\r\n"
        f"\r\n"
        f"{html or body}\r\n"
    )
    return payload.encode("utf-8")


# -- against the mail Ocado actually sends -------------------------------


def test_the_real_forwarded_mail_yields_its_code():
    raw = REAL_MAIL.read_bytes()

    found = find_code(iter([(b"1", 200.0, raw)]), OtpQuery(markers=(MAIN,)), since=100.0)

    assert found == (b"1", REAL_CODE)


def test_an_inline_forward_is_still_tied_back_to_its_account():
    """Forwarding, unlike redirecting, rewrites To: to the mailbox address.

    What saves it is that Outlook quotes the original headers into the body, so
    the address Ocado sent to is still in the message - and the plus-addressed
    destination identifies the account on its own regardless.
    """
    raw = REAL_MAIL.read_bytes()

    for marker in (MAIN, DESTINATION):
        assert find_code(iter([(b"1", 200.0, raw)]), OtpQuery(markers=(marker,)), since=100.0)
    assert find_code(iter([(b"1", 200.0, raw)]), OtpQuery(markers=(OTHER,)), since=100.0) is None


def test_the_real_mail_carries_a_decoy_six_digit_number():
    """Why a bare "the only six-digit number" rule could never have been enough.

    Ocado's footer has a Salesforce tracking pixel whose URL contains a second
    six-digit run, so the code has to be found by the phrasing around it.
    """
    import email

    text = otp_mail.visible_text(email.message_from_bytes(REAL_MAIL.read_bytes()))
    lone = set(otp_mail._LONE_SIX.findall(text))

    assert len(lone) > 1 and REAL_CODE in lone
    assert extract_code(text) == REAL_CODE


# -- pulling the code out of the text ------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your verification code is 481920", "481920"),
        ("Your one-time passcode: 481920", "481920"),
        ("481920 is your Ocado security code", "481920"),
        ("Enter the code 4819 to continue", "4819"),
        ("Nothing numeric to see here", None),
    ],
)
def test_the_code_is_read_from_the_phrasing_around_it(text, expected):
    assert extract_code(text) == expected


def test_an_order_number_next_to_the_code_does_not_win():
    """The reason extraction is anchored on phrasing rather than "first digits".

    Ocado's mail carries order and reference numbers, and a naive scan picks up
    whichever happens to be laid out first.
    """
    text = "Order 100248871 update. Your verification code is 481920. Total 42.50"

    assert extract_code(text) == "481920"


def test_two_bare_six_digit_numbers_are_refused_rather_than_guessed():
    """Better to hand back to the human than to submit a coin flip."""
    assert extract_code("Reference 998877 and 481920 both appear") is None


def test_ordinary_ocado_mail_yields_no_code_at_all():
    """The forwarding rule is scoped by sender, not by subject.

    So receipts and delivery notices land in this mailbox too, and one of them
    arriving mid-login must not be mistaken for the code - a wrong guess costs a
    login attempt. A lone six-digit number is only trusted in a message that
    reads like a code mail, and this one does not.
    """
    receipt = "Your Ocado order 452118 is on its way. Delivery Tuesday, 10 items, 84.20 total."

    assert extract_code(receipt) is None


def test_html_mail_is_read_as_a_human_would_see_it():
    """Codes come from the visible text, so markup cannot supply the digits."""
    message = _parsed(
        mail(
            html=(
                "<style>.c{color:#481920}</style>"
                "<p>Your verification code is <b>735104</b></p>"
            )
        )
    )

    assert extract_code(otp_mail.visible_text(message)) == "735104"


# -- telling the two accounts apart --------------------------------------


def test_a_code_is_only_used_for_the_account_it_was_sent_to():
    """Both accounts forward into one mailbox, so this is what stops a crossover.

    Without it the second account's login grabs the first account's code, which
    Ocado rejects - and the failure looks like a broken extractor.

    These two addresses are deliberately chosen so one contains the other: a
    marker has to match a whole address, or every message for OTHER would also
    answer for MAIN.
    """
    messages = [(b"1", 200.0, mail(to=OTHER))]

    assert find_code(iter(messages), OtpQuery(markers=(MAIN,)), since=100.0) is None
    assert find_code(iter(messages), OtpQuery(markers=(OTHER,)), since=100.0) == (b"1", "481920")


def test_a_forwarded_copy_is_matched_on_its_plus_address():
    """An inline forward rewrites To:, so the destination address is the fallback."""
    raw = mail(to="otp-mailbox+main@example.com")
    markers = (MAIN, plus_address("otp-mailbox@example.com", "main"))

    assert find_code(iter([(b"1", 200.0, raw)]), OtpQuery(markers=markers), since=100.0)


def test_a_mailbox_serving_one_account_needs_no_marker():
    assert find_code(iter([(b"1", 200.0, mail())]), OtpQuery(), since=100.0) == (b"1", "481920")


def test_unrelated_mail_is_ignored():
    raw = b"From: bank@example.com\r\nTo: x@y.z\r\n\r\nYour verification code is 481920\r\n"

    assert find_code(iter([(b"1", 200.0, raw)]), OtpQuery(markers=()), since=100.0) is None


# -- freshness ------------------------------------------------------------


def test_a_code_that_predates_the_login_attempt_is_never_used():
    """The bug this exists to prevent.

    Every Ocado code mail looks identical, so an unattended login without this
    check submits the last attempt's expired code, gets rejected, and burns a
    retry on a mailbox that is working perfectly.
    """
    stale = (b"1", 90.0, mail(body="Your verification code is 111111"))
    fresh = (b"2", 150.0, mail(body="Your verification code is 222222"))

    assert find_code(iter([stale, fresh]), OtpQuery(), since=100.0) == (b"2", "222222")


def test_only_stale_mail_means_no_code():
    stale = [(b"1", 90.0, mail())]

    assert find_code(iter(stale), OtpQuery(), since=100.0) is None


# -- the polling loop -----------------------------------------------------


class FakeMailbox:
    """Delivers its mail on the Nth poll, so the wait loop can be exercised."""

    def __init__(self, arrives_on_poll=1, raw=None):
        self.arrives_on_poll = arrives_on_poll
        self.raw = raw if raw is not None else mail()
        self.polls = 0
        self.seen: list[bytes] = []
        self.closed = False

    def refresh(self):
        self.polls += 1

    def messages_since(self, since):
        if self.polls >= self.arrives_on_poll:
            yield b"1", since + 10.0, self.raw

    def mark_seen(self, uid):
        self.seen.append(uid)

    def close(self):
        self.closed = True


def _fetch(mailbox, **kwargs):
    config = MailboxConfig(host="h", port=993, user="u@gmail.com", password="p")
    kwargs.setdefault("since", 100.0)
    kwargs.setdefault("wait_s", 1.0)
    kwargs.setdefault("poll_s", 0.01)
    return otp_mail.fetch_code(config, OtpQuery(), opener=lambda _: mailbox, **kwargs)


def test_the_loop_keeps_polling_until_the_mail_lands():
    mailbox = FakeMailbox(arrives_on_poll=3)

    assert _fetch(mailbox) == "481920"
    assert mailbox.polls == 3
    assert mailbox.seen == [b"1"], "a used code should not look unread"
    assert mailbox.closed


def test_a_code_that_never_arrives_returns_none_rather_than_raising():
    """A silent mailbox is an ordinary outcome: the manual endpoint takes over."""
    mailbox = FakeMailbox(arrives_on_poll=9999)

    assert _fetch(mailbox) is None
    assert mailbox.closed


def test_the_wait_is_bounded():
    started = time.monotonic()

    assert _fetch(FakeMailbox(arrives_on_poll=9999), wait_s=0.2, poll_s=0.05) is None
    assert time.monotonic() - started < 2.0


def test_the_connection_is_closed_even_when_a_poll_blows_up():
    mailbox = FakeMailbox()
    mailbox.refresh = _boom

    with pytest.raises(RuntimeError):
        _fetch(mailbox)
    assert mailbox.closed


def _boom():
    raise RuntimeError("imap fell over")


def _parsed(raw: bytes):
    import email

    return email.message_from_bytes(raw)
