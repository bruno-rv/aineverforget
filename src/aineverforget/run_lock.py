"""aineverforget.run_lock — ingest single-writer lock.

Ported from neverforget ``scripts/run_lock.py`` (pid + heartbeat + generation
guard).  Adapted for aineverforget paths (``runs/.ainf-ingest.lock``).

Enforces single-writer ingest so the "read max generation → write G+1" step
in the ingest flow is race-free.  Concurrent ingest attempts are rejected
(exit 3), not silently corrupting.

Lock file schema (runs/.ainf-ingest.lock, JSON):
    {
        "pid": <int>,
        "pid_start_time": "<verbatim ps -o lstart= output>",
        "session_id": "<opaque string>",
        "lock_id": "<uuid4>",
        "started_at": "<ISO-8601 with offset>",
        "heartbeat_at": "<ISO-8601 with offset>",
        "stage": "<stage name>"
    }

Exit codes:
    0  success / idempotent (missing file on release/heartbeat)
    3  live run overlap (acquire only) — caller must abort ingest
    4  release by non-owner lock_id
    5  malformed lock JSON

Atomicity and concurrency (TOCTOU + RMW protection):

    acquire (O_EXCL fast path):
        Uses O_CREAT|O_EXCL to create the lock file atomically.  If the file
        does not exist the exclusive-create succeeds, we write our lock, and
        exit 0.  If the file already exists (FileExistsError) we enter the
        stale/reclaim path under an fcntl.flock(LOCK_EX) on the SIDECAR guard
        file (see below), then rewrite via a unique temp-file + os.replace()
        rename.  The flock serialises concurrent reclaim attempts.

    heartbeat / stage / release (RMW protection):
        Every read-modify-write operation holds fcntl.flock(LOCK_EX) on the
        SIDECAR guard file for the entire read → modify → write critical section.

    Sidecar guard file (runs/.ainf-ingest.lock.flock):
        _flock_guard() opens the SIDECAR (``<lock_path>.flock``) with
        O_RDWR|O_CREAT.  O_CREAT is NEVER called on the real lock file by
        mutators (heartbeat/stage/release) — only acquire's O_EXCL creates it.
        This prevents the zombie-recreate bug.

Usage (Python API)
------------------
Callers in ``ingest.py`` use the context manager::

    from aineverforget.run_lock import IngestLock

    with IngestLock(session_id="my-session", run_dir=Path("runs")) as lock_id:
        # single-writer window
        ...  # acquire returns if no overlap
    # released on exit

    # Or check for overlap:
    try:
        with IngestLock(session_id=...) as _:
            do_ingest()
    except IngestLockOverlapError:
        sys.exit("another ingest is running")
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Default lock path (relative to project root or CWD)
# ---------------------------------------------------------------------------

DEFAULT_LOCK_PATH = "runs/.ainf-ingest.lock"


# ---------------------------------------------------------------------------
# Module-level nonce counter — ensures unique temp paths per _write_lock call.
# ---------------------------------------------------------------------------

_write_nonce_lock = threading.Lock()
_write_nonce: int = 0


def _next_nonce() -> int:
    global _write_nonce
    with _write_nonce_lock:
        _write_nonce += 1
        return _write_nonce


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IngestLockOverlapError(RuntimeError):
    """Raised when acquire detects a live concurrent ingest."""

    def __init__(self, existing_lock_id: str) -> None:
        self.existing_lock_id = existing_lock_id
        super().__init__(
            f"Another ingest is already running (lock_id={existing_lock_id!r}). "
            "Wait for it to finish or reclaim a stale lock."
        )


# ---------------------------------------------------------------------------
# Helpers (identical to neverforget run_lock.py)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current time as ISO-8601 string with UTC offset."""
    return datetime.now().astimezone().isoformat()


def _pid_start_time(pid: int) -> str | None:
    """Return verbatim ``ps -o lstart= -p <pid>`` output, or None on failure."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            return None
        return output
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* refers to a currently running process."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _is_process_incarnation_live(pid: int, recorded_start_time: str) -> bool:
    """True only when pid is alive AND ps lstart matches (guards PID reuse)."""
    if not _pid_alive(pid):
        return False
    live_start = _pid_start_time(pid)
    if live_start is None:
        return False
    return live_start == recorded_start_time


def _heartbeat_stale(heartbeat_at: str, grace_hours: float) -> bool:
    """True if *heartbeat_at* (ISO-8601) is older than *grace_hours*."""
    try:
        ts = datetime.fromisoformat(heartbeat_at)
        now = datetime.now().astimezone()
        elapsed_hours = (now - ts).total_seconds() / 3600
        return elapsed_hours > grace_hours
    except (ValueError, TypeError):
        return True


def _read_lock(lock_path: Path) -> dict:
    """Read and parse the lock file; raises FileNotFoundError or exits(5)."""
    try:
        text = lock_path.read_text()
    except FileNotFoundError:
        raise
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"malformed lock JSON at {lock_path}", file=sys.stderr)
        sys.exit(5)


def _write_lock(lock_path: Path, data: dict) -> None:
    """Atomically write *data* as JSON to *lock_path* via temp+rename."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    nonce = _next_nonce()
    tmp = lock_path.parent / f".{lock_path.name}.{os.getpid()}.{nonce}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(lock_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _sidecar_path(lock_path: Path) -> Path:
    """Return the sidecar guard-file path for *lock_path*."""
    return lock_path.parent / (lock_path.name + ".flock")


@contextlib.contextmanager
def _flock_guard(lock_path: Path):
    """Context manager holding LOCK_EX on the sidecar guard file."""
    sidecar = _sidecar_path(lock_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_acquire(args: argparse.Namespace) -> int:
    """Acquire the ingest lock.  Returns 0 on success, 3 on live overlap."""
    lock_path = Path(args.lock_path)
    grace_hours: float = args.grace_hours

    my_pid: int = args.owner_pid if args.owner_pid is not None else os.getpid()
    my_start_time = _pid_start_time(my_pid)
    if my_start_time is None:
        print(
            f"error: cannot determine pid_start_time for pid {my_pid}",
            file=sys.stderr,
        )
        sys.exit(1)

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    new_lock = {
        "pid": my_pid,
        "pid_start_time": my_start_time,
        "session_id": args.session_id,
        "lock_id": str(uuid.uuid4()),
        "started_at": args.run_id,
        "heartbeat_at": _now_iso(),
        "stage": "ingest",
    }

    # Phase 1: atomic exclusive create.
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, json.dumps(new_lock, indent=2).encode())
        finally:
            os.close(fd)
        print(f"lock_id={new_lock['lock_id']}")
        return 0
    except FileExistsError:
        pass

    # Phase 2: stale/reclaim under sidecar flock.
    sidecar = _sidecar_path(lock_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    flock_fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(flock_fd, fcntl.LOCK_EX)
        try:
            existing = _read_lock(lock_path)
        except FileNotFoundError:
            existing = None

        if existing is None:
            _write_lock(lock_path, new_lock)
            print(f"lock_id={new_lock['lock_id']}")
            return 0

        prev_pid = existing.get("pid")
        prev_start = existing.get("pid_start_time", "")
        prev_lock_id = existing.get("lock_id", "")
        prev_heartbeat = existing.get("heartbeat_at", "")

        incarnation_live = _is_process_incarnation_live(prev_pid, prev_start)
        stale = _heartbeat_stale(prev_heartbeat, grace_hours)

        if incarnation_live:
            print(f"run_overlap lock_id={prev_lock_id}")
            return 3

        _write_lock(lock_path, new_lock)
        print(f"reclaimed prev_pid={prev_pid}")
        print(f"lock_id={new_lock['lock_id']}")
        return 0
    finally:
        fcntl.flock(flock_fd, fcntl.LOCK_UN)
        os.close(flock_fd)


def cmd_release(args: argparse.Namespace) -> int:
    """Release the ingest lock.  Verifies ownership by lock_id."""
    lock_path = Path(args.lock_path)

    if not lock_path.exists():
        return 0

    with _flock_guard(lock_path):
        try:
            existing = _read_lock(lock_path)
        except FileNotFoundError:
            return 0

        if existing.get("lock_id") != args.lock_id:
            print(
                f"error: not owner (file lock_id={existing.get('lock_id')!r}, "
                f"provided={args.lock_id!r})",
                file=sys.stderr,
            )
            return 4

        lock_path.unlink(missing_ok=True)
        return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Run heartbeat refresher daemon (background process)."""
    lock_path = Path(args.lock_path)
    my_pid = args.pid
    my_lock_id = args.lock_id
    interval: int = args.interval

    while True:
        time.sleep(interval)

        if not lock_path.exists():
            return 0

        with _flock_guard(lock_path):
            try:
                data = _read_lock(lock_path)
            except FileNotFoundError:
                return 0

            if data.get("pid") != my_pid or data.get("lock_id") != my_lock_id:
                return 0

            data["heartbeat_at"] = _now_iso()
            _write_lock(lock_path, data)


def cmd_stage(args: argparse.Namespace) -> int:
    """Update the stage field in the lock (owner-checked)."""
    lock_path = Path(args.lock_path)

    if not lock_path.exists():
        return 0

    with _flock_guard(lock_path):
        try:
            data = _read_lock(lock_path)
        except FileNotFoundError:
            return 0

        if data.get("lock_id") != args.lock_id:
            return 0

        data["stage"] = args.stage
        _write_lock(lock_path, data)
        return 0


# ---------------------------------------------------------------------------
# Python API context manager for use by ingest.py
# ---------------------------------------------------------------------------


@contextmanager
def IngestLock(
    *,
    session_id: str,
    run_dir: Path | None = None,
    grace_hours: float = 2.0,
    owner_pid: int | None = None,
):
    """Context manager that acquires and releases the ingest lock.

    Designed for use in ``aineverforget.ingest.ingest_paths()`` to enforce
    single-writer access around the "read max generation → write G+1" step.

    On entry: acquires the lock; raises ``IngestLockOverlapError`` if a live
    concurrent ingest is running.
    On exit: releases the lock (always, even on exception).

    Parameters
    ----------
    session_id:
        Opaque caller-supplied session identifier (e.g. a run UUID).
    run_dir:
        Directory for the lock file.  Defaults to ``Path("runs")``.
    grace_hours:
        Hours before a stale heartbeat makes the lock reclaimable (default 2).
    owner_pid:
        PID to record in the lock.  Defaults to ``os.getpid()``.

    Raises
    ------
    IngestLockOverlapError
        If a live ingest (pid alive + fresh heartbeat) already holds the lock.

    Yields
    ------
    str
        The lock_id UUID string of the acquired lock.

    Examples
    --------
    ::

        from pathlib import Path
        from aineverforget.run_lock import IngestLock, IngestLockOverlapError

        try:
            with IngestLock(session_id="my-run-001") as lock_id:
                # single-writer critical section
                ...
        except IngestLockOverlapError as e:
            print(f"Cannot ingest: {e}")
            sys.exit(3)
    """
    if run_dir is None:
        run_dir = Path("runs")
    lock_path = run_dir / ".ainf-ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    my_pid = owner_pid if owner_pid is not None else os.getpid()
    my_start_time = _pid_start_time(my_pid)
    if my_start_time is None:
        raise RuntimeError(f"Cannot determine pid_start_time for pid {my_pid}")

    new_lock = {
        "pid": my_pid,
        "pid_start_time": my_start_time,
        "session_id": session_id,
        "lock_id": str(uuid.uuid4()),
        "started_at": _now_iso(),
        "heartbeat_at": _now_iso(),
        "stage": "ingest",
    }
    lock_id = new_lock["lock_id"]

    # Acquire (same logic as cmd_acquire but inline for Python callers)
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, json.dumps(new_lock, indent=2).encode())
        finally:
            os.close(fd)
    except FileExistsError:
        # Stale/reclaim path
        sidecar = _sidecar_path(lock_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        flock_fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(flock_fd, fcntl.LOCK_EX)
            try:
                existing = _read_lock(lock_path)
            except FileNotFoundError:
                existing = None

            if existing is None:
                _write_lock(lock_path, new_lock)
            else:
                prev_pid = existing.get("pid")
                prev_start = existing.get("pid_start_time", "")
                prev_lock_id = existing.get("lock_id", "")
                prev_heartbeat = existing.get("heartbeat_at", "")

                incarnation_live = _is_process_incarnation_live(prev_pid, prev_start)
                stale = _heartbeat_stale(prev_heartbeat, grace_hours)

                if incarnation_live:
                    # Do NOT unlock/close here — the finally below owns cleanup.
                    # Closing twice raised OSError(EBADF), masking the overlap error.
                    raise IngestLockOverlapError(prev_lock_id)

                _write_lock(lock_path, new_lock)
        finally:
            fcntl.flock(flock_fd, fcntl.LOCK_UN)
            os.close(flock_fd)

    # Fix E: Start a daemon thread that refreshes heartbeat_at every
    # heartbeat_interval_s seconds so any ingest lasting past grace_hours
    # is NOT incorrectly reclaimed by a second live writer.
    # The thread holds the sidecar flock during each write (same protocol
    # as cmd_heartbeat).
    heartbeat_interval_s = max(60.0, grace_hours * 3600 / 4)
    _stop_heartbeat = threading.Event()

    def _heartbeat_loop(
        _lock_path: Path = lock_path,
        _lock_id: str = lock_id,
        _interval: float = heartbeat_interval_s,
        _stop: threading.Event = _stop_heartbeat,
    ) -> None:
        while not _stop.wait(timeout=_interval):
            try:
                with _flock_guard(_lock_path):
                    data = _read_lock(_lock_path)
                    if data.get("lock_id") != _lock_id:
                        return  # lock was reclaimed; stop refreshing
                    data["heartbeat_at"] = _now_iso()
                    _write_lock(_lock_path, data)
            except Exception:
                pass  # best-effort; don't crash the daemon

    _hb_thread = threading.Thread(
        target=_heartbeat_loop,
        daemon=True,
        name=f"ainf-heartbeat-{lock_id[:8]}",
    )
    _hb_thread.start()

    try:
        yield lock_id
    finally:
        _stop_heartbeat.set()
        # Release — owner-checked
        if lock_path.exists():
            with _flock_guard(lock_path):
                try:
                    data = _read_lock(lock_path)
                    if data.get("lock_id") == lock_id:
                        lock_path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# CLI entry point (for subprocess-style usage from shell scripts / tests)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ainf-run-lock",
        description="Ingest single-writer lock for aineverforget.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    acq = subparsers.add_parser("acquire", help="Acquire the ingest lock.")
    acq.add_argument("--run-id", required=True)
    acq.add_argument("--session-id", required=True)
    acq.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    acq.add_argument("--grace-hours", type=float, default=2.0)
    acq.add_argument("--owner-pid", type=int, default=None)

    rel = subparsers.add_parser("release", help="Release the ingest lock.")
    rel.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    rel.add_argument("--lock-id", required=True)

    hb = subparsers.add_parser("heartbeat", help="Run heartbeat refresher daemon.")
    hb.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    hb.add_argument("--lock-id", required=True)
    hb.add_argument("--pid", type=int, required=True)
    hb.add_argument("--interval", type=int, default=600)

    st = subparsers.add_parser("stage", help="Update the stage field in the lock.")
    st.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    st.add_argument("--lock-id", required=True)
    st.add_argument("--stage", required=True)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "acquire":
        sys.exit(cmd_acquire(args))
    elif args.subcommand == "release":
        sys.exit(cmd_release(args))
    elif args.subcommand == "heartbeat":
        sys.exit(cmd_heartbeat(args))
    elif args.subcommand == "stage":
        sys.exit(cmd_stage(args))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
