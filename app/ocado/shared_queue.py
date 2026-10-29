"""Durable, globally debounced shared shopping batches.

All upstream work holds :func:`shared.account_lock`, including crash recovery.
SQLite compare-and-swap updates also protect the enqueue/claim boundary: a worker
only receives the exact generation it claimed after the last three-minute quiet
period. A process dying after its claim leaves a visible, non-replayable failed
batch when the next lock owner recovers it.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OcadoSharedQueue, RetailerAccount
from app.ocado import shared

log = logging.getLogger(__name__)
DEBOUNCE = timedelta(seconds=180)
ACTIVE = {"pending", "running", "failed"}
NOT_SAVED = ("Ocado didn't accept these changes ({reason}). We checked your order and nothing was changed, "
             "so your changes are kept here: try again, or keep editing and they'll be sent together.")
UNCERTAIN = ("The server stopped while this batch was running. Its changes or payment may have completed. "
             "Check the delivery on ocado.com, then cancel this queue before intentionally submitting anything again.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value is not None else None


def empty(target_id: str = "cart") -> dict:
    return {"target_id": target_id, "due_at": None, "status": "idle", "actions": [], "error": None, "updated_at": None}


def _out(row: OcadoSharedQueue) -> dict:
    return {"target_id": row.target_id, "due_at": _iso(row.due_at), "status": row.status,
            "actions": json.loads(row.actions_json) if row.status in ACTIVE else [],
            "error": row.error, "updated_at": _iso(row.updated_at)}


class QueueStore:
    """Short database transactions only; upstream calls never hold a DB lock."""

    def __init__(self, factory: sessionmaker[Session]):
        self.factory = factory

    @staticmethod
    def _row(db: Session) -> OcadoSharedQueue | None:
        return db.scalar(select(OcadoSharedQueue).join(
            RetailerAccount, OcadoSharedQueue.account_id == RetailerAccount.id
        ).where(RetailerAccount.key == shared.account_key(db), RetailerAccount.retailer == "ocado"))

    @staticmethod
    def _cas(db: Session, row: OcadoSharedQueue, **values) -> None:
        result = db.execute(update(OcadoSharedQueue).where(
            OcadoSharedQueue.id == row.id,
            OcadoSharedQueue.version == row.version,
            OcadoSharedQueue.status == row.status,
        ).values(version=row.version + 1, **values).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.rollback()
            raise shared.SharedError("The shared queue changed; refresh before trying again")
        db.commit()

    def snapshot(self) -> dict:
        with self.factory() as db:
            row = self._row(db)
            return _out(row) if row is not None else empty()

    def remember_target(self, target_id: str) -> dict:
        """Cache the read-selected target for cheap queue polling; no account creation."""
        with self.factory() as db:
            row = self._row(db)
            if row is not None:
                if row.status not in ACTIVE and row.target_id != target_id:
                    self._cas(db, row, target_id=target_id)
                    db.refresh(row)
                return _out(row)
            account = shared.require_account(db)
            row = OcadoSharedQueue(account_id=account.id, user_id=account.user_id, target_id=target_id)
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise shared.SharedError("The shared queue changed; refresh before trying again") from exc
            return _out(row)

    def enqueue(self, target_id: str, current_target: str, action: dict, user_id: int,
                *, now: datetime | None = None, quantities: dict[str, int] | None = None,
                slot_id: str | None = None) -> dict:
        return self.enqueue_many(target_id, current_target, [action], user_id, now=now,
                                 quantities=quantities, slot_id=slot_id)

    def enqueue_many(self, target_id: str, current_target: str, additions: list[dict], user_id: int,
                     *, now: datetime | None = None, quantities: dict[str, int] | None = None,
                     slot_id: str | None = None) -> dict:
        """Commit a reviewed offer selection together, with one shared timer."""
        now = now or utcnow()
        with self.factory() as db:
            account = shared.require_account(db)
            row = self._row(db)
            if row is not None and row.status == "running":
                raise shared.SharedError("The batch is running; wait before making another change")
            if row is not None and row.status == "failed":
                raise shared.SharedError("Inspect the failed batch and cancel it before intentionally submitting changes again")
            expected = row.target_id if row is not None and row.status == "pending" else current_target
            if target_id != expected or current_target != expected:
                raise shared.SharedError("This delivery target has changed; refresh before making another change")
            actions = json.loads(row.actions_json) if row is not None and row.status == "pending" else []
            # Absolute quantities and slot/checkout singletons: the most recent
            # person owns the retained action and extends the one global timer.
            for action in additions:
                previous = next((old for old in actions if old["kind"] == action["kind"] and
                                 (action["kind"] != "quantity" or old["sku"] == action["sku"])), None)
                action = dict(action)
                if action["kind"] == "quantity":
                    original = (previous or {}).get("original_quantity")
                    if original is None and quantities is not None:
                        original = quantities.get(action["sku"], 0)
                    if original is not None:
                        action["original_quantity"] = original
                elif action["kind"] == "slot":
                    original = (previous or {}).get("original_slot_id", slot_id)
                    if original is not None:
                        action["original_slot_id"] = original
                actions = [old for old in actions if not (
                    old["kind"] == action["kind"] and
                    (action["kind"] != "quantity" or old["sku"] == action["sku"])
                )]
                # Reversing an edit returns to the confirmed shop. There is
                # nothing to display or send, including after a server restart.
                if action["kind"] == "quantity" and action.get("original_quantity") == action["quantity"]:
                    continue
                if action["kind"] == "slot" and action.get("original_slot_id") == action["slot_id"]:
                    continue
                actions.append({**action, "id": uuid.uuid4().hex, "user_id": user_id})
            values = dict(target_id=target_id, status="pending" if actions else "idle", actions_json=json.dumps(actions),
                          due_at=now + DEBOUNCE if actions else None, updated_at=now, error=None)
            if row is None:
                row = OcadoSharedQueue(account_id=account.id, user_id=account.user_id, **values)
                db.add(row)
                try:
                    db.commit()
                except IntegrityError as exc:
                    db.rollback()
                    raise shared.SharedError("The shared queue changed; refresh before trying again") from exc
            else:
                self._cas(db, row, **values)
                db.refresh(row)
            return _out(row)

    def cancel(self, target_id: str | None = None, *, now: datetime | None = None) -> dict:
        with self.factory() as db:
            row = self._row(db)
            if row is None:
                return empty()
            if row.status == "running":
                raise shared.SharedError("An executing batch cannot be cancelled; wait for its result")
            if target_id is not None and row.target_id != target_id:
                raise shared.SharedError("The queue target changed; refresh before cancelling")
            self._cas(db, row, status="idle", actions_json="[]", due_at=None, error=None,
                      updated_at=now or utcnow())
            db.refresh(row)
            return _out(row)

    def recover_orphaned(self, *, now: datetime | None = None) -> bool:
        """Caller MUST hold the cross-process account lock, proving no executor lives."""
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.status != "running":
                return False
            self._cas(db, row, status="failed", due_at=None, error=UNCERTAIN, updated_at=now or utcnow())
            return True

    def claim(self, *, now: datetime | None = None) -> dict | None:
        now = now or utcnow()
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.status != "pending" or row.due_at is None or _aware(row.due_at) > now:
                return None
            batch = _out(row)
            # Include the due boundary in SQL too: no lost enqueue may bring an
            # old timer forward, even if this caller read just before it changed.
            result = db.execute(update(OcadoSharedQueue).where(
                OcadoSharedQueue.id == row.id, OcadoSharedQueue.version == row.version,
                OcadoSharedQueue.status == "pending", OcadoSharedQueue.due_at <= now,
            ).values(status="running", due_at=None, updated_at=now, version=row.version + 1)
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return {**batch, "version": row.version + 1}

    def settle(self, version: int, reason: str, outcome: str, *, now: datetime | None = None) -> None:
        """Record a failed run by what Ocado shows afterwards, not by the exception alone.

        Applied: it landed despite the error, so it succeeded. Unchanged: nothing
        landed, so the batch goes back to an editable pending state with no timer —
        it is resubmitted only when someone retries or edits again, never by itself.
        Anything else stays ``failed`` for a person to check on ocado.com."""
        if outcome == shared.APPLIED:
            self.finish(version, None, now=now)
            return
        if outcome != shared.UNCHANGED:
            self.finish(version, f"{reason}. Changes or checkout may be incomplete; check Ocado before cancelling "
                                 "and intentionally submitting another batch.", now=now)
            return
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.version != version or row.status != "running":
                raise shared.SharedError("The claimed batch changed while it was executing")
            self._cas(db, row, status="pending", due_at=None, error=NOT_SAVED.format(reason=reason.rstrip(".")),
                      updated_at=now or utcnow())

    def recheck(self, outcome_for: Callable[[str, list[dict]], str], *, now: datetime | None = None) -> dict:
        """Re-read Ocado for a failed batch. Caller MUST hold the account lock."""
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.status != "failed":
                return _out(row) if row is not None else empty()
            outcome = outcome_for(row.target_id, json.loads(row.actions_json))
            if outcome == shared.APPLIED:
                self._cas(db, row, status="succeeded", actions_json="[]", error=None, due_at=None,
                          updated_at=now or utcnow())
            elif outcome == shared.UNCHANGED:
                self._cas(db, row, status="pending", due_at=None, updated_at=now or utcnow(),
                          error=NOT_SAVED.format(reason="the last attempt failed"))
            else:
                raise shared.SharedError("Ocado's order doesn't match either the old or the requested basket; "
                                         "check it on ocado.com, then clear these changes")
            db.refresh(row)
            return _out(row)

    def retry(self, target_id: str | None = None, *, now: datetime | None = None) -> dict:
        """Send a held (unsent, verified-unchanged) batch now."""
        now = now or utcnow()
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.status != "pending" or not json.loads(row.actions_json):
                raise shared.SharedError("There are no held changes to send")
            if target_id is not None and row.target_id != target_id:
                raise shared.SharedError("The queue target changed; refresh before retrying")
            self._cas(db, row, due_at=now, error=None, updated_at=now)
            db.refresh(row)
            return _out(row)

    def finish(self, version: int, error: str | None, *, now: datetime | None = None) -> None:
        with self.factory() as db:
            row = self._row(db)
            if row is None or row.version != version or row.status != "running":
                raise shared.SharedError("The claimed batch changed while it was executing")
            values = dict(status="failed" if error else "succeeded", error=error,
                          due_at=None, updated_at=now or utcnow())
            if error is None:
                values["actions_json"] = "[]"
            self._cas(db, row, **values)


class Worker:
    def __init__(self, factory: sessionmaker[Session], *, client_factory: Callable = shared.make_client):
        self.store = QueueStore(factory)
        self.client_factory = client_factory
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def step(self, *, now: datetime | None = None) -> bool:
        try:
            with shared.account_lock(blocking=False), self.store.factory() as session, shared.using_account(session):
                if self.store.recover_orphaned(now=now):
                    return True
                batch = self.store.claim(now=now)
                if batch is None:
                    return False
                client = None
                try:
                    client = self.client_factory(shared.account_key())
                    shared.execute(client, batch["target_id"], batch["actions"])
                except Exception as exc:  # upstream failures are retained, never automatically retried
                    log.warning("Shared Ocado batch failed: %s", exc, exc_info=True)
                    outcome = shared.verify(client, batch["target_id"], batch["actions"]) \
                        if client is not None else shared.UNKNOWN
                    log.info("Shared Ocado batch read back as %s", outcome)
                    self.store.settle(batch["version"], str(exc).rstrip(".") or type(exc).__name__, outcome, now=now)
                else:
                    self.store.finish(batch["version"], None, now=now)
                return True
        except shared.SharedError:
            # Another reader/executor owns the lock, or a CAS lost. Never start
            # upstream work on the strength of a stale snapshot.
            return False

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.step()
            except Exception:  # keep the durable claim for explicit recovery on the next iteration
                log.exception("Shared Ocado queue worker failed")
            self.stopping.wait(1)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="ocado-shared-queue", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            # Do not release the execution lock or close the runtime underneath
            # a live checkout. A killed process instead follows orphan recovery.
            self.thread.join()
            self.thread = None


_worker: Worker | None = None
_lifecycle_lock = threading.Lock()


def start(factory: sessionmaker[Session]) -> None:
    global _worker
    with _lifecycle_lock:
        if _worker is None:
            _worker = Worker(factory)
            _worker.start()


def stop() -> None:
    global _worker
    with _lifecycle_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None
