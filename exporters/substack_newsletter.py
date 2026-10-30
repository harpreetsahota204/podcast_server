"""Original Substack newsletter articles: drafted from a Markdown file, then scheduled or sent.

Unlike a cross-post (`exporters.substack_post`), a newsletter is first published on
Substack, so it carries no "Originally published at" line. Its source is
`$DATA_DIR/newsletters/<slug>.md`, which the human writes or approves:

    ---
    title: "..."            # email subject and post title
    subtitle: "..."         # email preview
    seo_title: "..."        # optional search-result title; default the title
    seo_description: "..."  # optional search snippet; default the subtitle. 50-160 characters with the primary phrase
    cover_image: "..."      # optional thumbnail and social preview: a local image (relative to this file) or https URL
    tags: ["..."]
    ---
    The article in Markdown (images must be https URLs).

`draft_newsletter` creates the Substack draft, and on later calls rewrites it from the
file, refusing to overwrite edits made in Substack's editor since the engine last wrote it.
`pull_newsletter` copies those editor edits back into the file, so the file stays the source.
`send_newsletter` schedules the draft (cancellable with `unschedule_newsletter` until it goes
out) or publishes it at once, emailing subscribers unless told not to. Each step is logged
to `$DATA_DIR/social/substack_newsletters.jsonl`. Everything goes through Substack's private
web API (python-substack and the saved session cookie), like the cross-posts.
"""

import difflib
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from substack import Api
from substack.exceptions import SubstackAPIException
from substack.revision import compute_draft_revision

from core.config import settings
from core.discovery import check_blog, gate
from core.formatting import validate_slug, yaml_str
from core.jsonl import append_record, read_records
from exporters.blog_post import read_titled_markdown
from exporters.substack_markdown import UnsupportedNodeError, doc_to_markdown
from exporters.substack_post import (
    SubstackError,
    _client,
    _cookies_file,
    _detail,
    _edit_url,
    _publication_url,
    inspect_body,
)

# Image types Substack's uploader takes for a cover image.
COVER_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# Log events that leave the draft's content as the engine wrote it, and the one that sent it.
DRAFT_EVENTS = ("drafted", "updated", "pulled")
SENT_EVENT = "published"

@dataclass(frozen=True)
class Newsletter:
    """A newsletter article as written in `$DATA_DIR/newsletters/<slug>.md`."""

    slug: str
    title: str
    subtitle: str
    seo_title: str
    seo_description: str
    cover_image: str | None
    tags: list[str]
    body_markdown: str


def newsletter_path(slug: str) -> Path:
    """Where the newsletter `slug` is written (raises ValueError for an unsafe slug)."""
    return settings.newsletters_dir / f"{validate_slug(slug)}.md"


def read_newsletter(slug: str) -> Newsletter:
    """Load the newsletter `slug` from its Markdown file; raise if the file or its title is missing."""
    path = newsletter_path(slug)
    if not path.is_file():
        raise FileNotFoundError(f"No newsletter at {path}; write it there (title, subtitle, tags frontmatter) first.")
    frontmatter, body = read_titled_markdown(path)
    title, subtitle = str(frontmatter["title"]), str(frontmatter.get("subtitle") or "")
    return Newsletter(
        slug=slug,
        title=title,
        subtitle=subtitle,
        # Search results can use different wording from the email; without it they reuse the title and subtitle.
        seo_title=str(frontmatter.get("seo_title") or title),
        seo_description=str(frontmatter.get("seo_description") or subtitle),
        cover_image=_cover_image(path.parent, frontmatter.get("cover_image")),
        tags=[str(tag) for tag in frontmatter.get("tags") or []],
        body_markdown=body,
    )


def _cover_image(base_dir: Path, value: Any) -> str | None:
    """The cover image as an https URL or an absolute path to an existing image file; raise for anything else."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("https://"):
        return text
    path = (base_dir / Path(text).expanduser()).resolve()
    if not path.is_file() or path.suffix.lower() not in COVER_IMAGE_SUFFIXES:
        allowed = "/".join(COVER_IMAGE_SUFFIXES)
        raise ValueError(f"cover_image must be an https URL or an existing {allowed} file: {path}")
    return str(path)


def _history(slug: str) -> list[dict[str, Any]]:
    """The engine's log entries for this newsletter, oldest first, minus drafts the engine has since deleted."""
    deleted = {record.get("post_id") for record in read_records(settings.substack_deletion_log_path)}
    return [
        record for record in read_records(settings.substack_newsletter_log_path)
        if record.get("slug") == slug and record.get("draft_id") not in deleted
    ]


def _latest_draft(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest drafted/updated entry: the draft's id and the revision the engine last wrote."""
    return next((record for record in reversed(history) if record.get("event") in DRAFT_EVENTS), None)


def _log(slug: str, event: str, draft_id: Any, **fields: Any) -> dict[str, str]:
    """Append one event to the newsletter log; return `{"log_error": ...}` if the write failed, else `{}`."""
    record = {"at": datetime.now(UTC).isoformat(), "event": event, "slug": slug, "draft_id": draft_id, **fields}
    # The action already happened on Substack; a failed write is reported in the result, never raised.
    log_error = append_record(settings.substack_newsletter_log_path, record)
    return {"log_error": log_error} if log_error else {}


def _remote_draft(api: Api, draft_id: Any) -> dict[str, Any] | None:
    """The draft as Substack holds it now, or None if it was deleted there; raise SubstackError if Substack fails."""
    try:
        return api.get_draft(draft_id)
    except Exception as exc:
        # A 404 means the human deleted the draft in Substack's editor, which the engine's logs can't see.
        if isinstance(exc, SubstackAPIException) and exc.status_code == 404:
            return None
        raise SubstackError(f"Couldn't read Substack draft {draft_id} ({_detail(exc)}).") from exc


def _live_draft(api: Api, draft_id: Any, slug: str) -> dict[str, Any]:
    """The draft as Substack holds it now; raise ValueError if it was deleted there."""
    if (draft := _remote_draft(api, draft_id)) is None:
        raise ValueError(
            f"Draft {draft_id} of '{slug}' was deleted on Substack; create a new one with podcast_draft_newsletter."
        )
    return draft


def _draft_title(draft: dict[str, Any]) -> str:
    """The title shown in Substack's editor; drafts keep it in `draft_title`, published posts in `title`."""
    return str(draft.get("draft_title") or draft.get("title") or "")


def content_fingerprint(draft: dict[str, Any]) -> str:
    """A hash of what readers see in a draft: title, subtitle, and the body's node types, text, and links.

    Substack's editor re-saves a draft when it's opened, changing its timestamp and internal node
    attributes, so the library's revision check reports a conflict even when nobody edited anything.
    This fingerprint ignores those attributes, so it changes only when the words, structure, or links do.
    """
    body = draft.get("draft_body") or {}
    if isinstance(body, str):
        body = json.loads(body)
    parts = [_draft_title(draft), draft.get("draft_subtitle") or ""]

    def walk(node: dict[str, Any]) -> None:
        """Collect each node's type, text, and link targets, depth first."""
        parts.append(node.get("type", ""))
        parts.append(node.get("text", ""))
        parts.extend(str((mark.get("attrs") or {}).get("href", "")) for mark in node.get("marks") or [])
        for child in node.get("content") or []:
            walk(child)

    walk(body)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _written_state(api: Api, draft_id: Any) -> dict[str, Any]:
    """The revision and content fingerprint the engine just wrote, for the next rewrite's conflict check.

    Returns {} if the draft can't be read back; a later rewrite then asks for overwrite_editor_changes.
    """
    try:
        draft = api.get_draft(draft_id)
    except Exception:
        return {}
    return {"revision": compute_draft_revision(draft), "content": content_fingerprint(draft)}


def _rewrite_draft(
    api: Api, newsletter: Newsletter, logged: dict[str, Any], remote: dict[str, Any], overwrite: bool
) -> None:
    """Replace the draft's body, search fields, and tags with the file's, then its title and subtitle.

    `logged` is the engine's last write to the draft. If the readable content still matches it, the
    draft was only re-saved by the editor and is safe to replace; otherwise the library's revision
    check refuses an edit made in Substack's editor, unless `overwrite` is set.
    """
    draft_id = logged["draft_id"]
    expected = logged.get("revision")
    # Only re-saved, not edited: the words, structure, and links are what the engine last wrote.
    overwrite = overwrite or logged.get("content") == content_fingerprint(remote)
    if expected is None and not overwrite:
        raise SubstackError(
            f"The engine has no record of draft {draft_id}'s last revision, so it can't tell whether it was edited "
            "in Substack's editor. Nothing was changed; pass overwrite_editor_changes=true to replace it with the file."
        )
    try:
        # Images are re-uploaded from the file each time, so they never need to match the draft's copies.
        api.update_draft_from_markdown(
            draft_id,
            newsletter.body_markdown,
            search_engine_title=newsletter.seo_title,
            search_engine_description=newsletter.seo_description,
            slug=newsletter.slug,
            tags=newsletter.tags,
            expected_revision=expected,
            allow_conflict=overwrite,
            allow_image_replacement=True,
        )
        # The library's update keeps the old title, so the editor's title and subtitle are set directly.
        api.put_draft(draft_id, draft_title=newsletter.title, draft_subtitle=newsletter.subtitle)
    except Exception as exc:
        raise SubstackError(
            f"Substack refused the update to draft {draft_id} ({_detail(exc)}). If the draft was edited in Substack's "
            "editor, copy those edits into the file, or pass overwrite_editor_changes=true to discard them."
        ) from exc


def _set_cover_image(api: Api, draft_id: Any, cover_image: str) -> str | None:
    """Set the draft's cover image (its thumbnail and social preview); return an error message instead of raising.

    A local file is uploaded to Substack first. The draft exists by now, so a failure here is reported
    with the result rather than raised, and the human can set the image in the editor instead.
    """
    try:
        url = cover_image if cover_image.startswith("https://") else api.get_image(cover_image)["url"]
        api.put_draft(draft_id, cover_image=url)
    except Exception as exc:
        return f"Couldn't set the cover image ({_detail(exc)}); upload it in the editor's post settings instead."
    return None


def draft_newsletter(
    slug: str,
    dry_run: bool = False,
    discovery_override: str | None = None,
    overwrite_editor_changes: bool = False,
) -> dict[str, Any]:
    """Create the Substack draft of newsletter `slug` from its file, or rewrite the existing draft from it.

    Checks run before Substack is contacted: the file, the discovery brief (the search title
    and description, unless `discovery_override` gives the human's reason), https-only images,
    the cover image, and the configuration. A newsletter already sent is refused; write a new
    one under a new slug.
    """
    # 1. Local checks: the file, its discovery fields, its images, the configuration, and whether it went out already.
    newsletter = read_newsletter(slug)
    # Search engines show the SEO title and description, so those are what the brief checks.
    override = gate(
        "newsletter", slug, discovery_override,
        lambda brief: check_blog(brief, newsletter.seo_title, newsletter.seo_description),
    )
    warnings = inspect_body(newsletter.body_markdown)
    publication_url = _publication_url()
    _cookies_file()
    history = _history(slug)
    if any(record.get("event") == SENT_EVENT for record in history):
        raise ValueError(f"Newsletter '{slug}' was already sent; write a new newsletter under a new slug.")
    existing = _latest_draft(history)
    summary = {
        "slug": slug,
        "title": newsletter.title,
        "subtitle": newsletter.subtitle,
        "seo_title": newsletter.seo_title,
        "seo_description": newsletter.seo_description,
        "cover_image": newsletter.cover_image,
        "tags": newsletter.tags,
    }
    if dry_run:
        action = "update" if existing else "create"
        return {"status": "dry_run", "action": action, **summary, "warnings": warnings,
                "body_chars": len(newsletter.body_markdown)}

    # 2. Rewrite the draft the engine already made, after checking it hasn't been published from the editor.
    api = _client(publication_url)
    remote = _remote_draft(api, existing["draft_id"]) if existing else None
    if existing and remote is not None:
        draft_id = existing["draft_id"]
        if remote.get("is_published"):
            raise ValueError(f"Draft {draft_id} of '{slug}' is already published on Substack; nothing was changed.")
        _rewrite_draft(api, newsletter, existing, remote, overwrite_editor_changes)
        event = "updated"
    # 3. Or create it (also when the human deleted the earlier draft on Substack); publish=False is explicit
    #    because the library can also publish and email.
    else:
        try:
            created = api.create_draft_from_markdown(
                title=newsletter.title,
                subtitle=newsletter.subtitle,
                markdown=newsletter.body_markdown,
                search_engine_title=newsletter.seo_title,
                search_engine_description=newsletter.seo_description,
                slug=slug,
                tags=newsletter.tags,
                publish=False,
            )
        except Exception as exc:
            raise SubstackError(
                f"Substack rejected the draft ({_detail(exc)}). Check podcast_list_substack_posts for a half-made "
                "draft before calling again."
            ) from exc
        draft_id = created["draft"]["id"]
        event = "drafted"

    # 4. The cover image goes on last; a failure is reported, because the draft itself is already saved.
    cover_error = _set_cover_image(api, draft_id, newsletter.cover_image) if newsletter.cover_image else None

    # 5. Record the revision just written, so the next update can tell whether the editor changed the draft.
    edit_url = _edit_url(draft_id)
    log_error = _log(
        slug, event, draft_id,
        edit_url=edit_url, title=newsletter.title, discovery_override=override, **_written_state(api, draft_id),
    )
    return {
        "status": f"draft_{event}",
        **summary,
        "draft_id": draft_id,
        "edit_url": edit_url,
        "warnings": warnings,
        **({"cover_image_error": cover_error} if cover_error else {}),
        **log_error,
        "next_step": "Have the human review the draft at edit_url, then podcast_send_newsletter with its exact title.",
    }


def _parse_send_at(send_at: str) -> datetime:
    """An ISO 8601 time with a UTC offset, in the future; raise ValueError otherwise."""
    try:
        when = datetime.fromisoformat(send_at.strip())
    except ValueError as exc:
        raise ValueError(f"send_at must be an ISO 8601 time, e.g. 2026-11-03T09:00:00-05:00; got {send_at!r}.") from exc
    # A time without an offset would be read in Substack's time zone, not the human's.
    if when.utcoffset() is None:
        raise ValueError(f"send_at needs a UTC offset (e.g. -05:00 or Z); got {send_at!r}.")
    if when <= datetime.now(UTC):
        raise ValueError(f"send_at {when.isoformat()} is in the past; nothing was scheduled.")
    return when


def send_newsletter(
    slug: str,
    confirm_title: str,
    send_at: str | None = None,
    send_email: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Schedule newsletter `slug` for `send_at`, or publish it now when `send_at` is None.

    `confirm_title` must equal the draft's current title in Substack, so only the draft the
    human reviewed goes out. Subscribers are emailed unless `send_email` is false; for a
    schedule, that choice is saved on the draft and read back before scheduling. A newsletter
    already sent (logged, or found published on Substack) is reported, never sent again.
    """
    # 1. Local checks: a draft the engine made, not yet sent, and a valid time.
    history = _history(slug)
    if sent := next((record for record in history if record.get("event") == SENT_EVENT), None):
        return {"status": "already_sent", "slug": slug, "draft_id": sent["draft_id"], "url": sent.get("url")}
    draft = _latest_draft(history)
    if draft is None:
        raise ValueError(f"No newsletter draft for '{slug}'; create one with podcast_draft_newsletter first.")
    when = _parse_send_at(send_at) if send_at else None

    # 2. Read the draft from Substack: refuse a changed title, and never send a published post again.
    draft_id = draft["draft_id"]
    api = _client(_publication_url())
    remote = _live_draft(api, draft_id, slug)
    if remote.get("is_published"):
        return {"status": "already_published", "slug": slug, "draft_id": draft_id, "edit_url": _edit_url(draft_id)}
    title = _draft_title(remote)
    if confirm_title.strip() != title:
        raise ValueError(
            f"confirm_title doesn't match the draft's current title {title!r}; nothing was sent. "
            "Show the human the draft and pass its exact title."
        )
    summary = {
        "slug": slug,
        "draft_id": draft_id,
        "title": title,
        "edit_url": _edit_url(draft_id),
        "mode": "schedule" if when else "send_now",
        "send_at": when.isoformat() if when else None,
        "send_email": send_email,
    }
    if dry_run:
        return {"status": "dry_run", **summary}

    # 3a. Schedule: save the email choice on the draft (Substack reads it at release time), confirm it
    #     stuck, then schedule. It can be cancelled until then.
    if when:
        try:
            api.put_draft(draft_id, should_send_email=send_email)
            if api.get_draft(draft_id).get("should_send_email") != send_email:
                raise RuntimeError("the draft's email setting didn't change")
            api.schedule_draft(draft_id, when)
        except Exception as exc:
            raise SubstackError(
                f"Substack refused to schedule draft {draft_id} ({_detail(exc)}); nothing was scheduled."
            ) from exc
        log_error = _log(slug, "scheduled", draft_id, title=title, send_at=summary["send_at"], send_email=send_email)
        return {
            "status": "scheduled",
            **summary,
            **log_error,
            "next_step": f"It goes out at {summary['send_at']}; podcast_unschedule_newsletter cancels it before then.",
        }

    # 3b. Publish now, the same two calls as the editor's Publish button; the email can't be recalled.
    try:
        api.prepublish_draft(draft_id)
        published = api.publish_draft(draft_id, send=send_email, share_automatically=False)
    except Exception as exc:
        raise SubstackError(
            f"Substack refused to publish draft {draft_id} ({_detail(exc)}). Before retrying, check "
            "podcast_list_substack_posts: if it shows as published, it already went out."
        ) from exc
    # Substack answers with the published post; its slug can differ from ours if the human changed it in the editor.
    post_slug = (published.get("slug") if isinstance(published, dict) else None) or remote.get("slug") or slug
    url = f"{_publication_url()}/p/{post_slug}"
    log_error = _log(slug, SENT_EVENT, draft_id, title=title, url=url, send_email=send_email)
    return {"status": "published", **summary, "url": url, **log_error}


def _with_editor_fields(text: str, title: str, subtitle: str, body: str) -> str:
    """The newsletter file with its title, subtitle, and body replaced; every other frontmatter line is kept."""
    _, frontmatter, _ = text.split("---\n", 2)
    fields = {"title": title, "subtitle": subtitle}
    lines = []
    for line in frontmatter.splitlines():
        key = line.split(":", 1)[0]
        lines.append(f"{key}: {yaml_str(fields.pop(key))}" if key in fields else line)
    # A field the file didn't have yet (e.g. a subtitle first written in the editor) is added.
    lines += [f"{key}: {yaml_str(value)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


def pull_newsletter(slug: str, dry_run: bool = False) -> dict[str, Any]:
    """Copy the draft as it stands in Substack's editor (body, title, subtitle) back into its Markdown file.

    Search fields, cover image, and tags in the file are kept. Content Markdown can't hold (a
    button, embed, or footnote added in the editor) is refused by name rather than dropped. The
    pull is logged like a draft write, so the next draft_newsletter call sees no editor conflict.
    """
    draft = _latest_draft(_history(slug))
    if draft is None:
        raise ValueError(f"No newsletter draft for '{slug}'; create one with podcast_draft_newsletter first.")
    path = newsletter_path(slug)
    draft_id = draft["draft_id"]
    api = _client(_publication_url())
    remote = _live_draft(api, draft_id, slug)

    # Convert the editor's document; anything Markdown can't express stops the pull before the file changes.
    body = remote.get("draft_body") or {}
    try:
        markdown = doc_to_markdown(json.loads(body) if isinstance(body, str) else body)
    except UnsupportedNodeError as exc:
        raise ValueError(
            f"Draft {draft_id} has content Markdown can't hold ({exc}); the file was not changed. "
            "Remove it in the editor, or keep editing there and send from the editor's version."
        ) from exc
    title = _draft_title(remote)
    old = path.read_text(encoding="utf-8")
    new = _with_editor_fields(old, title, str(remote.get("draft_subtitle") or ""), markdown)
    changed = sum(line[:2] in ("+ ", "- ") for line in difflib.ndiff(old.splitlines(), new.splitlines()))
    result = {"slug": slug, "draft_id": draft_id, "path": str(path), "title": title, "changed_lines": changed}
    if dry_run or not changed:
        return {"status": "dry_run" if dry_run else "unchanged", **result}

    path.write_text(new, encoding="utf-8")
    log_error = _log(
        slug, "pulled", draft_id, edit_url=_edit_url(draft_id), title=title, **_written_state(api, draft_id)
    )
    return {"status": "pulled", **result, **log_error}


def unschedule_newsletter(slug: str) -> dict[str, Any]:
    """Cancel the scheduled release of newsletter `slug`; the draft stays as it is."""
    draft = _latest_draft(_history(slug))
    if draft is None:
        raise ValueError(f"No newsletter draft for '{slug}'; nothing to unschedule.")
    draft_id = draft["draft_id"]
    api = _client(_publication_url())
    if _live_draft(api, draft_id, slug).get("is_published"):
        raise ValueError(f"Newsletter '{slug}' is already published; a sent email can't be unscheduled.")
    try:
        api.unschedule_draft(draft_id)
    except Exception as exc:
        raise SubstackError(f"Substack refused to unschedule draft {draft_id} ({_detail(exc)}).") from exc
    log_error = _log(slug, "unscheduled", draft_id)
    return {"status": "unscheduled", "slug": slug, "draft_id": draft_id, "edit_url": _edit_url(draft_id), **log_error}
