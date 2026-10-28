"""Pure string helpers shared by importers, exporters, and the MCP server.

Nothing here touches the network, disk, or database, which keeps it trivially testable.
"""

import json
import re

# Slugs become filenames, R2 object keys, and URL path segments, so only
# lowercase alphanumerics and hyphens are allowed (no dots, slashes, or spaces).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def format_duration(seconds: float | None) -> str:
    """Render seconds as `HH:MM:SS` when an hour or longer, otherwise `MM:SS`."""
    if not seconds or seconds < 0:
        return "00:00"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_duration(value: str | None) -> float:
    """Parse `HH:MM:SS`, `MM:SS`, or plain seconds into seconds; malformed input yields 0.0."""
    if not value or not value.strip():
        return 0.0
    total = 0.0
    try:
        # Horner-style accumulation handles 1, 2, or 3 colon-separated parts uniformly.
        for part in value.strip().split(":"):
            total = total * 60 + float(part)
    except ValueError:
        return 0.0
    return total


def slugify(text: str) -> str:
    """Convert free text (e.g. an episode title) into a URL-safe slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower().strip())
    return re.sub(r"[-\s_]+", "-", text).strip("-")


def is_slug(text: str) -> bool:
    """True if `text` is safe to use as a slug (filename, object key, URL segment)."""
    return _SLUG_RE.fullmatch(text) is not None


def validate_slug(slug: str) -> str:
    """Return `slug` unchanged if safe for filenames and object keys, else raise ValueError."""
    if not is_slug(slug):
        raise ValueError(f"Invalid slug {slug!r}: use lowercase letters, digits, and hyphens only.")
    return slug


def one_line(text: str) -> str:
    """Collapse line breaks (LF or CRLF) to spaces, for single-line fields like frontmatter descriptions."""
    return text.replace("\r", "").replace("\n", " ")


def op3_url(audio_url: str) -> str:
    """Wrap an audio URL with the OP3 analytics redirect prefix."""
    bare = audio_url.removeprefix("https://").removeprefix("http://")
    return f"https://op3.dev/e/{bare}"


def yaml_str(value: str) -> str:
    """Quote a string for YAML frontmatter.

    JSON string syntax is a strict subset of YAML double-quoted scalars, so
    `json.dumps` escapes quotes, backslashes, and newlines correctly.
    """
    return json.dumps(value, ensure_ascii=False)
