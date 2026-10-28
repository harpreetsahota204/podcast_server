"""Turns a Descript export (Markdown transcript + published audio URL) into staging inputs.

Handles chapter extraction from markers, authenticated audio download, and
show-notes formatting with scrubbable bold timestamps.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.config import settings
from core.formatting import format_duration, parse_duration
from core.net import open_https

logger = logging.getLogger("descript_importer")

_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
# `[00:01:23] Title` or `(01:23) Title`
_BRACKET_MARKER = re.compile(rf"^[\(\[]({_TIME})[\)\]]\s*(.*)$")
# `## 00:01:23 Title`, `## [01:23] Title`, or `## Title 01:23`
_HEADING_MARKER = re.compile(rf"^#+\s+(?:\[?({_TIME})\]?\s*)?(.*?)(?:\s+\[?({_TIME})\]?)?$")
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def _chapter(time_str: str, title: str) -> dict[str, Any]:
    """Build a chapter dict from a raw timestamp string and title."""
    seconds = parse_duration(time_str)
    return {"time": format_duration(seconds), "seconds": seconds, "title": title.strip() or "Chapter"}


def extract_chapters_from_markers_or_transcript(transcript_markdown: str) -> list[dict[str, Any]]:
    """Return chapters (sorted by time) from timestamped bracket lines or Markdown headings.

    Headings without any timestamp are ignored, since they can't become chapters.
    """
    chapters = []
    for raw_line in transcript_markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if match := _BRACKET_MARKER.match(line):
            chapters.append(_chapter(match.group(1), match.group(2)))
            continue

        if match := _HEADING_MARKER.match(line):
            leading_time, title, trailing_time = match.groups()
            if time_str := leading_time or trailing_time:
                chapters.append(_chapter(time_str, title))

    chapters.sort(key=lambda c: c["seconds"])
    return chapters


def _is_descript_host(url: str) -> bool:
    """True only for descript.com and its subdomains (not e.g. `evil.com/?descript.com`)."""
    host = (urlparse(url).hostname or "").lower()
    return host == "descript.com" or host.endswith(".descript.com")


def download_audio_from_url(audio_url: str, target_path: Path) -> Path:
    """Stream an HTTPS audio URL to `target_path`, attaching the Descript token only for Descript hosts.

    Downloads to a `.part` file first so an interrupted transfer never leaves a
    truncated MP3 that looks complete.
    """
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    headers = {}
    token = settings.descript_api_bearer or settings.descript_api_key
    if token and _is_descript_host(audio_url):
        headers["Authorization"] = f"Bearer {token}"

    logger.info("Downloading master audio to %s", target_path)
    part_path = target_path.with_suffix(target_path.suffix + ".part")
    with open_https(audio_url, headers) as resp, open(part_path, "wb") as f:
        while chunk := resp.read(_DOWNLOAD_CHUNK_BYTES):
            f.write(chunk)
    os.replace(part_path, target_path)
    return target_path


def format_show_notes_with_chapters(
    summary: str,
    chapters: list[dict[str, Any]],
    guest_info: str | None = None,
    links: list[str] | None = None,
) -> str:
    """Assemble show notes Markdown; chapters use `**MM:SS**` so the site player can scrub to them."""
    sections = []

    if summary:
        sections.append(summary.strip())

    if chapters:
        sections.append("### Chapters\n")
        sections.append("\n".join(f"- **{c['time']}** — {c['title']}" for c in chapters))

    if guest_info:
        sections.append(f"### Guest\n\n{guest_info.strip()}")

    if links:
        sections.append("### Links & Resources\n")
        sections.append("\n".join(f"- {link}" for link in links))

    return "\n\n".join(sections)
