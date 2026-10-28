"""Cross-posts a companion blog post to dev.to as an unpublished draft.

The engine never publishes on dev.to: it creates a draft, and the human reviews
and publishes it from the dev.to dashboard. Each draft carries a canonical URL
pointing at the original (the post on the show's website, unless its frontmatter
names another), so search engines credit it. Before creating anything, the engine
looks for an existing draft of the same post (in its own log, then among the
account's dev.to articles), so a retry after a timeout can't leave a duplicate.
"""

import json
import re
import urllib.error
from datetime import UTC, datetime
from typing import Any

from core.config import settings
from core.jsonl import append_record, read_records
from core.net import open_https
from exporters.blog_post import crosspost_source

DEVTO_API = "https://dev.to/api"
DASHBOARD_URL = "https://dev.to/dashboard"
# The account's own articles, drafts included; one page covers any personal account.
MY_ARTICLES_PATH = "/articles/me/all?per_page=1000"
# dev.to accepts up to four tags per article, each lowercase letters and digits only.
MAX_TAGS = 4
_TAG_RE = re.compile(r"^[a-z0-9]+$")
# dev.to error text is returned to the agent; cap it so a long dump stays readable.
MAX_ERROR_CHARS = 300


class DevtoError(RuntimeError):
    """Cross-posting is not configured, or dev.to rejected or didn't answer a request."""


def _api_key() -> str:
    """Return DEVTO_API_KEY, or raise if cross-posting isn't configured."""
    if not settings.devto_api_key:
        raise DevtoError("dev.to cross-posting is off: set DEVTO_API_KEY (dev.to → Settings → Extensions).")
    return settings.devto_api_key


def _request(path: str, payload: dict[str, Any] | None = None) -> Any:
    """Call the dev.to API (GET, or POST with a JSON `payload`) and return the decoded JSON.

    The API key is sent only to dev.to and never appears in an error message.
    """
    headers = {
        "api-key": _api_key(),
        "Accept": "application/vnd.forem.api-v1+json",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        with open_https(f"{DEVTO_API}{path}", headers, data) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # dev.to explains a rejection (e.g. an unknown tag) in the body; pass that on, not just the status.
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise DevtoError(f"dev.to HTTP {exc.code}: {detail}"[:MAX_ERROR_CHARS]) from exc
    except OSError as exc:
        # A timeout after a POST leaves the outcome unknown; the existing-draft check makes a retry safe.
        raise DevtoError(f"dev.to didn't answer ({exc}). Calling again is safe: an existing draft is found first.") from exc


def devto_username() -> str:
    """The dev.to account the API key belongs to (used by the setup doctor)."""
    return str(_request("/users/me")["username"])


def validate_tags(tags: list[str] | None) -> list[str]:
    """Return the tags if dev.to accepts them (at most 4, lowercase letters and digits), else raise ValueError."""
    tags = tags or []
    if len(tags) > MAX_TAGS:
        raise ValueError(f"dev.to allows at most {MAX_TAGS} tags; got {len(tags)}.")
    if bad := [tag for tag in tags if not _TAG_RE.fullmatch(tag)]:
        raise ValueError(f"Invalid dev.to tags {bad}: use lowercase letters and digits only, e.g. 'machinelearning'.")
    return tags


def _find_existing_draft(slug: str, canonical_url: str) -> dict[str, Any] | None:
    """Return the id and URL of an existing dev.to article for this post, from the log or else from dev.to."""
    # The log answers without a request; dev.to itself catches a draft whose POST timed out before it was logged.
    for record in reversed(read_records(settings.devto_post_log_path)):
        if record.get("slug") == slug and record.get("article_id"):
            return {"article_id": record["article_id"], "url": record["url"]}
    for article in _request(MY_ARTICLES_PATH):
        if article.get("canonical_url") == canonical_url:
            return {"article_id": article["id"], "url": article["url"]}
    return None


def crosspost_to_devto(slug: str, tags: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Create an unpublished dev.to draft of the website's blog post `slug`, with a canonical URL back to it.

    Every check runs before the first request. `dry_run` reports what would be sent
    without calling dev.to. If a draft of this post already exists, it's returned
    instead of creating another, so calling again is always safe.
    """
    # 1. Local checks: the post and its publish state, the tags, and the configuration.
    source = crosspost_source(slug)
    article_tags = validate_tags(tags)
    _api_key()

    # 2. The article as dev.to receives it: never published.
    article = {
        "title": source.title,
        "description": source.description,
        "body_markdown": source.body_markdown,
        "tags": ", ".join(article_tags),
        "canonical_url": source.canonical_url,
        "published": False,
    }
    summary = {"slug": slug, "title": source.title, "canonical_url": source.canonical_url, "tags": article_tags}
    if dry_run:
        return {"status": "dry_run", **summary, "body_chars": len(source.body_markdown)}

    # 3. Reuse an existing draft rather than creating a second one.
    if existing := _find_existing_draft(slug, source.canonical_url):
        return {"status": "exists", **summary, **existing, "dashboard_url": DASHBOARD_URL}

    created = _request("/articles", {"article": article})
    record = {"created_at": datetime.now(UTC).isoformat(), **summary, "article_id": created["id"], "url": created["url"]}
    result: dict[str, Any] = {
        "status": "draft_created",
        **record,
        "dashboard_url": DASHBOARD_URL,
        "next_step": "Review the draft on dev.to and publish it there.",
    }
    # The draft already exists on dev.to here. A log failure must not raise, or a retry would look like a new post.
    if log_error := append_record(settings.devto_post_log_path, record):
        result["log_error"] = log_error
    return result
