"""Extend the review queue with the next ingredients from the frequency list.

The batch scrape only covered the top slice of ``ingredient_frequency.csv``. This
module walks the rest on demand — "load 10 more" — doing for each ingredient what
the original pipeline did in bulk: search Ocado, cache the candidates, and run the
LLM first pass.

Three outcomes per ingredient:

* **pantry line** — HelloFresh's non-shipped entries ("Water for the Sauce",
  "Olive Oil for the Dressing"). Filed straight as an approved pantry staple with
  no products; no Ocado search and no LLM call, because searching for them is
  meaningless and they only clog the review queue.
* **candidates found** — cached, then handed to the LLM for a proposal.
* **nothing found** — recorded as ``no_match`` with no products. It still appears
  in the list so the reviewer can reach it and fix the wording by hand.

Work is persisted per ingredient as it goes, so an interrupted run simply resumes
where it left off the next time the button is pressed.

The two halves run at different widths, because they are limited by different
things. **Searching is serial** — every retailer call goes through the one
:class:`~app.mapping.live_search.LiveSearchRunner` thread behind a shared
backoff, so a shop is asked one thing at a time and a rate-limit signal still
means something. **Proposing is not**: the LLM calls are independent of each
other, so they run on a small pool while the search thread moves on to the next
ingredient. The run is therefore roughly as long as its LLM leg rather than the
sum of the two, and the retailer sees exactly the traffic it saw before.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import IngredientMapping, ProductSearchHit
from app.mapping import service
from app.mapping.candidates import gather_candidates, load_usage_stats
from app.mapping.openai_client import Completer
from app.mapping.propose import propose_one
from app.scraper.products.worklist import load_worklist
from app.retailers import DEFAULT_RETAILER

if TYPE_CHECKING:
    from app.mapping import live_search

log = logging.getLogger("holafresca.mapping")

#: Default shop; every public entry point takes ``retailer`` and falls back to it.
RETAILER = DEFAULT_RETAILER

#: Proposals in flight behind the serial search. Deliberately small: the ceiling
#: here is SQLite taking one writer at a time, not the model's rate limit, and
#: the writes this pool makes interleave with the search thread's own.
DEFAULT_LLM_WORKERS = 4

# HelloFresh names its assumed-owned lines "<thing> for the <component>" ("Water
# for the Sauce", "Sugar for the Pickle"), plus a few bare cupboard staples.
_PANTRY_PHRASE = re.compile(r"\bfor the\b", re.I)
_PANTRY_NAMES = {
    "water", "salt", "pepper", "sugar", "olive oil", "oil", "vegetable oil",
    "salt and pepper", "cold water", "boiling water",
}


def is_pantry_line(name: str) -> bool:
    """True for ingredients the cook is assumed to already have."""
    cleaned = name.strip().lower()
    return bool(_PANTRY_PHRASE.search(cleaned)) or cleaned in _PANTRY_NAMES


@dataclass
class GenerateJob:
    job_id: str
    total: int = 0
    processed: int = 0
    added: int = 0
    staples: int = 0
    no_match: int = 0
    errors: int = 0
    status: str = "running"  # running | done | failed
    error: str | None = None
    #: The ingredient being *searched*. The proposal pool trails behind it, so
    #: this is the head of the pipeline rather than the last thing finished, and
    #: it clears while the final proposals drain.
    current: str | None = None
    #: The tallies are written by the search thread and the proposal pool, and
    #: read by whichever request thread is polling the job.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def tally(self, *, added: int = 0, staples: int = 0, no_match: int = 0, errors: int = 0) -> int:
        """Record one finished ingredient; returns how many are done."""
        with self._lock:
            self.added += added
            self.staples += staples
            self.no_match += no_match
            self.errors += errors
            self.processed += 1
            return self.processed

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "processed": self.processed,
                "total": self.total,
                "added": self.added,
                "staples": self.staples,
                "no_match": self.no_match,
                "errors": self.errors,
                "error": self.error,
                "current": self.current,
            }


class _JobRegistry:
    """In-memory job tracking; one generate run at a time.

    Deliberately not persisted: every ingredient is committed as it completes, so
    losing a job record costs nothing more than pressing the button again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, GenerateJob] = {}
        self._active: str | None = None

    def active(self) -> GenerateJob | None:
        with self._lock:
            return self._jobs.get(self._active) if self._active else None

    def get(self, job_id: str) -> GenerateJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self) -> GenerateJob:
        with self._lock:
            if self._active and self._jobs[self._active].status == "running":
                raise RuntimeError("a generate job is already running")
            job = GenerateJob(job_id=uuid.uuid4().hex[:12])
            self._jobs[job.job_id] = job
            self._active = job.job_id
            return job


REGISTRY = _JobRegistry()


def pending_worklist(
    session: Session,
    *,
    count: int,
    csv_path: Path | None = None,
    retailer: str = RETAILER,
) -> list[tuple[int, str, str, int]]:
    """The next ``count`` ingredients with no cached candidates, richest first.

    Returns ``(rank, ingredient_key, name, line_count)``.
    """
    covered = {
        row[0]
        for row in session.execute(
            select(ProductSearchHit.ingredient_key).where(ProductSearchHit.retailer == retailer)
        )
    }
    mapped = service.existing_mapping_keys(session, retailer)
    skip = covered | mapped

    out: list[tuple[int, str, str, int]] = []
    # ``limit=None`` gives the whole frequency list, already ranked.
    for item in load_worklist(csv_path, limit=None):
        if item.ingredient_key in skip:
            continue
        out.append((item.rank, item.ingredient_key, item.name, item.line_count))
        if len(out) >= count:
            break
    return out


def _file_pantry_staple(session: Session, key: str, name: str, line_count: int, retailer: str) -> None:
    mapping = IngredientMapping(
        retailer=retailer,
        ingredient_key=key,
        name=name,
        line_count=line_count,
        status="approved",
        pantry_staple=1,
        decided_by="auto",
        llm_notes="Assumed already in the cupboard; not shopped for.",
    )
    session.add(mapping)
    session.commit()


def _file_no_match(session: Session, key: str, name: str, line_count: int, retailer: str) -> None:
    mapping = IngredientMapping(
        retailer=retailer,
        ingredient_key=key,
        name=name,
        line_count=line_count,
        status="no_match",
        decided_by="auto",
        llm_notes="Ocado returned no products for this name — try rewording the search.",
    )
    session.add(mapping)
    session.commit()


def _file_needs_review(
    session: Session, key: str, name: str, line_count: int, reason: str, retailer: str
) -> None:
    """Record an ingredient whose candidates cached but whose proposal failed."""
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer, IngredientMapping.ingredient_key == key
        )
    )
    if mapping is None:
        mapping = IngredientMapping(retailer=retailer, ingredient_key=key)
        session.add(mapping)
    mapping.name = name
    mapping.line_count = line_count
    mapping.status = "needs_review"
    mapping.decided_by = "auto"
    mapping.llm_notes = f"Candidates cached but the proposal step failed: {reason}"
    session.commit()


def generate(
    session_factory: sessionmaker[Session],
    *,
    count: int = 10,
    job: GenerateJob | None = None,
    complete: Completer | None = None,
    runner: live_search.LiveSearchRunner | None = None,
    csv_path: Path | None = None,
    model: str | None = None,
    retailer: str = RETAILER,
    llm_workers: int = DEFAULT_LLM_WORKERS,
) -> GenerateJob:
    """Bring ``count`` more ingredients into ``retailer``'s review queue.

    Mappings are per-retailer rows, so this is run once per shop: the same
    ingredient needs its own proposal against each catalogue, and an approval at
    one shop says nothing about the other.

    Searching stays on this thread, one ingredient at a time; each proposal is
    handed to a pool of ``llm_workers``. Note that ``llm_workers=1`` narrows the
    pool to one proposal at a time but does not restore the old fully serial
    run: the search loop still moves on without waiting for it.
    """
    job = job or GenerateJob(job_id=uuid.uuid4().hex[:12])

    # Imported here, not at module scope: live_search pulls in the whole
    # Playwright/browser stack, and read-only callers of this module — the
    # /mapping/stats endpoint only wants pending_worklist — should not pay for a
    # browser they never open.
    from app.mapping import live_search

    # Built on first use, not up front: a batch of nothing but pantry lines needs
    # no LLM at all, and a missing API key should cost only the ingredients that
    # genuinely need a proposal rather than aborting the whole run.
    state: dict = {"client": complete, "model": model or "test"}
    # Held while the client is built so a pool that starts several proposals at
    # once builds one client, not one each — and so every worker reads the model
    # name the client settled on.
    client_lock = threading.Lock()

    def completer() -> Completer:
        with client_lock:
            if state["client"] is None:
                from app.mapping.openai_client import OpenAIJSONClient

                client = OpenAIJSONClient(model=model)
                state["client"] = client
                state["model"] = client.model
            return state["client"]

    usage_by_key = load_usage_stats(csv_path)

    with session_factory() as session:
        work = pending_worklist(
            session, count=count, csv_path=csv_path, retailer=retailer
        )
    job.total = len(work)
    log.info("generate: %d ingredients to add for %s", job.total, retailer)

    def propose_and_write(key: str, name: str, line_count: int, found: int) -> None:
        """The LLM leg of one ingredient, run on the pool.

        Reports its own outcome rather than raising: a future nobody waits on
        would swallow the exception, and one bad ingredient must not stop the
        rest of the run.
        """
        try:
            with session_factory() as session:
                ic = gather_candidates(
                    session, key, name=name, usage=usage_by_key.get(key), retailer=retailer,
                )
                try:
                    proposed = propose_one(ic, completer())
                    service.write_proposal(
                        session, ic, proposed, model=state["model"], retailer=retailer,
                    )
                except Exception as exc:  # noqa: BLE001
                    # The search already cached candidates, so this key now looks
                    # "covered" and would never be revisited. Record it as needing
                    # review rather than orphaning cached candidates behind no
                    # mapping row at all.
                    _file_needs_review(session, key, name, line_count, str(exc), retailer)
                    raise
        except Exception as exc:  # noqa: BLE001
            done = job.tally(errors=1)
            log.warning("[%d/%d] %s -> ERROR: %s", done, job.total, name, exc)
        else:
            done = job.tally(added=1)
            log.info(
                "[%d/%d] %s -> %d candidates, %d accepted",
                done, job.total, name, found, len(proposed.accepted),
            )

    # The search thread is this one; the proposals run behind it. Progress is
    # logged in completion order, which is no longer worklist order.
    with ThreadPoolExecutor(
        max_workers=max(1, llm_workers), thread_name_prefix="propose"
    ) as pool:
        for rank, key, name, line_count in work:
            job.current = name
            try:
                if is_pantry_line(name):
                    with session_factory() as session:
                        _file_pantry_staple(session, key, name, line_count, retailer)
                    done = job.tally(staples=1)
                    log.info("[%d/%d] %s -> pantry staple", done, job.total, name)
                    continue
                with session_factory() as session:
                    found = live_search.search_and_store(
                        session, key, name, runner=runner,
                        term_rank=rank, line_count=line_count, retailer=retailer,
                    )
                if not found:
                    with session_factory() as session:
                        _file_no_match(session, key, name, line_count, retailer)
                    done = job.tally(no_match=1)
                    log.info("[%d/%d] %s -> no candidates", done, job.total, name)
                    continue
            except Exception as exc:  # noqa: BLE001 - one bad search must not abort the run
                done = job.tally(errors=1)
                log.warning("[%d/%d] %s -> ERROR: %s", done, job.total, name, exc)
                continue
            # Hand the LLM leg over and go back to searching. ``job.processed``
            # is incremented by the worker, so the count still means finished.
            pool.submit(propose_and_write, key, name, line_count, found)
        # Nothing is left to search, but proposals are still in flight; leaving
        # the block waits for them.
        job.current = None

    # New mapping rows arrive with no unit classification, and only the recipe
    # library can supply one. The planner reads that classification rather than
    # deriving it per request, so the batch that created the rows has to leave
    # them decided.
    if job.added or job.staples:
        from app.planner.index import derive_count_metadata

        derive_count_metadata(session_factory, csv_path=csv_path, retailer=retailer)
    job.status = "done"
    log.info(
        "generate done: %d proposed, %d staples, %d no-match, %d errors",
        job.added, job.staples, job.no_match, job.errors,
    )
    return job


def start_background(
    session_factory: sessionmaker[Session],
    *,
    count: int = 10,
    retailer: str = RETAILER,
    llm_workers: int = DEFAULT_LLM_WORKERS,
) -> GenerateJob:
    """Kick off :func:`generate` on a worker thread and return the job handle."""
    job = REGISTRY.start()

    def run() -> None:
        try:
            generate(
                session_factory, count=count, job=job, retailer=retailer,
                llm_workers=llm_workers,
            )
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            log.exception("generate job failed")

    threading.Thread(target=run, name=f"generate-{job.job_id}", daemon=True).start()
    return job
