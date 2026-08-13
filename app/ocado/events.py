"""Where auth-ladder outcomes go.

A registration point rather than a direct write, for two reasons. The ladder is
constructed by :mod:`app.ocado.session` at import time, long before anything has
decided which database this process is talking to — and in tests, often no
database at all. And the writer needs the session factory the API is already
using: opening a second engine against the same SQLite file would work, but it
would also mean two connection pools writing the same journal for no reason.

So the sink starts unset and nothing is recorded. :func:`app.api.deps` installs
one at startup. A test that wants the events asserts on them by installing its
own; every other test stays silent without having to know this module exists.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OcadoAuthEvent
from app.ocado.auth import LadderEvent

log = logging.getLogger("holafresca.ocado")

_sink: Callable[[LadderEvent], None] | None = None


def set_sink(sink: Callable[[LadderEvent], None] | None) -> None:
    """Install (or clear) the process-wide sink. Idempotent."""
    global _sink
    _sink = sink


def record(event: LadderEvent) -> None:
    """Hand an event to the installed sink, if any. Never raises."""
    sink = _sink
    if sink is None:
        return
    sink(event)


def db_sink(
    get_factory: Callable[[], sessionmaker[Session]]
) -> Callable[[LadderEvent], None]:
    """A sink that appends to ``ocado_auth_events``.

    Owns its session rather than joining a request's: a heartbeat tick has no
    request to borrow one from, and a rung recorded mid-login must not ride on
    the transaction of whatever else that request was doing.

    Takes a *callable* rather than a factory, and calls it on the first write
    rather than at install time. Building the factory opens an engine and runs
    the runtime schema check against whatever database this process is
    configured for — which, under the test suite's ``with TestClient(app)``, is
    the real one. No ladder event ever fires in those tests, so nothing is built.
    """

    def write(event: LadderEvent) -> None:
        try:
            with get_factory()() as session:
                session.add(
                    OcadoAuthEvent(
                        account_id=event.account_id,
                        rung=event.rung,
                        outcome=event.outcome,
                        trigger=event.trigger,
                        detail=event.detail,
                        duration_ms=event.duration_ms,
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001 - telemetry must not break a login
            log.warning("ocado auth: could not persist a ladder event", exc_info=True)

    return write
