"""A slow, jittered pulse that keeps each Ocado session measured and warm.

Two jobs, one mechanism.

The **feature** is catching a dead session early. The cart push happens near the
week's cutoff, and finding out then that the login expired means finding out when
there is no time left to do anything about it. A check that runs a day ahead
turns that into a message somebody can act on.

The **measurement** is how long a session actually lives. That number decides
whether an interactively-logged-in account is a quarterly chore or a weekly
interruption, and it cannot be read off the logs of an app that is only exercised
when somebody happens to shop — two weeks of journald yielded seven ladder
events, of which exactly one was a full login. Sampling on a timer turns session
lifetime into something observed continuously rather than incidentally.

Deliberately **in-process**, not a systemd timer beside the backup job. The
runtime registry, the httpx cookie jar and the browser profile are all owned by
the server process; a separate process would load its own copy of the jar,
refresh it, and write ``session.json`` underneath the running server, which would
then overwrite it from memory on its next save. One writer, in the process that
owns the state.

The pulse is shaped to look like a person rather than a monitor: roughly daily
rather than hourly, jittered so it never lands on the hour, staggered so several
accounts never fire together, and confined to waking hours. At one or two
requests per account per day that is far below a single real browsing session --
the point is the shape, not the volume.
"""
from __future__ import annotations

import logging
import os
import random
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from app.ocado.session import list_account_runtimes

log = logging.getLogger("holafresca.ocado")

#: Nominal gap between checks for one account.
DEFAULT_INTERVAL_HOURS = 24.0
#: Fraction of the interval to jitter by, either way. A check that is always
#: exactly 24 h after the last one is a signature; this smears it over hours.
JITTER_FRACTION = 0.25
#: Waking hours, local time. A session probe at 04:00 has no human explanation.
DEFAULT_WINDOW = "09:00-21:00"
#: How often the loop wakes to see whether anything is due. Short enough that a
#: shutdown is not left waiting on it, long enough to cost nothing.
TICK_S = 60.0
#: No account is checked until at least this long after start-up.
#:
#: Without it the first check lands at ``now +/- jitter``, which is in the past
#: half the time — so starting the server probed Ocado immediately. That is wrong
#: twice over: it undoes the stagger that keeps accounts apart, and under
#: ``UVICORN_RELOAD`` every saved file restarts the worker, so editing the app
#: turned into a burst of probes. Observed doing exactly that: 33 in four
#: minutes. A floor also means a reload storm produces *no* probes, since each
#: worker is replaced long before its first check comes due.
STARTUP_DELAY = timedelta(minutes=5)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_window(raw: str) -> tuple[dtime, dtime]:
    start_s, _, end_s = raw.partition("-")
    try:
        start = dtime.fromisoformat(start_s.strip())
        end = dtime.fromisoformat(end_s.strip())
    except ValueError:
        log.warning(
            "ocado heartbeat: could not parse window %r, falling back to %s",
            raw,
            DEFAULT_WINDOW,
        )
        return _parse_window(DEFAULT_WINDOW)
    return start, end


@dataclass
class _Slot:
    account_id: str
    due_at: datetime


class Heartbeat:
    """Owns the timer thread and each account's next due time."""

    def __init__(
        self,
        *,
        interval_hours: float | None = None,
        window: str | None = None,
        rng: random.Random | None = None,
    ):
        self.interval = timedelta(
            hours=interval_hours
            if interval_hours is not None
            else float(
                os.environ.get("HOLAFRESCA_OCADO_HEARTBEAT_HOURS", DEFAULT_INTERVAL_HOURS)
            )
        )
        self.window_start, self.window_end = _parse_window(
            window
            if window is not None
            else os.environ.get("HOLAFRESCA_OCADO_HEARTBEAT_WINDOW", DEFAULT_WINDOW)
        )
        self._rng = rng or random.Random()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._slots: list[_Slot] = []

    # -- scheduling --------------------------------------------------------

    def _jitter(self) -> timedelta:
        span = self.interval.total_seconds() * JITTER_FRACTION
        return timedelta(seconds=self._rng.uniform(-span, span))

    def _in_window(self, when: datetime) -> bool:
        moment = when.time()
        if self.window_start <= self.window_end:
            return self.window_start <= moment <= self.window_end
        # A window that wraps midnight ("21:00-06:00").
        return moment >= self.window_start or moment <= self.window_end

    def _next_window_start(self, after: datetime) -> datetime:
        candidate = after.replace(
            hour=self.window_start.hour,
            minute=self.window_start.minute,
            second=0,
            microsecond=0,
        )
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    def _schedule(
        self,
        base: datetime,
        *,
        spread: timedelta = timedelta(),
        not_before: datetime | None = None,
    ) -> datetime:
        """When this account should next be checked.

        Pushed into the waking window if it lands outside, then jittered again so
        several accounts deferred to the same window start do not all fire at it.

        ``not_before`` floors the result. The recurring case does not need it —
        the interval dwarfs the jitter, so ``now + interval +/- jitter`` is always
        ahead — but the *first* schedule is relative to now, where negative
        jitter would put the check in the past and fire it instantly.
        """
        due = base + spread + self._jitter()
        if not self._in_window(due):
            due = self._next_window_start(due) + timedelta(
                seconds=abs(self._jitter().total_seconds())
            )
        if not_before is not None and due < not_before:
            return not_before
        return due

    def _plan(self, now: datetime) -> list[_Slot]:
        """First due time per account, spread across one interval.

        Never sooner than :data:`STARTUP_DELAY`, so starting or reloading the
        server is not itself a reason to talk to Ocado.
        """
        runtimes = list_account_runtimes()
        if not runtimes:
            return []
        step = self.interval / len(runtimes)
        floor = now + STARTUP_DELAY
        return [
            _Slot(
                account_id=runtime.account.id,
                due_at=self._schedule(now, spread=step * position, not_before=floor),
            )
            for position, runtime in enumerate(runtimes)
        ]

    # -- running -----------------------------------------------------------

    def check_account(self, account_id: str) -> None:
        """One quiet check. Never escalates past the silent refresh.

        No credentials are supplied here, which is the whole safety story: rung
        3 emails a one-time code, and a background timer must never reach it.
        """
        from app.ocado.session import get_account_runtime

        runtime = get_account_runtime(account_id)
        try:
            state = runtime.auth.ensure_authenticated(runtime.session, trigger="heartbeat")
        except Exception:  # noqa: BLE001 - a failed check is data, not an outage
            log.warning(
                "ocado heartbeat: check failed for %s", account_id, exc_info=True
            )
            return
        log.info("ocado heartbeat: %s -> %s", account_id, state)

    def _run(self) -> None:
        now = datetime.now()
        self._slots = self._plan(now)
        if not self._slots:
            log.info("ocado heartbeat: no accounts configured, nothing to do")
            return
        log.info(
            "ocado heartbeat: watching %d account(s), every ~%.0fh within %s-%s",
            len(self._slots),
            self.interval.total_seconds() / 3600,
            self.window_start.isoformat(timespec="minutes"),
            self.window_end.isoformat(timespec="minutes"),
        )
        while not self._stop.is_set():
            now = datetime.now()
            for slot in self._slots:
                if self._stop.is_set():
                    return
                if slot.due_at > now:
                    continue
                self.check_account(slot.account_id)
                slot.due_at = self._schedule(datetime.now() + self.interval)
            self._stop.wait(TICK_S)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ocado-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None


_HEARTBEAT: Heartbeat | None = None


def start() -> Heartbeat | None:
    """Start the process-wide heartbeat, unless it is switched off.

    Off by default: a heartbeat that starts itself in every process that imports
    the app would run in the scraper CLIs and in the test suite, and the whole
    point of the thing is that it talks to Ocado.
    """
    global _HEARTBEAT
    if not _env_flag("HOLAFRESCA_OCADO_HEARTBEAT", False):
        log.info("ocado heartbeat: disabled (set HOLAFRESCA_OCADO_HEARTBEAT=1)")
        return None
    if _HEARTBEAT is not None:
        return _HEARTBEAT
    _HEARTBEAT = Heartbeat()
    _HEARTBEAT.start()
    return _HEARTBEAT


def stop() -> None:
    global _HEARTBEAT
    if _HEARTBEAT is not None:
        _HEARTBEAT.stop()
        _HEARTBEAT = None
