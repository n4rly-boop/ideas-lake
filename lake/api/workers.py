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
import json
import os
import re
import threading
from pathlib import Path

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
    """One staging file per job, named by what makes it unique: the arXiv id for a
    `fetch` job (`run.ingest_one`), the `run_id` for a `run` job (`13` §2.5 §9 item
    11 — its own staging file, or it would steal the corpus cursor)."""
    if job["kind"] == "run":
        from ..models import RUN_DIR
        return RUN_DIR / f"{job['args']['run_id']}.jsonl"
    return FETCH_DIR / f"{job['args']['arxiv_id']}.jsonl"


# `RunRequest.run_id`'s own pattern (`schemas.py:367`), repeated here rather than
# imported: this is the SECOND of the two layers that must reject the same shapes
# (see `payload_for` below), and it has to work with no `RunRequest` in scope at
# all — a future non-HTTP caller (a script, a CLI push) never instantiates one.
# Keep the two patterns identical if either changes.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def payload_for(run_id: str):
    """Where `POST /run` parks the batch body before the job exists — too big for
    the `args` column (`13` build note 2). Same directory as the staging file, a
    different suffix so the two never collide. Shared with `routes.submit_run`,
    which is the only writer; `fetch_step`, once `queue.stage()` has committed, is
    the only deleter (see `_cleanup_run` below).

    `run_id` reaches here from `RunRequest.run_id`, which is already a strict slug
    pattern at the door (`schemas.py`) — but this helper is also the one a future
    non-HTTP caller (a script, a CLI push) would call directly, with no request
    model standing guard. So the check is repeated here, against the SLUG ITSELF
    (MINOR, second round), not only against the resolved path: `is_relative_to`
    alone passed `run_id="a/b"` straight through — it never leaves `RUN_DIR`, so
    the path check saw nothing wrong, but it silently created a subdirectory
    (`a/`) that no other `run_id` may ever collide into, which the door's pattern
    (no `/` in the charset) was written to make impossible. `_RUN_ID_RE` rejects
    that, an empty string, a leading dot (`.hidden`, `..`) and anything past 64
    chars — exactly what the door already refuses, so a caller who reaches this
    helper directly gets the same refusal an HTTP request would have gotten.

    Checked AFTER the resolved-path guard, not instead of it: `"../../pwned"`
    fails both, and the path guard's message (below) is the one that actually
    says where it would have landed — worth keeping for anyone chasing a
    traversal attempt. `_RUN_ID_RE` is what catches the shapes that stay inside
    `RUN_DIR` and so slip past a path check with nothing to complain about.
    """
    from ..models import RUN_DIR
    root = RUN_DIR.resolve()
    path = (RUN_DIR / f"{run_id}.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"run_id {run_id!r} would write outside RUN_DIR: {path}")
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id {run_id!r} is not a valid slug "
                         f"({_RUN_ID_RE.pattern}) — refusing to build a path from it")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
    """Terminal on a permanent error, back in the queue on a transient one.

    Either way the job can end up `failed` — a permanent error fails it on this
    attempt, a transient one that has now spent `queue.MAX_ATTEMPTS` fails it
    inside `queue.retry` itself — and `failed` is terminal: no later attempt will
    ever read this job's side-channel payload again (MINOR, second round). The
    first round moved the payload's deletion to AFTER `queue.stage()` commits so a
    transient failure keeps it for the retry that needs it, but `_CLEANUP` was
    only ever consulted from that success path — a `run` job that fails
    permanently on attempt 1 (`_permanent` -> `queue.finish(..., "failed", ...)`
    right here) left its multi-megabyte payload on disk forever, because nothing
    else was ever going to look at it again either. Checked off the row `queue`
    actually returns, not off which branch ran: `queue.retry` decides internally
    whether `MAX_ATTEMPTS` was hit, and that decision is the only place that
    knows.
    """
    reason = f"{type(exc).__name__}: {exc or '(no message)'}"
    if _permanent(exc):
        queue.finish(job["id"], "failed", error=f"{reason} (permanent, not retried)")
        final_status = "failed"
    else:
        row = queue.retry(job["id"], back_to, reason)
        final_status = row["status"] if row else None
    if final_status == "failed":
        cleanup = _CLEANUP.get(job["kind"])
        if cleanup is not None:
            cleanup(job)


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

def _stage_fetch(job: dict) -> dict:
    from ..ingest import run
    return run.stage_one({"arxiv_id": job["args"]["arxiv_id"], "type": "paper"},
                         staging_for(job))


def _stage_run(job: dict) -> dict:
    """Phase 1 of a `run` job: the converter turns the batch into staging lines.

    The payload file is only READ here, never written or deleted — `routes.submit_run`
    wrote it before the job existed, and `fetch_step` deletes it (`_cleanup_run`,
    below) only once `queue.stage()` has actually COMMITTED the row to `staged`.

    Deleting it here, before that commit, cost a transient failure of `stage()` (a
    momentarily locked `jobs.db`) a retry that could not possibly work: `_fail` puts
    the job back to `queued` on anything that is not `_permanent`, and attempt 2
    would find no payload to read, burning all `MAX_ATTEMPTS` discovering a file this
    same process had already removed on attempt 1's success. Reproduced with a probe
    that fails `queue.stage()` once (`api/selfcheck.py`).
    """
    from ..ingest import runlog
    path = Path(job["args"]["payload"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    # `limit`/`min_abs_delta` ride inside the payload itself (`RunRequest`'s own
    # fields, `schemas.py`), not only in `job["args"]` — reading them off the
    # PAYLOAD, not the queue row, is what still works for a future non-HTTP
    # caller of `runlog.from_payload` directly, with no `RunRequest`/queue row in
    # front of it at all. Dropping this forwarding leaves every /run job
    # converting everything, silently: the two filters (§2.4) would sit on no
    # path a real request can reach, even though the schema and the CLI both
    # still advertise them.
    return runlog.from_payload(payload, staging_for(job),
                               limit=payload.get("limit"),
                               min_abs_delta=payload.get("min_abs_delta", 0.0))


def _cleanup_run(job: dict) -> None:
    """Drop the payload file of a `run` job — called by `fetch_step` only after
    `queue.stage()` has committed, never by `_stage_run` itself (see there)."""
    Path(job["args"]["payload"]).unlink(missing_ok=True)


# Kind -> what to drop once phase 1's `stage()` transition has committed. `fetch`
# has no side-channel file to clean up, so it is simply absent from the table.
_CLEANUP = {"run": _cleanup_run}


def _drain_fetch(job: dict, staged: dict) -> dict:
    from ..ingest import run
    return run.drain_one(staging_for(job), staged)


def _drain_run(job: dict, staged: dict) -> dict:
    from ..ingest import runlog
    return runlog.drain_run(staging_for(job), staged)


# Table, not a chain of ifs bolted onto the arXiv path (`13` build note 4): both
# `fetch_step` and `write_step` claim the oldest row of ANY kind and look here for
# what to do with it, rather than each kind getting its own claimer/thread — the
# pool stays `FETCH_WORKERS` wide and the writer stays exactly one, whichever kind
# is waiting.
_STAGE = {"fetch": _stage_fetch, "run": _stage_run}
_DRAIN = {"fetch": _drain_fetch, "run": _drain_run}


def fetch_step() -> bool:
    """Claim one queued job of any kind and run its phase 1. True if it took one."""
    job = queue.claim("queued", "phase1")
    if job is None:
        return False
    handler = _STAGE.get(job["kind"])
    if handler is None:
        # Not reachable through this API — `JobOut.kind` and `queue.enqueue`'s
        # callers only ever write a kind that is in this table — but a queue row is
        # a string column, not an enum, and a silent skip here is exactly the
        # fail-open shape this project bans: dead letter, not a spin, not a guess.
        _fail(job, "queued", ValueError(f"no phase-1 handler for job kind {job['kind']!r}"))
        return True
    try:
        with trace.request(job["id"]) as own:
            staged = handler(job)
        queue.stage(job["id"], {**staged, "cost": dict(own)})
    except BaseException as exc:
        # BaseException on purpose: nothing above this frame catches anything, and a
        # job left `running` by an escaping KeyboardInterrupt would block its own
        # retry forever while reading as work in progress (`jobs._run` says the same).
        _fail(job, "queued", exc)
        return True
    # Only now, with the `staged` transition actually committed, is it safe to drop
    # whatever side-channel file the handler read (MAJOR 2): dropping it earlier and
    # having `stage()` itself fail would leave a `queued` job with nothing left to
    # retry.
    cleanup = _CLEANUP.get(job["kind"])
    if cleanup is not None:
        cleanup(job)
    return True


def write_step() -> bool:
    """Claim one staged job of any kind and run its phase 2 under the ingest slot.

    Returns True if it did work. A busy slot is not a failure and not work: the job
    goes back to `staged` with its attempt refunded, and the caller waits a poll.
    """
    job = queue.claim("staged", "phase2")
    if job is None:
        return False
    handler = _DRAIN.get(job["kind"])
    if handler is None:
        _fail(job, "staged", ValueError(f"no phase-2 handler for job kind {job['kind']!r}"))
        return True
    # What phase 1 measured, carried on the row. Handed to the drain handler so the
    # final report keeps the numbers only phase 1 can know — `leakage` and
    # `theses_dropped` for a `fetch` job — instead of dropping them and reporting
    # the linking half as the whole article.
    staged = job.get("report") or {}
    try:
        # Same id as the queue row: one piece of work, one id. `/ingest/jobs` merges
        # the two registers by id, and `stats.job_running` then names the id the
        # caller who posted the url (or run_id) is polling. `job["kind"]`, not the
        # literal "fetch": a run job busy-refusing a manual phase2 must say "run" in
        # the 409, not lie about what is actually holding the slot.
        with jobs.exclusive(job["kind"], job["args"], job_id=job["id"]):
            with trace.request(job["id"]) as own:
                report = handler(job, staged)
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
