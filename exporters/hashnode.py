"""Cross-posts website blog posts to a Hashnode publication, and lists and removes the copies.

Every request goes through Hashnode's official CLI (`@hashnode/cli`, its `gql` command):
the CLI keeps and refreshes its own login (`hashnode login`, or `HASHNODE_PAT` in its
environment), so the engine never reads a Hashnode credential. Hashnode's API needs a
Pro (or Growth) plan on the publication; without one every call answers FORBIDDEN.

A copy keeps the website as the original: `originalArticleURL` is the post's canonical
URL, the SEO title and description are the post's own, the slug is the same, links are
absolute, the post's FAQ becomes a section, the show's footer is appended, and
`publishedAt` is the post's original date when it has one. Before creating anything,
the engine looks for an existing copy (its own log, then the publication's posts by
canonical URL or slug), so a retry can't leave a duplicate.
"""

import json
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from core.config import settings
from core.formatting import slugify
from core.jsonl import append_record, read_records
from exporters.blog_post import crosspost_source, reachable_image, with_footer

# A create that hangs past this may still have gone through; the existing-copy check covers the retry.
CLI_TIMEOUT_SECONDS = 90
# Hashnode's limits: tags per post, and the size of one API request (query plus variables).
MAX_TAGS = 15
MAX_REQUEST_BYTES = 100_000
# Posts per page when listing (Hashnode caps connections at 100) and pages read before giving up.
PAGE_SIZE = 50
MAX_PAGES = 40
# Hashnode tag slugs: lowercase letters and digits in dash-separated words.
_TAG_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Error text is returned to the agent; cap it so a long dump stays readable.
MAX_ERROR_CHARS = 300

PUBLICATION_QUERY = """query ($host: String!) {
  publication(host: $host) {
    id title url
    aeoSettings { isEnabled llmsTxtEnabled crawlers { gptBot claudeBot perplexityBot googleExtended ccBot } }
  }
}"""
POSTS_QUERY = """query ($host: String!, $first: Int!, $after: String) {
  publication(host: $host) {
    id
    posts(first: $first, after: $after) {
      edges { node { id title slug url canonicalUrl publishedAt views } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
PUBLISH_MUTATION = "mutation ($input: PublishPostInput!) { publishPost(input: $input) { post { id slug url } } }"
DRAFT_MUTATION = "mutation ($input: CreateDraftInput!) { createDraft(input: $input) { draft { id slug } } }"
REMOVE_MUTATION = "mutation ($input: RemovePostInput!) { removePost(input: $input) { post { id } } }"
UPDATE_MUTATION = "mutation ($input: UpdatePostInput!) { updatePost(input: $input) { post { id url } } }"


class HashnodeError(RuntimeError):
    """Hashnode is not configured, its CLI is missing or logged out, or Hashnode refused or didn't answer."""


def cli_command() -> list[str]:
    """Return the Hashnode CLI binary; raise if Hashnode isn't configured or the CLI can't be found."""
    if not settings.hashnode_publication_host:
        raise HashnodeError("Hashnode is off: set HASHNODE_PUBLICATION_HOST (e.g. blog.example.com).")
    binary = settings.hashnode_cli_path or (Path(found) if (found := shutil.which("hashnode")) else None)
    if binary is None or not binary.is_file():
        raise HashnodeError(f"Hashnode CLI not found at {binary or 'PATH'}; install @hashnode/cli or set HASHNODE_CLI_PATH.")
    return [str(binary)]


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one GraphQL request through the CLI and return its `data`; raise HashnodeError with Hashnode's message."""
    variables_json = json.dumps(variables or {}, ensure_ascii=False)
    # Hashnode rejects larger requests; refusing here gives a clearer message than its 413.
    if len(query.encode()) + len(variables_json.encode()) > MAX_REQUEST_BYTES:
        raise HashnodeError(f"The request is over Hashnode's {MAX_REQUEST_BYTES // 1000} KB limit; shorten the post.")
    try:
        completed = subprocess.run(
            [*cli_command(), "gql", query, "--vars", variables_json],
            capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        raise HashnodeError(f"Hashnode didn't answer within {CLI_TIMEOUT_SECONDS}s; a create may still have gone through.") from None
    if completed.returncode != 0:
        # The CLI prints one "error: <message>" line, Hashnode's own text (e.g. the Pro-plan refusal) included.
        detail = (completed.stderr or completed.stdout).strip().removeprefix("error: ").replace("\n", " ")
        raise HashnodeError(f"Hashnode: {detail or f'CLI exit {completed.returncode}'}"[:MAX_ERROR_CHARS])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HashnodeError("The Hashnode CLI returned output that isn't JSON.") from exc


def whoami() -> str:
    """The Hashnode username the CLI is logged in as (used by the setup doctor)."""
    return str(gql("query { me { username } }")["me"]["username"])


def _publication_data(query: str, **variables: Any) -> dict[str, Any]:
    """Run a query about the configured publication and return its `publication` object; raise if there's none."""
    found = gql(query, {"host": settings.hashnode_publication_host, **variables}).get("publication")
    if not found:
        raise HashnodeError(f"No Hashnode publication at {settings.hashnode_publication_host}; check HASHNODE_PUBLICATION_HOST.")
    return found


def publication() -> dict[str, Any]:
    """The configured publication's id, title, URL, and AEO settings."""
    return _publication_data(PUBLICATION_QUERY)


def _post_summary(post: dict[str, Any]) -> dict[str, Any]:
    """The fields of a Hashnode post an agent or human needs to recognize it."""
    return {
        "post_id": post.get("id"),
        "title": post.get("title"),
        "slug": post.get("slug"),
        "url": post.get("url"),
        "canonical_url": post.get("canonicalUrl"),
        "published_at": post.get("publishedAt"),
        "views": post.get("views"),
    }


def _posts_page(first: int, after: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the publication's posts, newest first, and the cursor of the next page (None on the last)."""
    connection = _publication_data(POSTS_QUERY, first=first, after=after)["posts"]
    posts = [edge["node"] for edge in connection["edges"]]
    page = connection["pageInfo"]
    return posts, page["endCursor"] if page["hasNextPage"] else None


def all_posts() -> list[dict[str, Any]]:
    """Every published post in the publication (raw API objects), newest first."""
    posts, cursor = _posts_page(PAGE_SIZE, None)
    for _ in range(MAX_PAGES - 1):
        if cursor is None:
            break
        more, cursor = _posts_page(PAGE_SIZE, cursor)
        posts.extend(more)
    return posts


def list_posts(limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """One page (up to 50) of the publication's posts, newest first, plus `next_cursor` for the next page."""
    posts, cursor = _posts_page(max(1, min(limit, PAGE_SIZE)), after)
    return {"posts": [_post_summary(post) for post in posts], "next_cursor": cursor}


def tag_inputs(site_tags: list[str], extra_tags: list[str] | None = None) -> list[dict[str, str]]:
    """Hashnode tags for a copy: the post's own tags as slugs (named as on the site), then `extra_tags` slugs.

    Hashnode creates unknown tags; existing popular ones (e.g. `machine-learning`) put the post in more feeds.
    Raises ValueError for a malformed extra slug or more than 15 tags in total.
    """
    if bad := [tag for tag in extra_tags or [] if not _TAG_SLUG_RE.fullmatch(tag)]:
        raise ValueError(f"Invalid Hashnode tags {bad}: use lowercase words joined by dashes, e.g. 'machine-learning'.")
    tags: dict[str, str] = {}
    for name in site_tags:
        if _TAG_SLUG_RE.fullmatch(slug := slugify(name)):
            tags.setdefault(slug, name)
    for slug in extra_tags or []:
        tags.setdefault(slug, slug)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"Hashnode allows at most {MAX_TAGS} tags; this copy would have {len(tags)}.")
    return [{"slug": slug, "name": name} for slug, name in tags.items()]


def published_at_iso(override: str | None, original: date | None) -> str | None:
    """The copy's `publishedAt`: `override` (ISO 8601) or the post's original date, as UTC ISO; never in the future."""
    if override:
        try:
            moment = datetime.fromisoformat(override)
        except ValueError as exc:
            raise ValueError(f"published_at must be an ISO 8601 date or date-time, got {override!r}.") from exc
    elif isinstance(original, datetime):
        moment = original
    elif isinstance(original, date):
        moment = datetime.combine(original, time())
    else:
        return None
    # A date without a zone is read as UTC, the way the site's date-only pubDate values are meant.
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    if moment > datetime.now(UTC):
        raise ValueError("published_at is in the future; Hashnode copies are backdated to the original, never scheduled.")
    return moment.astimezone(UTC).isoformat()


def find_existing(slug: str, canonical_url: str) -> dict[str, Any] | None:
    """Return the id and URL of an existing copy of this post, from the log or else from the publication."""
    # The log answers without a request (and knows drafts); the publication catches a create whose answer was lost.
    for record in reversed(read_records(settings.hashnode_post_log_path)):
        if record.get("slug") == slug and (record.get("post_id") or record.get("draft_id")):
            return {key: record.get(key) for key in ("post_id", "draft_id", "url")}
    for post in all_posts():
        if post.get("canonicalUrl") == canonical_url or post.get("slug") == slug:
            return {"post_id": post["id"], "url": post["url"]}
    return None


def build_post_input(
    title: str,
    description: str,
    body: str,
    slug: str,
    canonical_url: str,
    tags: list[dict[str, str]],
    published_at: str | None = None,
    series_id: str | None = None,
    cover_image: str | None = None,
) -> dict[str, Any]:
    """A copy's PublishPostInput, less `publicationId`: the original stays canonical, and SEO fields come from it.

    Shared by `crosspost_to_hashnode` (posts on the website) and show scripts that copy posts
    first published elsewhere (their own canonical URL and date).
    """
    optional = {"publishedAt": published_at, "seriesId": series_id, "coverImage": cover_image, "ogImage": cover_image}
    return {
        "title": title,
        "subtitle": description,
        "contentMarkdown": body,
        "slug": slug,
        "tags": tags,
        "originalArticleURL": canonical_url,
        "metaTitle": title,
        "metaDescription": description,
        "enableToc": True,
        **{key: value for key, value in optional.items() if value},
    }


def _draft_input(post_input: dict[str, Any]) -> dict[str, Any]:
    """The same copy as a CreateDraftInput, which nests the SEO, table-of-contents, and cover fields."""
    flat = dict(post_input)
    meta = {"title": flat.pop("metaTitle"), "description": flat.pop("metaDescription")}
    flat.pop("enableToc")
    if cover := flat.pop("coverImage", None):
        flat["coverImageOptions"] = {"coverImageURL": cover}
        meta["image"] = flat.pop("ogImage")
    return {**flat, "metaTags": meta, "settings": {"enableTableOfContent": True}}


def create_copy(post_input: dict[str, Any], publish: bool) -> dict[str, Any]:
    """Create one copy from `build_post_input`'s fields (live or as a draft) and return its ids and URL; no checks or log."""
    post_input = {**post_input, "publicationId": publication()["id"]}
    if publish:
        created = gql(PUBLISH_MUTATION, {"input": post_input})["publishPost"]["post"]
        return {"post_id": created["id"], "url": created["url"]}
    created = gql(DRAFT_MUTATION, {"input": _draft_input(post_input)})["createDraft"]["draft"]
    return {"draft_id": created["id"], "url": None}


def update_copy(post_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Change fields (e.g. `contentMarkdown`) of one of the publication's posts and return its id and URL.

    The post must be in the configured publication, so an id from anywhere else is refused
    before anything changes. The caller owns the change; the post stays published.
    """
    if not any(p.get("id") == post_id for p in all_posts()):
        raise HashnodeError(f"Post {post_id} isn't one of {settings.hashnode_publication_host}'s posts; nothing changed.")
    return gql(UPDATE_MUTATION, {"input": {**fields, "id": post_id}})["updatePost"]["post"]


def crosspost_to_hashnode(
    slug: str,
    tags: list[str] | None = None,
    dry_run: bool = False,
    publish: bool = False,
    published_at: str | None = None,
    series_id: str | None = None,
    cover_image: str | None = None,
) -> dict[str, Any]:
    """Copy the website's blog post `slug` to Hashnode: live with `publish=True`, else as an unpublished draft.

    Every check runs before the first request; `dry_run` reports what would be sent without
    calling Hashnode. If a copy of this post already exists, it's returned instead of
    creating another, so calling again is always safe.
    """
    # 1. Local checks: the post and its publish state, tags, date, cover, and the CLI configuration.
    source = crosspost_source(slug)
    tag_list = tag_inputs(source.tags, tags)
    published = published_at_iso(published_at, source.published_on)
    if cover_image and not cover_image.startswith("https://"):
        raise ValueError("cover_image must be an https:// URL (host it with podcast_upload_media_asset).")
    cli_command()

    # 2. The copy as Hashnode receives it (create_copy adds the publication id). Without an explicit
    #    cover, the post's preview image is used when it's live, so the copy looks like the original when shared.
    body = with_footer(source.body_markdown, source.canonical_url)
    cover = cover_image or reachable_image(source.image_url)
    post_input = build_post_input(
        source.title, source.description, body, slug, source.canonical_url, tag_list, published, series_id, cover
    )
    summary = {
        "slug": slug,
        "title": source.title,
        "canonical_url": source.canonical_url,
        "tags": [tag["slug"] for tag in tag_list],
        "published_at": published,
        "cover_image": cover,
        "publish": publish,
    }
    if dry_run:
        return {"status": "dry_run", **summary, "body_chars": len(body)}

    # 3. Reuse an existing copy rather than creating a second one.
    if existing := find_existing(slug, source.canonical_url):
        return {"status": "exists", **summary, **existing}

    record = {"created_at": datetime.now(UTC).isoformat(), **summary, "published": publish, **create_copy(post_input, publish)}
    result: dict[str, Any] = {
        "status": "published" if publish else "draft_created",
        **record,
        "next_step": "The copy is live on Hashnode." if publish else "Review the draft under Drafts in the Hashnode dashboard and publish it there.",
    }
    # The copy already exists on Hashnode here. A log failure must not raise, or a retry would look like a new post.
    if log_error := append_record(settings.hashnode_post_log_path, record):
        result["log_error"] = log_error
    return result


def delete_post(post_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Permanently remove one of the publication's posts and log what it was; `dry_run` only shows which.

    The post must be in the configured publication's post list, so an id from anywhere else
    is refused before anything is removed. Hashnode's API can't restore a removed post.
    """
    post = next((p for p in all_posts() if p.get("id") == post_id), None)
    if post is None:
        raise HashnodeError(f"Post {post_id} isn't one of {settings.hashnode_publication_host}'s posts; nothing was removed.")
    summary = _post_summary(post)
    if dry_run:
        return {"status": "dry_run", **summary}

    gql(REMOVE_MUTATION, {"input": {"id": post_id}})
    result: dict[str, Any] = {"status": "removed", **summary}
    # The post is already gone; a log failure is reported, never raised.
    record = {"removed_at": datetime.now(UTC).isoformat(), **result}
    if log_error := append_record(settings.hashnode_deletion_log_path, record):
        result["log_error"] = log_error
    return result
