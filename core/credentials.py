"""Private credential files kept outside `.env`: the Substack session cookie and the Instagram token.

Each controls a whole account, so the file is created readable only by its owner and
the setup doctor fails if anyone else can read it.
"""

import json
import os
from pathlib import Path
from typing import Any


def write_private_json(path: Path, data: dict[str, Any]) -> Path:
    """Write `data` as JSON to `path`, readable only by this user, and return the path.

    The parent directory is created 0700 if missing. A new file is created 0600, so the
    secret is never readable by others, even briefly; the creation mode doesn't apply to a
    file that already exists, so chmod tightens one left with looser permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
        json.dump(data, secret_file)
    path.chmod(0o600)
    return path


def readable_by_others(path: Path) -> bool:
    """True if the file exists and its group or other users have any permission on it."""
    return path.is_file() and bool(path.stat().st_mode & 0o077)
