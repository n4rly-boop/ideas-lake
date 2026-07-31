"""Долговечная очередь заданий: своя SQLite-база, переживает рестарт процесса.

Why a file and not the dict in `api/jobs.py`: that dict dies with the process, and
`GET /ingest/jobs/{id}` after a deploy answers 404 for work that is still in the
lake's files. The cursor already made the *work* restart-safe (§4.7); this makes
the *record* of it restart-safe too, which is what a caller polling a job id needs.

Why its own database file and not a Neo4j node: format B is `graph_client`'s alone
to write (§3.4), and a queue of jobs is not format B — it is this module's own
state, restart-safe the same way the graph is, but through a different store.
`index.py` set the precedent: own file, own module, own lock (`index.py:1-6`).

The queue does not decide anything about the ingest. It holds rows and hands the
oldest one out exactly once — `claim` is a single UPDATE, so two workers asking at
the same microsecond cannot both get the same job. Everything about *what* a job
means lives in `api/workers.py`; everything about *who may write the graph* stays
in `api/jobs.py`, which is the mutual-exclusion slot §4.5 rests on.

Statuses, and every transition that exists:

    queued  --claim-->  running(stage=phase1)  --stage()-->  staged
    staged  --claim-->  running(stage=phase2)  --finish()->  ok | failed
                                               --release()->  staged   (slot busy)

`staged` is a real status and not "running with a comment": between the two stages
the article is parsed but not linked, its staging file is on disk, and the writer
may be busy with somebody else's. A caller polling the job has to be able to see
that difference — "running" for an hour while nothing runs is the status that lies.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import JOBS_DB

# `UPDATE ... RETURNING` is what makes a claim one statement instead of a
# select-then-update with a window in the middle. Refused at import rather than
# silently degrading to the racy form: two workers taking one job would run the
# same source twice, and the second run would be invisible in the report.
if sqlite3.sqlite_version_info < (3, 35):
    raise RuntimeError(f"sqlite {sqlite3.sqlite_version} is too old for UPDATE ... "
                       "RETURNING (3.35+), which this queue's atomic claim needs")

DB: Path = JOBS_DB              # rebindable: the self-checks point it at a temp dir
MAX_ATTEMPTS = 3                # a job that died mid-run this many times stays failed
KEEP_FINISHED = 200             # ring of finished rows, like jobs.MAX_KEPT but on disk

DDL = """
CREATE TABLE IF NOT EXISTS job (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    args        TEXT NOT NULL,          -- json
    dedup_key   TEXT,
    status      TEXT NOT NULL,          -- queued | running | staged | ok | failed
    stage       TEXT,                   -- phase1 | phase2, null while queued
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    report      TEXT,                   -- json
    error       TEXT
);
CREATE INDEX IF NOT EXISTS job_status ON job(status, created_at);
CREATE INDEX IF NOT EXISTS job_dedup ON job(dedup_key, status);
"""

# One lock over one connection per database file — the same ponytail compromise as
# `index.py:30`, and for the same reason: every caller is in this process. `claim`
# is still a single UPDATE, so the invariant does not depend on the lock; the lock
# only keeps the connection to itself.
_LOCK = threading.RLock()
_CONNS: dict[str, sqlite3.Connection] = {}


class Full(RuntimeError):
    """The queue is at its ceiling. The caller answers 429, never a silent accept."""


class DedupConflict(RuntimeError):
    """A live job (queued/running/staged) already owns `dedup_key`, and its stored
    `args["payload_hash"]` differs from this call's. Handing back the live job here,
    the way a same-body replay does, would silently keep serving the FIRST body
    forever and drop the second one with no error anywhere — the exact lie idempotency
    must not tell. Only raised when both sides carry a `payload_hash`; a caller that
    never sets one (`/fetch`, whose `args` already double as identity) keeps today's
    plain idempotent replay."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _con(db=None) -> sqlite3.Connection:
    """Open (once per path) and create the table. Call under `_LOCK`."""
    path = Path(db or DB)
    key = str(path)
    con = _CONNS.get(key)
    if con is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: `claim` needs BEGIN IMMEDIATE to be its own statement.
        # The implicit-transaction mode python defaults to would open a *deferred*
        # transaction, which upgrades to a write lock only at the UPDATE — and that
        # upgrade is what raises "database is locked" instead of waiting.
        con = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                              timeout=30.0)
        con.row_factory = sqlite3.Row
        con.executescript(DDL)
        _CONNS[key] = con
    return con


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    job = dict(row)
    job["args"] = json.loads(job["args"]) if job["args"] else {}
    job["report"] = json.loads(job["report"]) if job["report"] else None
    return job


def close() -> None:
    """Drop the cached handles. For the self-checks, which reopen a temp file."""
    with _LOCK:
        for con in _CONNS.values():
            con.close()
        _CONNS.clear()


# ------------------------------------------------------------------------ writing

def enqueue(kind: str, args: dict, *, dedup_key: str | None = None,
            ceiling: int = 0, db=None, on_accept=None) -> dict:
    """Add a job, or return the live one with the same `dedup_key`.

    Idempotent on purpose: `POST /fetch` of a url that is already waiting must not
    open a second run of the same article. Two jobs for one source would parse it
    twice and hand phase 2 two staging files whose second run reads as "0 written,
    30 skipped" — a duplicate that looks like a clean replay. A dedup hit whose
    `args["payload_hash"]` disagrees with this call's is a DIFFERENT body wearing
    the same id, and raises `DedupConflict` instead of silently serving the old one.

    `ceiling` > 0 refuses with `Full` once that many jobs are unfinished. Accepting
    an unbounded backlog is the polite version of dropping it: every accepted job
    gets a status saying "queued" that no worker will reach for hours.

    `on_accept`, if given, runs (still holding `_LOCK`) after the dedup and ceiling
    checks pass and BEFORE the row is inserted — so a caller with a side-channel
    payload too big for `args` (the batch body of `POST /run`) writes it exactly
    when, and only when, the job is actually accepted: never on a 429, never on a
    dedup hit. Before the INSERT, not after, so a worker can never see a queued row
    whose payload is not on disk yet.
    """
    with _LOCK:
        con = _con(db)
        if dedup_key:
            live = con.execute(
                "SELECT * FROM job WHERE dedup_key = ? AND status IN "
                "('queued', 'running', 'staged') ORDER BY created_at LIMIT 1",
                (dedup_key,)).fetchone()
            if live is not None:
                live_row = _row(live)
                new_hash = args.get("payload_hash")
                live_hash = live_row["args"].get("payload_hash")
                if new_hash is not None and live_hash is not None and new_hash != live_hash:
                    raise DedupConflict(
                        f"a {live_row['status']} job for dedup_key {dedup_key!r} (id "
                        f"{live_row['id']}) already exists with a different body; wait "
                        "for it to finish or use a different id")
                return live_row
        if ceiling:
            waiting = con.execute(
                "SELECT count(*) FROM job WHERE status IN ('queued', 'running', "
                "'staged')").fetchone()[0]
            if waiting >= ceiling:
                raise Full(f"{waiting} jobs already queued or running (ceiling "
                           f"{ceiling}); retry when the backlog drains")
        if on_accept is not None:
            on_accept()
        job_id = f"job_{os.urandom(6).hex()}"
        con.execute("INSERT INTO job (id, kind, args, dedup_key, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'queued', ?)",
                    (job_id, kind, json.dumps(args, ensure_ascii=False), dedup_key,
                     _now()))
        return _row(con.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone())


def claim(status: str, stage: str, *, db=None) -> dict | None:
    """Take the oldest job in `status` and mark it running at `stage`. None if empty.

    One statement, so the check and the take cannot be interleaved: the worker pool
    and the writer both call this, and a job handed to two of them would ingest one
    article twice.

    Claims the oldest row of ANY kind — `api/workers.py`'s two steps dispatch on
    `job["kind"]` themselves (the `_STAGE`/`_DRAIN` tables) rather than running a
    claimer per kind, so the pool stays `FETCH_WORKERS` wide and the writer stays
    exactly one, whichever kind is waiting. A `kind=` filter used to sit here for a
    worker pool split by kind that nothing ever built — both real callers always
    passed `kind=None` — so it was a second, duplicated `UPDATE ... RETURNING`
    branch that only this module's own self-check ever exercised. Removed rather
    than kept as a primitive nobody calls; the dispatch table is what is asserted
    now, in `api/selfcheck.py`'s `/fetch` and `/run` sections.
    """
    with _LOCK:
        con = _con(db)
        row = con.execute(
            "UPDATE job SET status = 'running', stage = ?, started_at = ?, "
            "attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM job WHERE status = ? "
            "            ORDER BY created_at, rowid LIMIT 1) "
            "RETURNING *", (stage, _now(), status)).fetchone()
        return _row(row)


def stage(job_id: str, report: dict | None = None, *, db=None) -> None:
    """Phase 1 is done: the staging file exists, the graph has not been opened.

    `attempts` goes back to zero, because it counts claims and the two phases are two
    different pieces of work: an article that needed two fetches to parse would enter
    phase 2 with one life left, and a single busy-slot afternoon would then fail work
    that was already parsed and sitting on disk.
    """
    with _LOCK:
        _con(db).execute(
            "UPDATE job SET status = 'staged', stage = 'phase1', attempts = 0, report = ? "
            "WHERE id = ?",
            (json.dumps(report, ensure_ascii=False) if report else None, job_id))


def release(job_id: str, back_to: str, *, db=None) -> None:
    """Put a claimed job back without counting the attempt.

    The writer calls this when the ingest slot is held by a manual phase 2 or a
    repair: nothing about the job failed, it simply is not its turn. Counting that
    as an attempt would burn a job's three lives on a busy afternoon.
    """
    with _LOCK:
        _con(db).execute(
            "UPDATE job SET status = ?, stage = ?, started_at = NULL, "
            "attempts = attempts - 1 WHERE id = ?", (back_to, _stage_of(back_to), job_id))


def _stage_of(status: str) -> str | None:
    """Which half a waiting job is in, by the status it waits in.

    `claim` writes the stage it is about to run; putting a job back has to unwrite it,
    or a released job reads `queued / phase2` — and `recover` routes by exactly that
    field, so a stale one sends a job to the wrong queue after a restart.
    """
    return "phase1" if status == "staged" else None


def finish(job_id: str, status: str, *, report: dict | None = None,
           error: str | None = None, db=None) -> None:
    """Terminal: `ok` or `failed`. Both carry their evidence."""
    assert status in ("ok", "failed"), status
    with _LOCK:
        _con(db).execute(
            "UPDATE job SET status = ?, finished_at = ?, report = ?, error = ? "
            "WHERE id = ?",
            (status, _now(), json.dumps(report, ensure_ascii=False) if report else None,
             error, job_id))
        _prune(db)


def retry(job_id: str, back_to: str, error: str, *, db=None) -> dict | None:
    """Return a failed attempt to the queue, or fail it for good past MAX_ATTEMPTS.

    A canary timeout means the school's pool is full, not that the article is bad —
    it comes back. But an article that dies three times is a bug or a dead source,
    and looping it forever would spend the GPU on a hole and bury the reason.
    """
    with _LOCK:
        con = _con(db)
        row = con.execute("SELECT attempts FROM job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        if row["attempts"] >= MAX_ATTEMPTS:
            finish(job_id, "failed", error=f"{error} (attempt {row['attempts']} of "
                                           f"{MAX_ATTEMPTS}, giving up)", db=db)
        else:
            con.execute("UPDATE job SET status = ?, stage = ?, started_at = NULL, "
                        "error = ? WHERE id = ?",
                        (back_to, _stage_of(back_to), error, job_id))
        return _row(con.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone())


def recover(*, db=None) -> dict:
    """Startup: nothing is running, whatever says otherwise died with the last process.

    A `running` row cannot be resumed in place — the thread that held it is gone —
    but its work can: phase 1 replays a source from its own beginning (and the parse
    cache makes the replay cheap), phase 2 resumes at the staging cursor. So the row
    goes back to the status it was claimed from, one attempt poorer. Left as
    "running", it would be a job that says it is working while no thread exists.
    """
    with _LOCK:
        con = _con(db)
        back = {"phase1": "queued", "phase2": "staged"}
        moved = {"queued": 0, "staged": 0, "failed": 0}
        for row in con.execute("SELECT * FROM job WHERE status = 'running'").fetchall():
            target = back.get(row["stage"] or "phase1", "queued")
            if row["attempts"] >= MAX_ATTEMPTS:
                finish(row["id"], "failed",
                       error=f"process restarted while this job was in {row['stage']}, "
                             f"and it had already used {row['attempts']} of "
                             f"{MAX_ATTEMPTS} attempts", db=db)
                moved["failed"] += 1
                continue
            con.execute("UPDATE job SET status = ?, stage = ?, started_at = NULL, "
                        "error = ? WHERE id = ?",
                        (target, _stage_of(target),
                         f"process restarted during {row['stage']}, requeued", row["id"]))
            moved[target] += 1
        return moved


def _prune(db=None) -> None:
    """Keep the last KEEP_FINISHED terminal rows. Caller holds `_LOCK`.

    `jobs.py` bounded its dict by the life of the process; a file has no such bound,
    and an operator's history is worth exactly as much as the last few hundred runs.
    """
    _con(db).execute(
        "DELETE FROM job WHERE status IN ('ok', 'failed') AND id NOT IN "
        "(SELECT id FROM job WHERE status IN ('ok', 'failed') "
        " ORDER BY finished_at DESC, rowid DESC LIMIT ?)", (KEEP_FINISHED,))


# ------------------------------------------------------------------------ reading

def get(job_id: str, *, db=None) -> dict | None:
    with _LOCK:
        return _row(_con(db).execute("SELECT * FROM job WHERE id = ?",
                                     (job_id,)).fetchone())


def listing(limit: int = 50, *, db=None) -> list[dict]:
    """Newest first."""
    with _LOCK:
        rows = _con(db).execute("SELECT * FROM job ORDER BY created_at DESC, rowid DESC "
                                "LIMIT ?", (limit,)).fetchall()
        return [_row(row) for row in rows]


def counts(*, db=None) -> dict:
    """One number per status, zeros included — a missing key reads as "no such state"."""
    with _LOCK:
        rows = _con(db).execute("SELECT status, count(*) AS n FROM job "
                                "GROUP BY status").fetchall()
    out = {s: 0 for s in ("queued", "running", "staged", "ok", "failed")}
    for row in rows:
        out[row["status"]] = row["n"]
    return out


if __name__ == "__main__":                                          # python3 -m lake.queue
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "jobs.db"

        one = enqueue("fetch", {"url": "u1"}, dedup_key="2406.04824", db=db)
        assert one["status"] == "queued" and one["attempts"] == 0, one
        again = enqueue("fetch", {"url": "u1"}, dedup_key="2406.04824", db=db)
        assert again["id"] == one["id"], "the same url twice must not open two jobs"
        two = enqueue("fetch", {"url": "u2"}, dedup_key="2504.16891", db=db)
        assert two["id"] != one["id"]

        # FIFO, and a claim is exclusive: the second call gets the *other* job, never
        # the same one — this is the property the whole worker pool rests on.
        first = claim("queued", "phase1", db=db)
        second = claim("queued", "phase1", db=db)
        assert first["id"] == one["id"] and second["id"] == two["id"], (first, second)
        assert claim("queued", "phase1", db=db) is None, "empty queue must hand out None"
        assert first["attempts"] == 1 and first["status"] == "running"

        # Ceiling: three unfinished jobs, ceiling of 2 -> refusal, not a silent accept.
        try:
            enqueue("fetch", {"url": "u3"}, ceiling=2, db=db)
        except Full:
            pass
        else:
            raise AssertionError("a full queue must raise, not accept")

        stage(one["id"], {"staging_lines": 30}, db=db)
        assert get(one["id"], db=db)["status"] == "staged"
        assert counts(db=db)["staged"] == 1, counts(db=db)
        # Phase 2 gets its own lives: this job already spent one claim in phase 1, and
        # entering the writer's queue with two left is how parsed work dies of a busy
        # afternoon.
        assert get(one["id"], db=db)["attempts"] == 0, get(one["id"], db=db)

        # The writer's turn: claim from `staged`, hit a busy slot, put it back. The
        # attempt must not be spent — otherwise a busy afternoon burns all three.
        writing = claim("staged", "phase2", db=db)
        assert writing["id"] == one["id"] and writing["attempts"] == 1, writing
        assert writing["stage"] == "phase2", writing
        release(one["id"], "staged", db=db)
        assert get(one["id"], db=db)["attempts"] == 0, "release must not spend a life"
        assert get(one["id"], db=db)["status"] == "staged"
        # And it must un-write the stage it was claimed for: `recover` routes a restarted
        # job by this field, so `staged / phase2` would come back as a phase-2 job whose
        # phase 1 never happened.
        assert get(one["id"], db=db)["stage"] == "phase1", get(one["id"], db=db)
        claim("staged", "phase2", db=db)
        release(one["id"], "queued", db=db)
        assert get(one["id"], db=db)["stage"] is None, get(one["id"], db=db)
        stage(one["id"], {"staging_lines": 30}, db=db)

        # Restart with a job mid-flight: it comes back to the status it was taken
        # from, not to "running" with no thread behind it.
        claim("staged", "phase2", db=db)
        assert get(one["id"], db=db)["status"] == "running"
        close()
        moved = recover(db=db)
        assert moved["staged"] == 1, moved
        assert get(one["id"], db=db)["status"] == "staged", get(one["id"], db=db)
        assert "restarted" in get(one["id"], db=db)["error"]

        # Three deaths and it stays dead, with the reason on the row.
        for _ in range(4):
            job = claim("staged", "phase2", db=db) or claim("queued", "phase1", db=db)
            if job is None or job["id"] != one["id"]:
                break
            retry(one["id"], "staged", "LLMError: canary timed out", db=db)
        final = get(one["id"], db=db)
        assert final["status"] == "failed", final
        assert "giving up" in final["error"], final

        finish(two["id"], "ok", report={"theses_written": 12}, db=db)
        done = get(two["id"], db=db)
        assert done["status"] == "ok" and done["report"]["theses_written"] == 12
        assert done["finished_at"], "a terminal job must carry when it ended"

        # The ring: finished rows are bounded on disk the way jobs.py bounded them
        # in memory.
        KEEP_FINISHED = 3
        for i in range(6):
            job = enqueue("fetch", {"url": f"ring{i}"}, db=db)
            finish(job["id"], "ok", db=db)
        assert counts(db=db)["ok"] == 3, counts(db=db)
        KEEP_FINISHED = 200
        close()

        # --- the claim is atomic, and only another PROCESS can prove it -------------
        # In one process every caller goes through `_LOCK`, so a select-then-update
        # would pass a threaded test while still handing one job to two workers across
        # processes — which is what `uvicorn --workers 2` and a CLI beside a container
        # actually are. Four processes drain one queue; every id must be claimed exactly
        # once, and the union must be the whole queue (nothing lost, nothing doubled).
        import subprocess
        import sys
        from collections import Counter

        # Every child says "ready" and only THEN are the jobs written, because the
        # obvious order — fill the queue, then spawn — does not race at all: the first
        # child drains all 60 rows before the second interpreter finishes importing, and
        # the check prints ok having had one claimer. Measured, not guessed: with the
        # racy two-statement claim in place, that version reported per-process counts of
        # [0, 60, 0, 0] and stayed green.
        race_db = Path(tmp) / "race.db"
        done_flag = Path(tmp) / "race.done"
        total = 200
        claimer = ("import json, sys, time\n"
                   "from pathlib import Path\n"
                   "from lake import queue\n"
                   "db, flag = sys.argv[1], Path(sys.argv[2])\n"
                   "queue._con(db)\n"                       # pay the import and the DDL first
                   "print('ready', flush=True)\n"
                   "while not flag.exists():\n"             # all four start on a full queue
                   "    time.sleep(0.0002)\n"
                   "out = []\n"
                   "while True:\n"
                   "    job = queue.claim('queued', 'phase1', db=db)\n"
                   "    if job is None:\n"
                   "        break\n"
                   "    out.append(job['id'])\n"
                   "    time.sleep(0.0005)\n"          # a real worker works between claims
                   "print(json.dumps(out))\n")
        root = str(Path(__file__).resolve().parents[1])
        procs = [subprocess.Popen([sys.executable, "-c", claimer, str(race_db),
                                   str(done_flag)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, cwd=root,
                                  env={**os.environ, "PYTHONPATH": root})
                 for _ in range(4)]
        claimed: list[str] = []
        try:
            for proc in procs:
                assert proc.stdout.readline().strip() == "ready", "a claimer never came up"
            for i in range(total):
                enqueue("fetch", {"url": f"race{i}"}, db=race_db)
            close()                     # the writers must not queue behind this handle
        finally:
            # Released together, onto a queue that is already full: this is the only
            # arrangement in which four processes are inside `claim` at the same time.
            done_flag.write_text("go\n", encoding="utf-8")
        per_proc = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            ids = json.loads(out)
            per_proc.append(len(ids))
            claimed += ids
        doubled = [job_id for job_id, n in Counter(claimed).items() if n > 1]
        assert not doubled, f"one job handed to two processes: {doubled} (per process: {per_proc})"
        assert len(claimed) == total, f"{len(claimed)} of {total} jobs claimed: {per_proc}"
        assert counts(db=race_db)["queued"] == 0, counts(db=race_db)
        # A run where one process took everything proved nothing about atomicity, and
        # would have gone green with the racy claim. Named, not silently accepted.
        assert sum(1 for n in per_proc if n) >= 2, \
            f"only one process ever claimed, so nothing raced: {per_proc}"
        close()

    print("ok: dedup, FIFO claim, ceiling, release keeps the attempt and un-writes the "
          "stage, phase 2 gets its own attempts, restart recovery, give-up after "
          "MAX_ATTEMPTS, finished ring, 4 processes drain 200 jobs with no double claim")
