"""Cross-posts a companion blog post to Substack as an unpublished draft, through Substack's private web API.

Substack has no official publishing API, so this uses the community python-substack
library, which signs in with a browser session cookie. That cookie controls the
whole Substack account, so it lives in its own file (SUBSTACK_COOKIES_PATH, mode
0600), never in .env, logs, or tool output. The private API is undocumented and may
change without notice; a failure is reported, never worked around.

The engine only creates drafts: the human publishes, and decides whether to email
subscribers, in the Substack editor. Substack can't declare a canonical URL, so each
draft opens with an "Originally published at" line linking to the original.

The publication's posts and drafts can also be listed, and one deleted at a time
(permanent; logged to `$DATA_DIR/social/substack_deletions.jsonl`).

Save the cookie once (and again whenever it expires):
    uv run python -m exporters.substack_post login
"""

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt
from substack import Api

from core.config import settings
from core.credentials import write_private_json
from core.jsonl import append_record, read_records
from exporters.blog_post import CrosspostSource, crosspost_source

# Library errors are returned to the agent; cap them so a long HTML error page stays readable.
MAX_ERROR_CHARS = 300
# Substack sometimes answers slowly; a hung call must not block an MCP tool forever.
REQUEST_TIMEOUT_SECONDS = 60
# How many of the newest published posts the existing-draft check looks through.
RECENT_POSTS_CHECKED = 50
# Page size when listing published posts (metrics, content management) or drafts; the drafts endpoint
# answers "400 Invalid value" for pages larger than 25.
PAGE_SIZE = 25
# Substack has used both cookie names for the session; sending both covers old and new accounts.
SESSION_COOKIE_NAMES = ("substack.sid", "connect.sid")


class SubstackError(RuntimeError):
    """Substack is not configured (publication URL or session cookie), or Substack rejected or didn't answer a request."""


def _publication_url() -> str:
    """Return SUBSTACK_PUBLICATION_URL, or raise if Substack drafts aren't configured."""
    if not settings.substack_publication_url:
        raise SubstackError("Substack is off: set SUBSTACK_PUBLICATION_URL (e.g. https://name.substack.com).")
    return settings.substack_publication_url


def _cookies_file() -> Path:
    """Return the session cookie file, or raise with the command that creates it."""
    path = settings.substack_cookies_path.expanduser()
    if not path.is_file():
        raise SubstackError(f"No Substack session cookie at {path}; run `uv run python -m exporters.substack_post login`.")
    return path


def _detail(exc: Exception) -> str:
    """A library error as short text; only this part is capped, so the fix that follows it is always shown."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def _client(publication_url: str | None) -> Api:
    """Sign in with the saved session cookie, on `publication_url` (or the account's primary publication if None)."""
    cookies_file = _cookies_file()
    try:
        return Api(cookies_path=str(cookies_file), publication_url=publication_url, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        # The library raises its own, requests', and ValueError exceptions; all of them mean sign-in failed.
        raise SubstackError(
            f"Substack sign-in failed ({_detail(exc)}); the cookie may have expired, "
            "so run `uv run python -m exporters.substack_post login` again."
        ) from exc


def inspect_body(markdown: str) -> list[str]:
    """Refuse images that aren't https URLs, and return warnings for content Substack's editor can't show.

    python-substack uploads any image path that exists on disk, so an image like
    `![x](notes/secret.png)` would send a local file to Substack; only web images pass.
    """
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    inline_children = [child for token in tokens if token.type == "inline" for child in token.children or []]
    images = [str(child.attrGet("src") or "") for child in inline_children if child.type == "image"]
    if local := [src for src in images if not src.startswith("https://")]:
        raise ValueError(f"Images must be https URLs, or Substack would get a local file: {local[:3]}")

    # The library's converter has no tables and drops raw HTML, so the human must fix those in the draft.
    warnings = []
    if tables := sum(token.type == "table_open" for token in tokens):
        warnings.append(f"{tables} table(s) arrive as plain text (Substack has no tables); fix them in the draft.")
    raw_html = sum(token.type == "html_block" for token in tokens) + sum(c.type == "html_inline" for c in inline_children)
    if raw_html:
        warnings.append(f"{raw_html} raw HTML element(s) are dropped; check the draft.")
    return warnings


def _attributed_body(source: CrosspostSource) -> str:
    """The post body, opened by a line crediting the original, since Substack has no canonical URL setting."""
    host = urlparse(source.canonical_url).hostname or source.canonical_url
    return f"*Originally published at [{host}]({source.canonical_url}).*\n\n{source.body_markdown}"


def _edit_url(draft_id: Any) -> str:
    """The Substack editor page for a draft, where the human reviews and publishes it."""
    return f"{_publication_url()}/publish/post/{draft_id}"


def _posts_in(response: Any) -> list[dict[str, Any]]:
    """The post list from a drafts or published-posts response, which Substack returns bare or under `posts`."""
    posts = response.get("posts", []) if isinstance(response, dict) else response
    return [post for post in posts or [] if isinstance(post, dict)]


def _logged_draft(slug: str) -> dict[str, Any] | None:
    """The engine's own record of a Substack draft of this post, if it made one and hasn't since deleted it."""
    deleted = {record.get("post_id") for record in read_records(settings.substack_deletion_log_path)}
    for record in reversed(read_records(settings.substack_post_log_path)):
        if record.get("slug") == slug and record.get("draft_id") and record["draft_id"] not in deleted:
            return {"draft_id": record["draft_id"], "edit_url": record["edit_url"]}
    return None


def _remote_draft(api: Api, source: CrosspostSource) -> dict[str, Any] | None:
    """A draft or recent post of this post on Substack, matched by slug or title (e.g. after a failed call)."""
    try:
        posts = _posts_in(api.get_drafts()) + _posts_in(api.get_published_posts(limit=RECENT_POSTS_CHECKED))
    except Exception as exc:
        raise SubstackError(f"Couldn't list Substack posts to check for a duplicate ({_detail(exc)}).") from exc
    for post in posts:
        title = post.get("draft_title") or post.get("title")
        if post.get("slug") == source.slug or title == source.title:
            return {"draft_id": post.get("id"), "edit_url": _edit_url(post.get("id"))}
    return None


def crosspost_to_substack(slug: str, dry_run: bool = False) -> dict[str, Any]:
    """Create an unpublished Substack draft of the website's blog post `slug`, opened by an attribution line.

    Every check runs before Substack is contacted. `dry_run` reports what would be
    sent, including warnings about content Substack can't show, without signing in.
    If a draft of this post already exists, it's returned instead of creating another.
    """
    # 1. Local checks: the post and its publish state, its images, and the configuration.
    source = crosspost_source(slug)
    body = _attributed_body(source)
    warnings = inspect_body(body)
    publication_url = _publication_url()
    _cookies_file()
    summary = {"slug": slug, "title": source.title, "canonical_url": source.canonical_url, "tags": source.tags}
    if dry_run:
        return {"status": "dry_run", **summary, "warnings": warnings, "body_chars": len(body)}

    # 2. Reuse an existing draft rather than creating a second one: the log first, then Substack itself.
    if existing := _logged_draft(slug):
        return {"status": "exists", **summary, **existing}
    api = _client(publication_url)
    if existing := _remote_draft(api, source):
        return {"status": "exists", **summary, **existing}

    # 3. Create the draft; publish=False is explicit because the library can also publish and email.
    try:
        created = api.create_draft_from_markdown(
            title=source.title,
            subtitle=source.description,
            markdown=body,
            search_engine_title=source.title,
            search_engine_description=source.description,
            slug=slug,
            tags=source.tags,
            publish=False,
        )
    except Exception as exc:
        raise SubstackError(
            f"Substack rejected the draft ({_detail(exc)}). Calling again is safe: an existing draft is found first."
        ) from exc

    draft_id = created["draft"]["id"]
    record = {
        "created_at": datetime.now(UTC).isoformat(),
        **summary,
        "draft_id": draft_id,
        "edit_url": _edit_url(draft_id),
    }
    result: dict[str, Any] = {
        "status": "draft_created",
        **record,
        "warnings": warnings,
        "next_step": "Review the draft in Substack's editor and publish it there (choose whether to email subscribers).",
    }
    # The draft already exists on Substack here. A log failure must not raise, or a retry would look like a new post.
    if log_error := append_record(settings.substack_post_log_path, record):
        result["log_error"] = log_error
    return result


def _all_pages(fetch: Callable[[int], Any], what: str) -> list[dict[str, Any]]:
    """Every item from a paged Substack listing; `fetch(offset)` returns one page of PAGE_SIZE."""
    items: list[dict[str, Any]] = []
    while True:
        try:
            page = _posts_in(fetch(len(items)))
        except Exception as exc:
            raise SubstackError(f"Couldn't list Substack {what} ({_detail(exc)}).") from exc
        items += page
        if len(page) < PAGE_SIZE:
            return items


def _published(api: Api) -> list[dict[str, Any]]:
    """Every published post on the publication, newest first, through an already signed-in client."""
    return _all_pages(lambda offset: api.get_published_posts(offset=offset, limit=PAGE_SIZE), "posts")


def published_posts() -> list[dict[str, Any]]:
    """Every published post on the publication, newest first, with Substack's per-post `stats` (private API)."""
    return _published(_client(_publication_url()))


def _post_summary(post: dict[str, Any], state: str) -> dict[str, Any]:
    """What an agent or human needs to recognize a Substack post (`state` "published") or draft (`state` "draft")."""
    post_id = post.get("id")
    slug = post.get("slug")
    return {
        "post_id": post_id,
        "title": post.get("title") or post.get("draft_title"),
        "state": state,
        "date": post.get("post_date") or post.get("draft_updated_at"),
        "url": f"{_publication_url()}/p/{slug}" if state == "published" and slug else _edit_url(post_id),
    }


def list_posts() -> list[dict[str, Any]]:
    """The publication's published posts and unpublished drafts (private API), newest first within each."""
    api = _client(_publication_url())
    # filter="draft" leaves out published posts, which the drafts endpoint would otherwise list again.
    drafts = _all_pages(
        lambda offset: api.get_drafts(filter="draft", offset=offset, limit=PAGE_SIZE), "drafts"
    )
    return [_post_summary(p, "published") for p in _published(api)] + [_post_summary(d, "draft") for d in drafts]


def delete_post(post_id: int, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one of the publication's posts or drafts and log what it was; `dry_run` only shows which.

    The post is read from the publication first, so an id from anywhere else is refused
    before anything is deleted. Uses Substack's private API, like the editor's Delete button.
    """
    api = _client(_publication_url())
    try:
        post = api.get_draft(post_id)
    except Exception as exc:
        raise SubstackError(f"No post {post_id} in this publication ({_detail(exc)}); nothing was deleted.") from exc
    summary = _post_summary(post, "published" if post.get("is_published") else "draft")
    if dry_run:
        return {"status": "dry_run", **summary}

    try:
        api.delete_draft(post_id)
    except Exception as exc:
        raise SubstackError(f"Substack refused to delete post {post_id} ({_detail(exc)}).") from exc
    result: dict[str, Any] = {"status": "deleted", **summary}
    # The post is already gone; a log failure is reported, never raised.
    record = {"deleted_at": datetime.now(UTC).isoformat(), **result}
    if log_error := append_record(settings.substack_deletion_log_path, record):
        result["log_error"] = log_error
    return result


def save_session_cookie(value: str) -> Path:
    """Write the session cookie to SUBSTACK_COOKIES_PATH, readable only by this user, and return the path."""
    # Browsers show the value URL-encoded (s%3A...); python-substack expects it decoded, as in its cookie-string login.
    cookies = dict.fromkeys(SESSION_COOKIE_NAMES, unquote(value.strip()))
    return write_private_json(settings.substack_cookies_path.expanduser(), cookies)


def substack_publication() -> str:
    """Sign in and return the URL of the publication drafts go to (used by the setup doctor and `login`)."""
    publication_url = _publication_url()
    _client(publication_url)
    return publication_url


def main(argv: list[str]) -> int:
    """`login`: save a session cookie from a hidden prompt, then confirm it signs in."""
    if argv != ["login"]:
        print("Usage: uv run python -m exporters.substack_post login", file=sys.stderr)
        return 2
    print("In a browser signed in to Substack: DevTools → Application → Cookies → https://substack.com,")
    print("copy the value of `substack.sid` (or `connect.sid`). It is read without echoing.")
    value = getpass("Cookie value: ")
    if not value.strip():
        print("No value entered; nothing saved.", file=sys.stderr)
        return 1
    print(f"Saved to {save_session_cookie(value)} (mode 600).")
    try:
        # Without SUBSTACK_PUBLICATION_URL, show the account's primary publication so it can be set.
        if settings.substack_publication_url:
            print(f"Signed in. Drafts will go to {substack_publication()}.")
        else:
            primary = _client(None).publication_url.removesuffix("/api/v1")
            print(f"Signed in. Set SUBSTACK_PUBLICATION_URL in .env, e.g. {primary}")
    except SubstackError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
