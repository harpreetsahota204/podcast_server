"""Append-only JSON Lines logs (one JSON object per line), e.g. the X post log and the YouTube upload log.

These logs record actions that already happened on an external service, so writing
must never raise (a caller could retry and post twice) and reading must survive a
torn last line from an interrupted write.
"""

import json
from pathlib import Path
from typing import Any


def append_record(path: Path, record: dict[str, Any]) -> str | None:
    """Append one record; return the error message instead of raising if the write fails."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        return str(exc)
    return None


def read_records(path: Path) -> list[dict[str, Any]]:
    """Every well-formed object in the log, oldest first; missing file → []; malformed lines are skipped."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
