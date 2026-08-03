"""Reading Ocado's one-time login codes out of an app-controlled mailbox.

Ocado emails the code, so unattended re-auth needs somewhere to read mail from.
That somewhere is a dedicated Gmail account, read over IMAP with an app-specific
password, which the real Ocado addresses forward their Ocado mail into.

Three decisions are worth knowing about:

* The app mailbox is deliberately *not* the address registered with Ocado. A
  password reset goes to the registered address, so a mailbox holding nothing
  but five-minute codes is worth very little if its app password ever leaks.
* A message only counts if it landed *after* the login attempt started. Every
  Ocado code mail looks the same, so without that check the first unattended
  login happily submits yesterday's expired code and burns an attempt.
* Both Ocado accounts forward into one mailbox, so a message has to be tied back
  to an account before its code is used. Matching is on markers - the account's
  own email address, and the plus-addressed destination it forwards to - looked
  for across the whole raw message, because a redirect preserves the original
  ``To`` while an inline forward buries it in the body instead.
"""
from __future__ import annotations

import email
import html
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from email.message import Message
from typing import Callable, Iterator

log = logging.getLogger("holafresca.ocado")

#: Cushion on the IMAP ``SINCE`` search, which only has day granularity and is
#: evaluated in the server's timezone. The precise cut is made on INTERNALDATE.
SEARCH_CUSHION_S = 86400.0
#: Newest-first cap on how many messages one poll will open.
SCAN_LIMIT = 25


@dataclass(frozen=True, slots=True)
class MailboxConfig:
    host: str
    port: int
    user: str
    password: str
    folder: str = "INBOX"


@dataclass(frozen=True, slots=True)
class OtpQuery:
    """What makes a message the code we are waiting for.

    ``markers`` identifies the Ocado account. Empty means "any Ocado code mail
    will do", which is right for a mailbox serving a single account and wrong
    for one serving two.
    """

    markers: tuple[str, ...] = ()
    sender_contains: str = "ocado"


# Ordered most specific first: the phrasing around the number is what separates a
# login code from an order number sitting elsewhere in the same email. The gap is
# wide because it cannot cross a digit anyway - it captures the first number
# after the anchor, so the width only decides how far away that number may sit.
# Ocado's real mail puts 34 characters between "one-time code" and the digits.
_DIGITS = r"(\d{4,8})"
_GAP = r"\D{0,120}?"
_CONTEXT_PATTERNS = (
    # "380059 This code is valid for 10 minutes" - the tightest anchor Ocado
    # gives us, and the one that survives a reworded introduction.
    re.compile(_DIGITS + r"\s*(?:This|Your)?\s*code is valid", re.I),
    # "Here's your one-time code to log in to your Ocado account. 380059"
    re.compile(
        r"(?:one[-\s]?time|single[-\s]?use|verification|security|log[-\s]?in|access)\s*"
        r"(?:pass)?code\b" + _GAP + _DIGITS,
        re.I,
    ),
    re.compile(_DIGITS + _GAP + r"\bis your\b", re.I),
    re.compile(r"\b(?:pass)?code\b" + _GAP + _DIGITS, re.I),
)
#: Last resort, and only trusted when the message contains exactly one of them
#: *and* reads like a code mail - see ``extract_code``.
_LONE_SIX = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_OTP_SIGNAL = re.compile(
    r"one[-\s]?time|single[-\s]?use|verification code|security code|passcode|code to log ?in",
    re.I,
)

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def plus_address(user: str, tag: str) -> str:
    """``a@gmail.com`` + ``nikhil`` -> ``a+nikhil@gmail.com``.

    Gmail routes the lot to one inbox but keeps the address it was delivered to,
    which is what lets two Ocado accounts share one mailbox.
    """
    local, _, domain = user.partition("@")
    if not domain:
        return user
    return f"{local}+{tag}@{domain}"


def visible_text(message: Message) -> str:
    """The body as a human would read it, HTML stripped.

    Codes are extracted from this rather than the raw source so a hex colour or
    a tracking URL cannot be mistaken for a six-digit code.
    """
    parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 - a malformed part is not fatal
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            text = _strip_html(text)
        parts.append(text)
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()


def _strip_html(markup: str) -> str:
    without_scripts = _SCRIPT_STYLE.sub(" ", markup)
    return html.unescape(_TAG.sub(" ", without_scripts))


def extract_code(text: str) -> str | None:
    """The login code, or ``None`` when the text does not clearly contain one.

    The bar for the last-resort "one lone six-digit number" rule is deliberately
    high, because the forwarding rule is scoped by sender: order confirmations
    and delivery notices reach this mailbox too, and a wrong code submitted to
    Ocado costs an attempt. So the message has to read like a code mail before a
    bare number counts - and the real mail shows why "one lone number" alone is
    not enough on its own, since Ocado's tracking pixel URL contributes a second.
    """
    for pattern in _CONTEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if not _OTP_SIGNAL.search(text):
        return None
    lone = set(_LONE_SIX.findall(text))
    if len(lone) == 1:
        return lone.pop()
    return None


def _marker_present(haystack: str, marker: str) -> bool:
    """Find ``marker`` in the message, but only as a whole address.

    Plain containment is not enough: one account's address can sit inside
    another's (``shopper@example.com`` inside ``other-shopper@example.com``),
    which would hand the first account a code meant for the second. Guarding
    both edges with the characters addresses are made of keeps the match to a
    whole address while still finding it anywhere - inside ``To:``, inside
    angle brackets, or quoted into a forwarded body.
    """
    pattern = r"(?<![A-Za-z0-9._%+\-])" + re.escape(marker) + r"(?![A-Za-z0-9\-])"
    return re.search(pattern, haystack) is not None


def matches(raw: bytes, message: Message, query: OtpQuery) -> bool:
    """Whether this message is an Ocado code mail for the account we asked about.

    The account check runs against the whole raw message, headers included: a
    server-side redirect keeps the original ``To``, an inline forward quotes the
    original headers into the body, and either is enough to tell the accounts
    apart.
    """
    haystack = raw.decode("utf-8", errors="replace").lower()
    if query.sender_contains and query.sender_contains.lower() not in haystack:
        return False
    if not query.markers:
        return True
    return any(_marker_present(haystack, marker.lower()) for marker in query.markers if marker)


def find_code(
    messages: Iterator[tuple[object, float, bytes]],
    query: OtpQuery,
    *,
    since: float,
) -> tuple[object, str] | None:
    """First message newer than ``since`` that yields a code for this account."""
    for uid, received_at, raw in messages:
        if received_at <= since:
            continue
        message = email.message_from_bytes(raw)
        if not matches(raw, message, query):
            continue
        code = extract_code(visible_text(message))
        if code:
            return uid, code
    return None


class ImapMailbox:
    """One IMAP connection, polled repeatedly for new mail."""

    def __init__(self, config: MailboxConfig):
        self.config = config
        # Google shows app passwords in spaced groups; the spaces are display
        # only, and pasting them through unstripped is the classic way to get an
        # unexplained AUTHENTICATIONFAILED.
        password = re.sub(r"\s+", "", config.password or "")
        self._imap = imaplib.IMAP4_SSL(config.host, config.port)
        self._imap.login(config.user, password)
        self._imap.select(config.folder)

    def refresh(self) -> None:
        """Make mail that arrived since the last poll visible on this session."""
        self._imap.noop()

    def messages_since(self, since: float) -> Iterator[tuple[bytes, float, bytes]]:
        """Newest first, dated then fetched.

        The dates are pulled in one round trip *before* any body, for two
        reasons. Only messages that beat the freshness cut get downloaded, which
        on a mailbox with any history is most of the bytes saved. And asking for
        ``(INTERNALDATE RFC822)`` together does not work: Gmail answers with the
        literal first and the date trailing *after* it, where
        ``Internaldate2tuple`` cannot see it - so every message silently parsed
        as undated and got skipped.
        """
        cutoff = time.strftime("%d-%b-%Y", time.localtime(since - SEARCH_CUSHION_S))
        status, data = self._imap.search(None, "SINCE", cutoff)
        if status != "OK" or not data or not data[0]:
            return
        nums = data[0].split()[-SCAN_LIMIT:]
        if not nums:
            return

        status, dated = self._imap.fetch(b",".join(nums), "(INTERNALDATE)")
        if status != "OK":
            return
        fresh: list[tuple[bytes, float]] = []
        for line in dated:
            if not isinstance(line, bytes):
                continue
            received = imaplib.Internaldate2tuple(line)
            num = line.split(b" ", 1)[0]
            if received is None or num not in nums:
                continue
            received_at = time.mktime(received)
            if received_at > since:
                fresh.append((num, received_at))

        for num, received_at in reversed(fresh):
            status, payload = self._imap.fetch(num, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            yield num, received_at, payload[0][1]

    def mark_seen(self, uid: object) -> None:
        try:
            self._imap.store(uid, "+FLAGS", "\\Seen")
        except Exception:  # noqa: BLE001 - housekeeping, never worth failing over
            pass

    def close(self) -> None:
        for step in (self._imap.close, self._imap.logout):
            try:
                step()
            except Exception:  # noqa: BLE001 - the socket may already be gone
                pass


def fetch_code(
    config: MailboxConfig,
    query: OtpQuery,
    *,
    since: float,
    wait_s: float = 120.0,
    poll_s: float = 4.0,
    opener: Callable[[MailboxConfig], ImapMailbox] = ImapMailbox,
) -> str | None:
    """Poll the mailbox until a code for this account turns up, or time runs out.

    Returns ``None`` rather than raising when nothing arrives: a code that never
    comes is an ordinary outcome that hands back to the manual OTP endpoint.
    Connection and login failures do raise - those are misconfiguration, and
    swallowing them here would make it look like the mail simply never landed.
    """
    mailbox = opener(config)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            mailbox.refresh()
            found = find_code(mailbox.messages_since(since), query, since=since)
            if found is not None:
                uid, code = found
                mailbox.mark_seen(uid)
                return code
            if time.monotonic() + poll_s >= deadline:
                return None
            time.sleep(poll_s)
    finally:
        mailbox.close()


def mailbox_from_env() -> MailboxConfig | None:
    """The configured mailbox, or ``None`` when OTP automation is switched off."""
    from app import config as app_config

    if not app_config.OCADO_OTP_IMAP_USER or not app_config.OCADO_OTP_IMAP_PASSWORD:
        return None
    return MailboxConfig(
        host=app_config.OCADO_OTP_IMAP_HOST,
        port=app_config.OCADO_OTP_IMAP_PORT,
        user=app_config.OCADO_OTP_IMAP_USER,
        password=app_config.OCADO_OTP_IMAP_PASSWORD,
        folder=app_config.OCADO_OTP_IMAP_FOLDER,
    )
