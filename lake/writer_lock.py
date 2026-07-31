"""Один писатель фазы 2 на озеро, поверх процессов: `flock` на `data/writer.lock`.

Phase 2 is sequential by design (§4.5): two runs linking at the same time open two
ideas for one mechanism. `api/jobs.py` holds that invariant inside one process and
cannot see past it — `python3 -m lake.ingest.run phase2` on the host while the
container's writer thread drains the queue is two writers on one lake, and neither
one notices. That command is documented, so the guard has to sit below HTTP.

Every entry into phase 2 therefore passes through here:

    run.phase2       -> held()      the CLI, `/ingest/phase2`, and `drain_one` under it
    workers.start()  -> acquire()   held for as long as this process runs a writer

Re-entrant, and only for that reason: the writer's stint is
`write_step -> drain_one -> phase2` inside a process that already holds the lock from
`start()`, and `flock` is per open file description — a second `open()` in the same
process is refused exactly like another process would be. So a nested `held()` counts
instead of locking again.

NOT this module's job: two threads of one process. `jobs.exclusive` is that guard —
one slot, claimed and released in the same frame — and this one would let them both
through on the same depth counter.

**Stopped binding all writers once the store moved off this disk (`13` §4.4 p2).**
`flock` is a guarantee about *this machine*: two processes on the same host,
racing for the same open file, and one of them loses. The store `graph_client`
now writes through is Bolt over the network (`13` §4) — reachable from anywhere
that can open a TCP connection to it, not only from whatever host holds
`data/writer.lock`. A second writer process on a different machine, pointed at
the same `NEO4J_URI`, never touches this file and never finds out this lock
exists; it opens two ideas under one mechanism exactly like the two-processes
case this module was built to stop (§4.5), and nothing here can see it happen.
This is a known limitation, not a bug to route around here: the fix is a lock
that lives where the store does (a lease row in Neo4j, an advisory lock the
driver takes), and it is out of scope for this file, which only ever promised
"on one host".
"""
import contextlib
import errno
import fcntl
import os
import threading

from .models import DATA

LOCK_PATH = DATA / "writer.lock"    # rebindable: the self-checks point it at a temp dir

_LOCK = threading.Lock()            # guards the two globals below, not the graph
_fh = None
_depth = 0


class SecondWriter(RuntimeError):
    """Another process already holds the writer lock."""


def acquire() -> None:
    """Take the lock, or raise `SecondWriter`. Re-entrant within this process."""
    global _fh, _depth
    with _LOCK:
        if _depth:
            _depth += 1
            return
        path = LOCK_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        # "a+", not "w": `open("w")` truncates BEFORE the lock is asked for, so a
        # refused second writer would already have wiped the pid of the process that
        # holds it — and naming that process is the whole content of the file.
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise SecondWriter(
                    f"another process holds {path}: exactly one phase-2 writer may run "
                    "against one lake (§4.5). Serve the API with a single worker "
                    "process, and do not run `python3 -m lake.ingest.run phase2` beside "
                    "it — check the pid in that file for who is writing") from exc
            # ENOTSUP / EOPNOTSUPP: a filesystem with no flock (some NFS mounts). Raised
            # as itself. "This guard does not work here" must not read as "the lock is
            # free", and it must not read as "somebody else is writing" either.
            raise
        handle.truncate(0)
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        _fh, _depth = handle, 1


def release() -> None:
    """Give back one level. The OS lock goes at depth 0, with the file handle."""
    global _fh, _depth
    with _LOCK:
        if not _depth:
            return
        _depth -= 1
        if _depth == 0 and _fh is not None:
            _fh.close()                     # closing releases the flock
            _fh = None


def depth() -> int:
    """How many levels deep this process holds the lock. 0 means it holds nothing."""
    with _LOCK:
        return _depth


@contextlib.contextmanager
def held():
    acquire()
    try:
        yield
    finally:
        release()


if __name__ == "__main__":                              # python3 -m lake.writer_lock
    import pathlib
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        LOCK_PATH = Path(tmp) / "writer.lock"
        assert depth() == 0
        with held():
            assert depth() == 1
            assert LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid())
            with held():                                # the nesting the writer needs
                assert depth() == 2
            assert depth() == 1, "the inner exit released the OS lock, not one level"

            # Another PROCESS is the case this file exists for, and it is the one an
            # in-process test cannot see: `fcntl.flock` is per open file description,
            # so a second `open()` here would be refused too and prove nothing about
            # a `python3 -m lake.ingest.run phase2` next to a running container.
            #
            # And the neighbour runs THIS module's `acquire()`, not a hand-rolled flock:
            # a probe that opens the file its own way cannot see how `acquire` opens it,
            # and the pid assertion below — the one that catches an `open("w")` truncating
            # the lock file before asking for the lock — would then be aimed at the probe.
            probe = ("import sys\n"
                     "from pathlib import Path\n"
                     "from lake import writer_lock\n"
                     "writer_lock.LOCK_PATH = Path(sys.argv[1])\n"
                     "try:\n"
                     "    writer_lock.acquire()\n"
                     "except writer_lock.SecondWriter:\n"
                     "    print('refused')\n"
                     "else:\n"
                     "    print('TOOK IT')\n")
            root = str(pathlib.Path(__file__).resolve().parents[1])
            env = {**os.environ, "PYTHONPATH": root}
            out = subprocess.run([sys.executable, "-c", probe, str(LOCK_PATH)],
                                 capture_output=True, text=True, timeout=60,
                                 cwd=root, env=env)
            assert out.stdout.strip() == "refused", (out.stdout, out.stderr)
            # The pid survived the refusal: a second writer that truncated the file on
            # its way out would leave nobody to blame.
            assert LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()), \
                "the refused writer rewrote the lock file it was refused"
        assert depth() == 0
        out = subprocess.run([sys.executable, "-c", probe, str(LOCK_PATH)],
                             capture_output=True, text=True, timeout=60, cwd=root, env=env)
        assert out.stdout.strip() == "TOOK IT", (out.stdout, out.stderr)

    print("ok: re-entrant in this process, refused in another one, the pid survives a "
          "refusal, released on exit")
