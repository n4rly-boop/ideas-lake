"""Пул фазы 1 и ровно один писатель фазы 2 поверх очереди (`lake/queue.py`).

The shape the whole thing exists for:

    POST /fetch ──▶ queue ──┬─▶ fetch pool (N threads)  phase 1, own staging file,
                            │                           the graph is never opened
                            └─▶ writer (exactly 1)      phase 2, drains `staged`
                                                        as they appear

Phase 1 is per-source and parallel-safe: `_one_source` appends its lines under the
staging lock (`run.py:126`) and touches nothing else. Phase 2 is not, and the reason
is §4.5, quoted in `run.phase2`: two sources linked at the same time open two ideas
for one mechanism. So the concurrency here is deliberately lopsided — many fetchers,
one writer — and the writer is what turns a batch into a lake.

Three things guard the "exactly one writer", because one of them alone is not enough:

1. one writer thread per process, started once from the app lifespan;
2. `jobs.exclusive` around the phase-2 stint, so a manual `/ingest/phase2` or an
   `/admin/reindex` cannot run while the writer holds the graph, and vice versa;
3. an OS-level `flock` (`lake/writer_lock.py`), so a second *process* — uvicorn
   `--workers 2`, an overlapping deploy, `python3 -m lake.ingest.run phase2` on the
   host — refuses instead of quietly ingesting alongside the first one. It is taken
   here for the life of the writer AND inside `run.phase2` itself, which is what
   covers the entry points this module never sees.

Guard 3 is the one that catches the mistake nobody notices: with only (1) and (2),
two processes would each hold their own slot, both claims would succeed against the
same queue file, and the lake would grow two ideas for every mechanism.
"""
import os
import threading

from .. import queue, trace, writer_lock
from ..models import FETCH_DIR
from . import jobs

# Bounded by the school's LLM pool, not by our CPU: the 9B pool is 16 slots shared
# with everyone, so a wide fetch pool only lengthens somebody else's queue. Two is a
# calibration knob, not a law — raise it when the pool is idle.
FETCH_WORKERS = int(os.environ.get("LAKE_FETCH_WORKERS", "2"))
# Accepting an unbounded backlog is the polite way to drop it: every job would get a
# `queued` status that nothing reaches for hours.
QUEUE_MAX = int(os.environ.get("LAKE_QUEUE_MAX", "100"))
POLL_S = float(os.environ.get("LAKE_QUEUE_POLL_S", "1.0"))

_stop = threading.Event()
_threads: dict[str, threading.Thread] = {}
_holds_lock = False

# Raised by `start()`. Kept as a name here because this is where a caller meets it.
SecondWriter = writer_lock.SecondWriter


def staging_for(job: dict):
    """One staging file per article, named by its arXiv id (`run.ingest_one`)."""
    return FETCH_DIR / f"{job['args']['arxiv_id']}.jsonl"


def _permanent(exc: BaseException) -> bool:
    """Failures a retry cannot fix, so the job fails once instead of three times.

    An article with no HTML anywhere (`FetchError` after all three paths), one the
    parser found no technique in (`ValueError` out of `stage_one`), an entry nothing
    resolves (`KeyError`): the second attempt re-runs the whole fetch to learn the same
    answer, and the third buries the reason under "attempt 3 of 3, giving up". A canary
    timeout or a 503 from the school's pool is the opposite case and still comes back.
    """
    from ..ingest.fetch import FetchError
    return isinstance(exc, (FetchError, ValueError, KeyError))


def _fail(job: dict, back_to: str, exc: BaseException) -> None:
    """Terminal on a permanent error, back in the queue on a transient one."""
    reason = f"{type(exc).__name__}: {exc or '(no message)'}"
    if _permanent(exc):
        queue.finish(job["id"], "failed", error=f"{reason} (permanent, not retried)")
    else:
        queue.retry(job["id"], back_to, reason)


def _merge_cost(before: dict | None, after: dict) -> dict:
    """Phase 1 + phase 2 into one number per key.

    The writer's counter starts at zero, so on its own it reports what the LINKING
    spent — the fetch, the parse, the generalization and the embedding of the article
    are all in the phase-1 half, and cost per idea is block D's metric (C5, §3.3).
    """
    out = dict(after)
    for key, value in (before or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[key] = round(out.get(key, 0) + value, 1)
    return out


# --------------------------------------------------------------------- one step each

def fetch_step() -> bool:
    """Claim one queued job and run phase 1. True if it took a job at all."""
    job = queue.claim("queued", "phase1")
    if job is None:
        return False
    from ..ingest import run
    try:
        with trace.request(job["id"]) as own:
            staged = run.stage_one({"arxiv_id": job["args"]["arxiv_id"], "type": "paper"},
                                   staging_for(job))
        queue.stage(job["id"], {**staged, "cost": dict(own)})
    except BaseException as exc:
        # BaseException on purpose: nothing above this frame catches anything, and a
        # job left `running` by an escaping KeyboardInterrupt would block its own
        # retry forever while reading as work in progress (`jobs._run` says the same).
        _fail(job, "queued", exc)
    return True


def write_step() -> bool:
    """Claim one staged job and run phase 2 under the ingest slot.

    Returns True if it did work. A busy slot is not a failure and not work: the job
    goes back to `staged` with its attempt refunded, and the caller waits a poll.
    """
    job = queue.claim("staged", "phase2")
    if job is None:
        return False
    from ..ingest import run
    # What phase 1 measured, carried on the row. Handed to `drain_one` so the final
    # report keeps the numbers only phase 1 can know — `leakage` and `theses_dropped` —
    # instead of dropping them and reporting the linking half as the whole article.
    staged = job.get("report") or {}
    try:
        # Same id as the queue row: one piece of work, one id. `/ingest/jobs` merges
        # the two registers by id, and `stats.job_running` then names the id the
        # caller who posted the url is polling.
        with jobs.exclusive("fetch", job["args"], job_id=job["id"]):
            with trace.request(job["id"]) as own:
                report = run.drain_one(staging_for(job), staged)
    except jobs.Busy:
        queue.release(job["id"], "staged")
        return False
    except BaseException as exc:
        _fail(job, "staged", exc)
        return True
    # Phase 2 is done and the graph has the article; only the record is left. Guarded,
    # because a raise from `finish` (a full disk, a read-only mount) would otherwise
    # leave the row `running` with the work already done — a status that says "in
    # progress" forever and comes back from the next restart as a failure.
    #
    # NOT retried as work: `drain_one` has deleted the staging file, so a job put back
    # to `staged` would spend three attempts discovering the file is gone and end
    # `failed` with a message about a missing file instead of the truth. So the truth
    # goes on the row instead. If even that write fails, the queue file itself is dead
    # and nothing in this process can record anything: `_loop` prints it.
    try:
        queue.finish(job["id"], "ok",
                     report={**report, "cost": _merge_cost(staged.get("cost"), own)})
    except BaseException as exc:
        queue.finish(job["id"], "failed",
                     error="phase 2 finished and the graph has this article, but the job "
                           f"record could not be written: {type(exc).__name__}: {exc}")
    return True


# ------------------------------------------------------------------------- the loops

def _loop(step) -> None:
    while not _stop.is_set():
        try:
            did = step()
        except BaseException as exc:                    # never let the loop die quietly
            print(f"lake.api.workers: {step.__name__} raised {type(exc).__name__}: {exc}")
            did = False
        if not did:
            _stop.wait(POLL_S)


def start(*, fetch_workers: int | None = None, writer: bool = True) -> dict:
    """Take the writer lock, recover what the last process left mid-flight, start threads.

    In that order, and the order is the point. `recover()` moves every `running` row
    back to the status it was claimed from, on the assumption that the thread behind it
    died with the previous process. A second process doing that first would requeue the
    jobs the FIRST one is running right now — the same article ingested twice, by two
    processes, one of which is about to be refused the lock it should have asked for
    before touching the queue. Reproduced: both processes claim both rows.

    Only a process that owns the writer recovers at all: without the lock there is no
    way to tell a `running` row of a dead process from one of a live neighbour.
    """
    global _holds_lock
    _stop.clear()
    started = []
    if writer:
        writer_lock.acquire()               # raises SecondWriter before anything moves
        _holds_lock = True
    recovered = queue.recover() if writer else {}
    if writer:
        _threads["writer"] = threading.Thread(target=_loop, args=(write_step,),
                                              name="lake-writer", daemon=True)
        started.append("writer")
    for i in range(FETCH_WORKERS if fetch_workers is None else fetch_workers):
        _threads[f"fetch{i}"] = threading.Thread(target=_loop, args=(fetch_step,),
                                                 name=f"lake-fetch{i}", daemon=True)
        started.append(f"fetch{i}")
    for name in started:
        _threads[name].start()
    return {"threads": started, "recovered": recovered}


def stop(timeout: float = 5.0) -> None:
    """Ask the loops to stop, and keep the writer lock while the writer is still writing.

    A phase-2 stint is minutes; this join is seconds. Releasing the lock on a timeout
    would hand the next process a free writer lock while this one is still linking —
    two writers on one lake, which is the §4.5 violation the lock exists for, produced
    by the shutdown path of the guard itself. The threads are daemons, so process exit
    ends them and the OS releases the flock; a restart inside one process waits.

    Only threads that actually stopped leave `_threads`, because `alive()` is what
    `/healthz` reads: a cleared dict would report "no writer" for a writer that is
    running, and the operator would go looking for a stall that is a live ingest.
    """
    global _holds_lock
    _stop.set()
    for name, thread in list(_threads.items()):
        thread.join(timeout=timeout)
        if not thread.is_alive():
            _threads.pop(name, None)
    if "writer" in _threads:
        print(f"lake.api.workers: the writer is still inside phase 2 after {timeout}s — "
              f"keeping {writer_lock.LOCK_PATH} until it finishes")
        return
    if _holds_lock:
        writer_lock.release()
        _holds_lock = False


def alive() -> dict:
    """Which threads are still up. A dead writer with jobs in `staged` is the one
    failure of this design that is otherwise invisible: the queue keeps accepting,
    every job reads `staged`, and nothing ever reaches the graph. `/healthz` reads
    this so the lie has somewhere to show up."""
    return {name: thread.is_alive() for name, thread in _threads.items()}
