"""Runs `scripts/backup_to_hf.py` as a background job, so an MCP call never waits on a multi-GB upload.

Even an incremental backup hashes every file in DATA_DIR/backup/ before uploading,
which takes longer than MCP clients wait for one tool call. The job therefore runs
detached and appends its output to DATA_DIR/backup/last_run.log. While it runs it
holds an exclusive lock on DATA_DIR/backup/.backup.lock; the kernel drops that lock
when the process exits, so a crashed run never leaves a stale "running" state.
"""

import fcntl
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from core.config import PROJECT_ROOT, settings

BACKUP_SCRIPT = PROJECT_ROOT / "scripts" / "backup_to_hf.py"
LOCK_NAME = ".backup.lock"
LOG_NAME = "last_run.log"
# Written before each run, so the log's latest run is everything after the last header.
RUN_HEADER = "=== backup started"
# backup_to_hf.py logs this only after its final upload succeeds.
COMPLETE_MARKER = "Backup complete"
LOG_TAIL_LINES = 15


@contextmanager
def backup_lock() -> Iterator[None]:
    """Hold the backup lock for the block; raise RuntimeError if another backup already holds it."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.backup_dir / LOCK_NAME, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another backup is already running.") from None
        # Closing the handle on exit releases the lock.
        yield


def _is_running() -> bool:
    """True while some backup process holds the lock."""
    try:
        with backup_lock():
            return False
    except RuntimeError:
        return True


def backup_status() -> dict[str, Any]:
    """Report whether a backup is running and how the latest background run ended, with its last log lines."""
    log_path = settings.backup_dir / LOG_NAME
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []
    # Keep only the latest run: the lines after the last header.
    headers = [i for i, line in enumerate(lines) if line.startswith(RUN_HEADER)]
    latest_run = lines[headers[-1]:] if headers else []

    if _is_running():
        status = "running"
    elif not latest_run:
        status = "never_run"
    elif any(COMPLETE_MARKER in line for line in latest_run):
        status = "completed"
    else:
        status = "failed"
    return {"status": status, "log_path": str(log_path), "log_tail": latest_run[-LOG_TAIL_LINES:]}


def start_backup() -> dict[str, Any]:
    """Start a detached backup unless one is already running; return immediately with its status."""
    if not settings.hf_backup_repo:
        raise ValueError("Backups are off: set HF_BACKUP_REPO in .env to a private Hugging Face dataset id.")
    if _is_running():
        return backup_status()

    log_path = settings.backup_dir / LOG_NAME
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"{RUN_HEADER} {datetime.now(UTC).isoformat(timespec='seconds')} ===\n")
        log.flush()
        # The child gets its own session so it outlives an MCP client disconnect. Its output goes to
        # the log, never to this process's stdout (the MCP stream). Progress bars would flood the log.
        process = subprocess.Popen(
            [sys.executable, str(BACKUP_SCRIPT)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
        )
    return {"status": "started", "pid": process.pid, "log_path": str(log_path)}
