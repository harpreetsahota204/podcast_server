"""Posts to X through the xurl CLI, with an optional image or video attached.

xurl keeps the OAuth tokens in its own auth file. This module passes only the
app name and username from settings, so no X credential passes through the
engine, its logs, or tool output.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import settings
from core.discovery import clean_alt_text, override_reason
from core.formatting import validate_slug
from core.jsonl import append_record, read_records
from core.media_uploader import MEDIA_CONTENT_TYPES

# xurl colors its JSON when it thinks stdout is a terminal; a stray color code would break parsing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# xurl error text is returned to the agent; cap it so a long dump stays readable.
MAX_ERROR_CHARS = 300


class XPostError(RuntimeError):
    """Posting is not configured, or xurl or X rejected the upload or the post."""


def decode_xurl_json(raw: str) -> dict[str, Any]:
    """Decode xurl stdout, ignoring color codes and any trailing status text; X `errors` are left in place.

    Reads (e.g. metrics) use this directly, because X reports per-item field errors
    alongside valid data; posting goes through `parse_xurl_json`, which rejects them.
    """
    cleaned = _ANSI_RE.sub("", raw).strip()
    start = cleaned.find("{")
    if start < 0:
        raise XPostError("xurl returned no JSON object")
    # raw_decode stops at the end of the first object, so trailing non-JSON output is ignored.
    try:
        payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise XPostError(f"xurl JSON decode error: {exc}") from exc
    if not isinstance(payload, dict):
        raise XPostError("xurl JSON was not an object")
    return payload


def parse_xurl_json(raw: str) -> dict[str, Any]:
    """Parse xurl stdout, ignoring color codes and any trailing status text; raise on X errors."""
    payload = decode_xurl_json(raw)
    if payload.get("errors"):
        raise XPostError(f"X error: {payload['errors']}"[:MAX_ERROR_CHARS])
    return payload


def id_from_payload(payload: Mapping[str, Any]) -> str:
    """Return the id from an upload or post response (`data.id`, or a top-level `id`)."""
    data = payload.get("data")
    if isinstance(data, dict) and data.get("id") not in (None, ""):
        return str(data["id"])
    if payload.get("id") not in (None, ""):
        return str(payload["id"])
    raise XPostError("xurl JSON had no data.id")


def xurl_command() -> list[str]:
    """Return the xurl binary plus account flags from settings; raise if X isn't configured.

    Shared by posting and by the X metrics collector, so both act as the same account.
    """
    if not settings.x_username:
        raise XPostError("X posting is off: set X_USERNAME (and XURL_APP if xurl has several apps).")
    binary = settings.xurl_path or (Path(found) if (found := shutil.which("xurl")) else None)
    if binary is None or not binary.is_file():
        raise XPostError(f"xurl not found at {binary or 'PATH'}; install it or set XURL_PATH.")
    # --app is only needed when the xurl auth file holds more than one developer app.
    app_flags = ["--app", settings.xurl_app] if settings.xurl_app else []
    return [str(binary), *app_flags, "--username", settings.x_username]


def run_xurl(args: Sequence[str]) -> dict[str, Any]:
    """Run one xurl command and return its parsed JSON. Verbose mode is never used: it prints tokens."""
    completed = subprocess.run([*xurl_command(), *args], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = _ANSI_RE.sub("", f"{completed.stdout}\n{completed.stderr}").strip().replace("\n", " ")
        raise XPostError(f"xurl exit {completed.returncode}: {detail}"[:MAX_ERROR_CHARS])
    return parse_xurl_json(completed.stdout or "")


def upload_media(media_path: Path) -> str:
    """Upload one image, GIF, or video and return its X media id.

    xurl waits for X to finish processing a video before returning (`--wait` is its default).
    """
    return id_from_payload(run_xurl(["media", "upload", str(media_path)]))


def set_alt_text(media_id: str, alt_text: str) -> None:
    """Attach alt text to uploaded media; X only accepts it before the post that uses the media."""
    body = {"id": media_id, "metadata": {"alt_text": {"text": alt_text}}}
    run_xurl(["-X", "POST", "/2/media/metadata", "-d", json.dumps(body)])


def publish_post(text: str, media_id: str | None = None) -> str:
    """Publish `text`, attaching the uploaded media when there is one, and return the post id."""
    media_flags = ["--media-id", media_id] if media_id else []
    # "--" ends flag parsing, so text that starts with "-" (e.g. a bullet list) isn't read as a flag.
    return id_from_payload(run_xurl(["post", *media_flags, "--", text]))


def _resolve_media(media_path: str | None) -> Path | None:
    """Return the media file to attach, refusing missing, empty, or non-media files.

    Only the media extensions the R2 uploader accepts are allowed, so an agent can't
    post an arbitrary local file such as `.env`.
    """
    if not media_path:
        return None
    path = Path(media_path).expanduser().resolve()
    if path.suffix.lower() not in MEDIA_CONTENT_TYPES:
        raise ValueError(f"Unsupported media type {path.suffix!r}; allowed: {', '.join(sorted(MEDIA_CONTENT_TYPES))}")
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Media file missing or empty: {path}")
    return path


def read_post_log() -> list[dict[str, Any]]:
    """Every post this engine published, oldest first; malformed lines (e.g. a torn write) are skipped.

    The X metrics collector reads only these posts, so analytics cover exactly what
    went out through the engine.
    """
    return [r for r in read_records(settings.x_post_log_path) if r.get("tweet_id")]


def post_to_x(
    text: str,
    media_path: str | None = None,
    dry_run: bool = False,
    episode_slug: str | None = None,
    alt_text: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Post `text` (plus optional media) as the configured account and record it in the post log.

    Every check runs before the upload, so a rejected call posts nothing. Attached media needs
    `alt_text` (search and screen readers use it) unless `discovery_override` gives the human's
    reason. `dry_run` runs the same checks and reports what would be sent without calling X.
    `episode_slug` credits the post's metrics to that episode.
    """
    body = text.strip()
    if not body:
        raise ValueError("Post text is empty.")
    media = _resolve_media(media_path)
    alt = clean_alt_text(alt_text)
    override = override_reason(discovery_override)
    if media and not alt and override is None:
        raise ValueError(
            "Attached media needs alt_text; nothing was posted. "
            "Add it, or pass discovery_override with the human's reason for skipping."
        )
    xurl_command()  # fail on missing configuration before anything is uploaded

    summary = {
        "username": settings.x_username,
        "chars": len(body),
        "media_file": media.name if media else None,
        "media_type": MEDIA_CONTENT_TYPES[media.suffix.lower()] if media else None,
        "alt_text_chars": len(alt or ""),
        "episode_slug": validate_slug(episode_slug) if episode_slug else None,
    }
    if override is not None:
        summary["discovery_override"] = override
    if dry_run:
        return {"status": "dry_run", **summary}

    media_id = upload_media(media) if media else None
    # Alt text has to land before the post: a failure here stops the post, with nothing public yet.
    if media_id and alt:
        set_alt_text(media_id, alt)
    tweet_id = publish_post(body, media_id)
    url = f"https://x.com/{settings.x_username}/status/{tweet_id}"
    result: dict[str, Any] = {"status": "posted", "tweet_id": tweet_id, "url": url, **summary}

    # The post is already live here. A log failure must not raise, or an agent may retry and post twice.
    record = {"posted_at": datetime.now(UTC).isoformat(), **result, "text": body}
    if log_error := append_record(settings.x_post_log_path, record):
        result["log_error"] = log_error
    return result
