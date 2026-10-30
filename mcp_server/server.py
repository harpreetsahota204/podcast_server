"""FastMCP server exposing the podcast engine to agents (e.g. Claude Desktop, Cursor) over stdio.

stdout carries the MCP JSON-RPC stream, so nothing in this process may print to
it; all diagnostics go through `logging`, which is routed to stderr.
"""

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from core.audio import inspect_audio
from core.backup import backup_status, start_backup
from core.capabilities import capabilities
from core.config import PROJECT_ROOT, settings
from core.db import CatalogDB
from core.discovery import CHANNEL_RULES, check_feed, gate, make_brief, override_reason, save_brief
from core.episode_assets import prepare_assets, upload_assets
from core.formatting import op3_url, slugify, validate_slug
from core.media_uploader import upload_blog_media
from core.site import deploy_site, rebuild_site
from core.storage import R2StorageClient
from core.gif import make_gif
from core.thumbnail import generate_youtube_thumbnail, render_episode_art, render_newsletter_preview, render_thumbnail
from exporters.blog_post import blog_post_path, check_blog_discovery, create_blog_post
# Imported as modules for the content-management tools: list/delete function names repeat across platforms.
from exporters import devto, instagram, substack_post, youtube
from exporters.devto import crosspost_to_devto
from exporters.instagram import PostType, post_to_instagram
from exporters.social import draft_social_package
from exporters.substack_newsletter import draft_newsletter, pull_newsletter, send_newsletter, unschedule_newsletter
from exporters.substack_post import crosspost_to_substack
from exporters.x_post import post_to_x
from exporters.youtube import PrivacyStatus, set_video_thumbnail, upload_video_to_youtube
from importers.descript import (
    download_audio_from_url,
    extract_chapters_from_markers_or_transcript,
    format_show_notes_with_chapters,
)
from metrics.analysis import episode_metrics, summarize
from metrics.collect import run_collection
from metrics.report import write_report

logger = logging.getLogger("podcast_server")

# Sent to every client when it connects, so an agent gets the rules even if nobody points it at the docs.
INSTRUCTIONS = f"""\
Self-hosted podcast publishing engine. Before any podcast task, read the show playbook \
({settings.data_dir / "PLAYBOOK.md"}) and the engine's runbook ({PROJECT_ROOT / "AGENTS.md"}); \
the playbook wins on anything show-specific.
Rules that always apply:
- Nothing goes public without explicit human approval of the exact content. Public tools: podcast_publish, \
podcast_update_episode, podcast_deploy_site, podcast_post_to_x, podcast_post_to_instagram, \
podcast_publish_to_youtube, podcast_set_youtube_thumbnail, podcast_delete_instagram_post, podcast_delete_youtube_video, \
podcast_unpublish_devto_article, podcast_delete_substack_post, podcast_send_newsletter.
- Removing content: list first, then remove only items the human named, one call per item, after a dry run they saw. \
The podcast_delete_* tools are permanent; podcast_unpublish_devto_article only turns an article back into a draft.
- Episode GUIDs and slugs are permanent, and excluded episodes stay excluded. When a tool refuses, report why; \
never work around it.
- If a public call fails, read {settings.data_dir / "social"}/*.jsonl before retrying, so nothing is posted twice.
- Use these tools instead of editing catalog.db, the website repo, or R2 directly.
- When the human gives a URL, video id, slug, or post id, resolve that exact identifier before using it. Never \
choose an episode by topic, recency, or title similarity. If the exact lookup finds nothing, say so and ask. Name the \
matched episode (guest and slug) in your first reply, before drafting anything.
- After publishing or editing an episode, run podcast_backup.
- podcast_crosspost_to_devto and podcast_crosspost_to_substack only create unpublished drafts, and only for a blog \
post already live on the site; the human publishes each draft on its platform.
- Newsletters: podcast_draft_newsletter drafts $DATA_DIR/newsletters/<slug>.md on Substack; the human reviews the \
draft. If they edited it in Substack, run podcast_pull_newsletter before changing the file, so their edits are kept. \
podcast_send_newsletter schedules it (recommend this: podcast_unschedule_newsletter cancels it until it goes \
out) or, only when the human asks for it, publishes and emails it at once. Dry run first, and pass the draft's exact \
title as confirm_title.
- Never ask the human to paste a password, token, or session cookie into chat, and never read credential files. When a \
tool says a feature is off or a login expired, relay its message and the matching setup step in \
{PROJECT_ROOT / "ADOPTING.md"}.
Working with the human:
- Ask before acting when an identifier didn't resolve exactly; the request reads two ways that lead to different \
work; the next step is public or can't be undone and its exact content isn't approved; it would mean bypassing a tool \
(raw API calls, direct SQL, hand-editing generated files, working around a refusal); or the work has grown past the \
request.
- Ask one question at a time, with your recommended answer. For low-stakes choices, state your default and continue.
- Without being asked, raise risks, better approaches, missing pieces, and capability gaps in a sentence or two.
- When the human asks what's possible, or before relying on a channel, call podcast_capabilities and show the result.
- End every reply with one concrete next step, yours or the human's."""

mcp = FastMCP("podcast-server", instructions=INSTRUCTIONS)

# MCP tool annotations: clients use them to decide which calls run without asking the human.
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
# Only reads from an outside service (e.g. listing the show's posts); changes nothing anywhere.
READ_ONLY_EXTERNAL = {"readOnlyHint": True, "openWorldHint": True}
# Writes local files only (staging, renders, reports); never overwrites anything public.
LOCAL_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# Talks to outside services but only adds or refreshes data, and repeating the call changes nothing more.
ADDITIVE_EXTERNAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
# Changes what the public sees; safe to retry because a repeat has no further effect.
PUBLIC_EDIT = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
# Irreversible public releases; a repeat would publish or post a second time.
PUBLIC_RELEASE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

MAX_LIST_LIMIT = 200


def _open_catalog() -> CatalogDB:
    """Open the catalog configured in settings."""
    return CatalogDB(settings.database_path)


@mcp.tool(annotations=READ_ONLY)
def podcast_status() -> dict[str, Any]:
    """Get overall health and statistics of the podcast catalog and audio hosting."""
    with _open_catalog() as db:
        total_episodes = db.count_episodes()
        synced_audio = db.count_synced_audio()
        latest_episodes = db.list_episodes(limit=3)

    return {
        "podcast_title": settings.podcast_title,
        "feed_url": settings.podcast_feed_url,
        "public_audio_domain": settings.r2_public_domain,
        "use_op3_analytics": settings.use_op3_prefix,
        "r2_storage_ready": R2StorageClient().is_configured(),
        "total_episodes_in_catalog": total_episodes,
        "audio_synced_to_r2": synced_audio,
        "audio_pending_sync": total_episodes - synced_audio,
        "latest_episodes": [
            {
                "slug": ep["slug"],
                "title": ep["title"],
                "published_at": ep["published_at"],
                "duration": ep["duration_formatted"],
                "synced": bool(ep["audio_synced"]),
            }
            for ep in latest_episodes
        ],
    }


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def podcast_capabilities() -> dict[str, Any]:
    """What the engine can do right now: each channel (feed, blog, YouTube, X, Instagram, dev.to, Substack, metrics) with its status (ready / off / needs_attention / broken), the reason, and its tools, plus requests no tool handles. Show it when the human asks what's possible or before relying on a channel. Runs the setup checks, so it takes a few seconds."""
    return capabilities()


@mcp.tool(annotations=READ_ONLY)
def podcast_inspect(audio_path: str) -> dict[str, Any]:
    """Inspect a local audio MP3 file to extract exact duration, file size, bitrate, and sample rate."""
    return inspect_audio(Path(audio_path))


@mcp.tool(annotations=READ_ONLY)
def podcast_list_episodes(limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    """List episodes (newest first, max 200 per call) with publication date, duration, and audio sync status."""
    # Clamp agent-supplied paging so one call can't dump the whole catalog (or pass a negative "no limit").
    with _open_catalog() as db:
        episodes = db.list_episodes(limit=max(1, min(limit, MAX_LIST_LIMIT)), offset=max(0, offset))
    return [
        {
            "id": ep["id"],
            "slug": ep["slug"],
            "title": ep["title"],
            "episode_number": ep["episode_number"],
            "published_at": ep["published_at"],
            "duration": ep["duration_formatted"],
            "file_size_bytes": ep["file_size_bytes"],
            "audio_url": ep["audio_url"],
            "audio_synced": bool(ep["audio_synced"]),
        }
        for ep in episodes
    ]


@mcp.tool(annotations=READ_ONLY)
def podcast_get_episode(slug: str) -> dict[str, Any] | None:
    """Fetch complete metadata, show notes, and audio links for a specific episode by its slug."""
    with _open_catalog() as db:
        return db.get_episode_by_slug(slug)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_rebuild_feed() -> dict[str, Any]:
    """Regenerate the RSS feed and episode pages from the catalog. Astro mode: writes them into the website repo; push with podcast_deploy_site. None mode: uploads the feed to R2, live at once."""
    with _open_catalog() as db:
        return {"status": "success", **rebuild_site(db)}


@mcp.tool(annotations={**LOCAL_WRITE, "destructiveHint": True})  # replaces an existing post with the same slug
def podcast_create_blog(
    slug: str,
    title: str,
    description: str,
    content_markdown: str,
    tags: list[str] | None = None,
    draft: bool = False,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Write a standalone companion blog post into the website's blog directory (astro mode only). Refuses when the title or description (50-160 characters, used as the meta description) misses the slug's discovery brief, unless discovery_override gives the human's reason."""
    output_path = create_blog_post(
        title=title,
        description=description,
        content_markdown=content_markdown,
        slug=slug,
        tags=tags,
        draft=draft,
        discovery_override=discovery_override,
    )
    return {
        "status": "created",
        "file_path": str(output_path),
        "slug": slug,
        "draft": draft,
        "discovery_override": override_reason(discovery_override),
    }


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_stage_discovery(
    slug: str,
    primary_phrase: str,
    secondary_phrases: list[str],
    audience: str,
    question: str,
    entities: list[str],
) -> dict[str, Any]:
    """Save the discovery brief for a piece of content (after the human approves it): the main search phrase, 3-5 secondary phrases, who it's for, the question it answers, and the names people search for (guest, tools, papers). Every publish tool for this slug checks its fields against the brief and refuses on a miss. Restaging replaces the brief. Returns the rules each channel is checked for."""
    brief = make_brief(slug, primary_phrase, secondary_phrases, audience, question, entities)
    return {"status": "staged", "path": str(save_brief(brief)), "brief": asdict(brief), "channel_rules": CHANNEL_RULES}


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_stage_from_descript(
    transcript_markdown: str,
    slug: str,
    audio_download_url: str | None = None,
    local_audio_path: str | None = None,
    guest_name: str | None = None,
    summary: str | None = None,
    links: list[str] | None = None,
) -> dict[str, Any]:
    """
    Stage a release package from a Descript export for human review:
    1. Downloads the master audio to DATA_DIR/raw/<slug>.mp3 (or uses local_audio_path).
    2. Measures duration, file size, and bitrate.
    3. Parses transcript markers into timestamped chapters.
    4. Drafts show notes with chapters, guest info, and links.
    """
    validate_slug(slug)

    # 1. Resolve audio: a Descript download wins over a local path when both are given.
    audio_path: Path | None = None
    if audio_download_url:
        audio_path = download_audio_from_url(audio_download_url, settings.raw_audio_dir / f"{slug}.mp3")
    elif local_audio_path:
        audio_path = Path(local_audio_path).resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Local audio file does not exist: {local_audio_path}")

    # 2. Pre-flight inspection so the review bundle shows real duration and size.
    audio_meta = inspect_audio(audio_path) if audio_path else None

    # 3-4. Chapters from Descript markers, then draft show notes around them.
    chapters = extract_chapters_from_markers_or_transcript(transcript_markdown)
    default_summary = f"A conversation with {guest_name}." if guest_name else ""
    formatted_show_notes = format_show_notes_with_chapters(
        summary=summary or default_summary,
        chapters=chapters,
        guest_info=guest_name,
        links=links,
    )

    return {
        "status": "staged_for_review",
        "slug": slug,
        "audio_file_path": str(audio_path) if audio_path else None,
        "audio_meta": audio_meta,
        "chapter_count": len(chapters),
        "chapters": chapters,
        "draft_show_notes_markdown": formatted_show_notes,
        "next_step": "Human review gate: get explicit approval of title, show notes, and chapters before calling podcast_publish.",
    }


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_publish(
    audio_file_path: str,
    title: str,
    description: str,
    show_notes_markdown: str,
    slug: str | None = None,
    episode_number: int | None = None,
    season: int = 1,
    explicit: bool = False,
    tags: list[str] | None = None,
    blog_content_markdown: str | None = None,
    blog_title: str | None = None,
    blog_draft: bool = False,
    transcript_path: str | None = None,
    episode_image_path: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """
    Publish a new episode end-to-end (only after human approval):
    1. Before any upload: rejects invalid, already-published, or excluded slugs; checks the title and
       summary (and any blog post) against the slug's discovery brief, refusing on a miss unless
       discovery_override gives the human's reason; validates the transcript and artwork; measures the audio.
    2. Uploads the MP3, plus the transcript (.vtt/.srt preferred by Apple, or .txt), a chapters file built
       from the show notes' timestamps, and square 1400-3000 px episode art, then inserts the catalog row
       with a new, permanent GUID.
    3. Optionally writes a companion blog post (astro mode).
    4. Rebuilds the feed (and episode pages) and deploys: R2 feed upload, or a scoped git push.
    """
    ep_slug = validate_slug(slug or slugify(title))

    # Every check that can fail runs before the upload, so a rejected publish leaves nothing behind.
    with _open_catalog() as db:
        if db.get_episode_by_slug(ep_slug):
            raise ValueError(f"Episode '{ep_slug}' is already published; pick a different slug.")
        if db.is_excluded(ep_slug):
            raise ValueError(f"Slug '{ep_slug}' belongs to a permanently excluded episode; pick another.")
    if settings.require_episode_image and not episode_image_path:
        raise ValueError(
            "This show requires episode art (REQUIRE_EPISODE_IMAGE): pass episode_image_path, "
            "a square 1400-3000 px JPEG or PNG. Nothing was uploaded."
        )
    override = gate("feed", ep_slug, discovery_override, lambda brief: check_feed(brief, title, description))
    writes_blog = bool(blog_content_markdown) and settings.site_mode == "astro"
    if writes_blog:
        check_blog_discovery(ep_slug, blog_title or title, description, discovery_override)
    assets = prepare_assets(transcript_path, episode_image_path, show_notes_markdown)
    audio_path = Path(audio_file_path).resolve()
    audio_meta = inspect_audio(audio_path)
    client = R2StorageClient()
    if not client.is_configured():
        raise RuntimeError(client.config_error)

    target_filename = f"{ep_slug}.mp3"
    r2_url = client.upload_file(audio_path, f"episodes/{target_filename}")
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%d%H%M%S")
    extras = upload_assets(client, assets, ep_slug, stamp)

    record = {
        "id": ep_slug,
        # Minted once here and never regenerated: podcast apps track listening state by GUID.
        "guid": f"{settings.episode_guid_prefix}-{ep_slug}-{stamp}",
        "episode_number": episode_number,
        "season": season,
        "title": title,
        "slug": ep_slug,
        "summary": description,
        "content_html": "",
        "content_markdown": show_notes_markdown,
        "published_at": now.isoformat(),
        "original_audio_url": r2_url,
        "audio_filename": target_filename,
        "audio_url": r2_url,
        "audio_op3_url": op3_url(r2_url) if settings.use_op3_prefix else r2_url,
        "duration_seconds": audio_meta["duration_seconds"],
        "duration_formatted": audio_meta["duration_formatted"],
        "file_size_bytes": audio_meta["file_size_bytes"],
        "explicit": int(explicit),
        "cover_image_url": settings.podcast_image_url,
        "tags": json.dumps(tags or []),
        "audio_synced": 1,
        # Transcript, chapters, and episode art (replacing the show art above) when they were given.
        **extras,
    }

    # Optional companion blog post shares the episode slug.
    blog_path: Path | None = None
    if writes_blog:
        blog_path = create_blog_post(
            title=blog_title or title,
            description=description,
            content_markdown=blog_content_markdown,
            slug=ep_slug,
            tags=tags,
            draft=blog_draft,
            discovery_override=discovery_override,
        )

    with _open_catalog() as db:
        db.insert_episode(record)
        site = rebuild_site(db)

    git_deploy = deploy_site(f"feat(podcast): publish episode {ep_slug}", [blog_path] if blog_path else None)

    return {
        "status": "published",
        "slug": ep_slug,
        "guid": record["guid"],
        "title": title,
        "published_at": record["published_at"],
        "audio_url": r2_url,
        "audio_op3_url": record["audio_op3_url"],
        "duration": audio_meta["duration_formatted"],
        "file_size_bytes": audio_meta["file_size_bytes"],
        "rss_feed_path": site["rss_path"],
        "episode_pages_changed": site.get("episode_pages_changed"),
        "feed_url": site.get("feed_url", settings.podcast_feed_url),
        "companion_blog_path": str(blog_path) if blog_path else None,
        "transcript_url": extras.get("transcript_url"),
        "chapters_url": extras.get("chapters_url"),
        "episode_image_url": extras.get("cover_image_url"),
        "discovery_override": override,
        "git_deploy": git_deploy,
    }


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_update_episode(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    show_notes_markdown: str | None = None,
    tags: list[str] | None = None,
    episode_number: int | None = None,
    season: int | None = None,
    explicit: bool | None = None,
) -> dict[str, Any]:
    """Edit a published episode (only after human approval of the new text): title, description, show notes, tags, number, season, or explicit flag. Omitted fields stay as they are; GUID, slug, and audio never change. Rebuilds the feed and pages. Astro mode: push them with podcast_deploy_site. None mode: the feed is live at once."""
    # Parameter names match podcast_publish; map them onto catalog columns, dropping the ones not given.
    columns = {
        "title": title,
        "summary": description,
        "content_markdown": show_notes_markdown,
        "tags": None if tags is None else json.dumps(tags),
        "episode_number": episode_number,
        "season": season,
        "explicit": None if explicit is None else int(explicit),
    }
    changes = {column: value for column, value in columns.items() if value is not None}
    with _open_catalog() as db:
        if not db.update_episode(validate_slug(slug), changes):
            raise ValueError(f"No episode with slug '{slug}'.")
        site = rebuild_site(db)
    return {"status": "updated", "slug": slug, "changed_fields": sorted(changes), **site}


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_deploy_site(message: str, blog_slugs: list[str] | None = None) -> dict[str, Any]:
    """Push the rebuilt feed and episode pages live (only after human approval). Astro mode: commits only engine-generated files on WEBSITE_BRANCH with `message` and pushes; blog_slugs adds posts written by podcast_create_blog. None mode: nothing to do. Use after podcast_update_episode or podcast_rebuild_feed, or to retry a publish whose git_deploy was skipped or failed."""
    blog_paths = [blog_post_path(slug) for slug in blog_slugs or []]
    # deploy_paths skips missing files silently; a mistyped slug must fail loudly instead.
    if missing := [str(path) for path in blog_paths if not path.exists()]:
        raise FileNotFoundError(f"No blog post at: {', '.join(missing)}")
    return deploy_site(message, blog_paths)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_backup(check_only: bool = False) -> dict[str, Any]:
    """Back up audio, catalog, and show assets to the private Hugging Face dataset (HF_BACKUP_REPO). Runs in the background because even an incremental run takes minutes: returns "started" (or "running") at once. Call again with check_only=true for "running", "completed", or "failed" plus the end of the log. Run after every publish or episode edit."""
    return backup_status() if check_only else start_backup()


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_upload_media_asset(
    file_path: str,
    target_filename: str | None = None,
    prefix: str = "media",
) -> dict[str, Any]:
    """Upload blog assets (GIF, PNG, JPEG, WebP, MP4, WebM, MOV) to the public Cloudflare R2 bucket. Refuses to overwrite an existing key. Returns the public URL and a Markdown embed."""
    return upload_blog_media(file_path=file_path, target_filename=target_filename, prefix=prefix)


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_publish_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: PrivacyStatus = "unlisted",
    episode_slug: str | None = None,
    expect_short: bool = False,
    captions_path: str | None = None,
    playlist_id: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Upload a video to the authenticated YouTube channel using resumable chunked upload. Pass episode_slug so the video's metrics are credited to that episode and its discovery brief is checked first: the title names a brief phrase or entity, the primary phrase is in the first 150 characters of the description, a tag contains a brief phrase, and regular videos list at least 3 chapters from 00:00. On a miss it refuses before uploading, unless discovery_override gives the human's reason. Titles over 100 characters, descriptions over 5,000, or tags over 500 in total are refused, not cut. captions_path (.vtt, .srt, or a .txt transcript YouTube times itself) becomes the caption track and playlist_id gets the video after the upload; failures there are reported in the result. Shorts: YouTube files any square or vertical video of up to 3 minutes as a Short (there's no separate upload); set expect_short=true to measure the file first and refuse one that would land as a regular video. Use privacy_status="public" for Shorts: unlisted ones never reach the Shorts feed. Custom thumbnails can't be set on Shorts through the API."""
    return upload_video_to_youtube(
        video_path=video_path,
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
        episode_slug=episode_slug,
        expect_short=expect_short,
        captions_path=captions_path,
        playlist_id=playlist_id,
        discovery_override=discovery_override,
    )


@mcp.tool(annotations=READ_ONLY)
def podcast_draft_social(
    title: str,
    guest_name: str,
    core_tension: str,
    technical_specs: list[str],
    episode_url: str,
    youtube_url: str | None = None,
) -> dict[str, Any]:
    """Draft a long and a short social post for an episode (templates overridable per show)."""
    return draft_social_package(
        title=title,
        guest_name=guest_name,
        core_tension=core_tension,
        technical_specs=technical_specs,
        episode_url=episode_url,
        youtube_url=youtube_url,
    )


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_post_to_x(
    text: str,
    media_path: str | None = None,
    dry_run: bool = False,
    episode_slug: str | None = None,
    alt_text: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Post to X as the configured account (only after explicit human approval of the exact text and media). Attaches one optional image, GIF, or video, which needs alt_text (up to 1,000 characters) unless discovery_override gives the human's reason; dry_run checks everything without posting. Pass episode_slug when promoting an episode so the post's metrics are credited to it. Returns the post id and URL."""
    return post_to_x(
        text=text,
        media_path=media_path,
        dry_run=dry_run,
        episode_slug=episode_slug,
        alt_text=alt_text,
        discovery_override=discovery_override,
    )


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_post_to_instagram(
    media_paths: list[str],
    caption: str = "",
    post_type: PostType = "image",
    dry_run: bool = False,
    episode_slug: str | None = None,
    alt_text: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Publish a feed post to the configured Instagram professional account (only after explicit human approval of the exact image and caption). post_type "image": one PNG, JPEG, or WebP with an aspect ratio between 4:5 and 1.91:1, converted to JPEG and staged on the public R2 domain for Instagram to fetch. Captions: up to 2,200 characters and 30 hashtags; links in captions aren't clickable on Instagram. Captions can't be edited after posting, so the discovery check runs first: alt_text (up to 1,000 characters) and a phrase from episode_slug's brief in the caption's first 125 characters; it refuses on a miss unless discovery_override gives the human's reason. dry_run checks everything (including the image) without uploading or posting. The same image with the same caption is never posted twice: that post is returned instead. Pass episode_slug when promoting an episode. Returns the media id and post URL."""
    return post_to_instagram(
        media_paths=media_paths,
        caption=caption,
        post_type=post_type,
        dry_run=dry_run,
        episode_slug=episode_slug,
        alt_text=alt_text,
        discovery_override=discovery_override,
    )


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def podcast_list_instagram_posts(limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """List the Instagram account's posts, newest first (up to 100 per call): media id, type, date, URL, and the start of the caption. Pass the returned next_cursor as `after` for the next page; it's null on the last page."""
    return instagram.list_posts(limit=limit, after=after)


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_delete_instagram_post(media_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one post from the configured Instagram account (only a post the human explicitly named; run with dry_run=true first and show what will go). Refuses ids that aren't the account's own posts. Carousels are deleted whole; ads can't be deleted. Logs the deleted post, caption included, to $DATA_DIR/social/instagram_deletions.jsonl. Needs the instagram_manage_contents permission on the token."""
    return instagram.delete_post(media_id=media_id, dry_run=dry_run)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def podcast_list_youtube_videos(limit: int = 25, page_token: str | None = None) -> dict[str, Any]:
    """List the YouTube channel's uploads, newest first (up to 50 per call): video id, title, date, privacy, URL, and the episode it's tracked under. Pass the returned next_page_token for the next page; it's null on the last page."""
    return youtube.list_videos(limit=limit, page_token=page_token)


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_delete_youtube_video(video_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one video from the authenticated YouTube channel (only a video the human explicitly named; run with dry_run=true first and show what will go). Refuses videos from other channels. Logs the deleted video, description included, to $DATA_DIR/social/youtube_deletions.jsonl. Needs the youtube.force-ssl scope on the token. Costs 50 quota units."""
    return youtube.delete_video(video_id=video_id, dry_run=dry_run)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def podcast_list_devto_articles() -> list[dict[str, Any]]:
    """List the dev.to account's articles, published and drafts: article id, title, published flag and date, URL, and canonical URL."""
    return devto.list_articles()


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_unpublish_devto_article(article_id: int, dry_run: bool = False) -> dict[str, Any]:
    """Turn one of the dev.to account's published articles back into a draft (only an article the human named; dry_run first). dev.to's API can't delete: the article stays as a draft, and the human deletes it for good on dev.to if they want. Refuses articles that aren't the account's. Logged to $DATA_DIR/social/devto_unpublished.jsonl."""
    return devto.unpublish_article(article_id=article_id, dry_run=dry_run)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def podcast_list_substack_posts() -> list[dict[str, Any]]:
    """List the Substack publication's published posts and unpublished drafts (private API): post id, title, state (published or draft), date, and URL (the editor URL for drafts)."""
    return substack_post.list_posts()


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_delete_substack_post(post_id: int, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one post or draft from the Substack publication (only one the human named; dry_run first and show what will go). Reads it from the publication first, so ids from elsewhere are refused. Uses Substack's private API, like the editor's Delete button. Logged to $DATA_DIR/social/substack_deletions.jsonl."""
    return substack_post.delete_post(post_id=post_id, dry_run=dry_run)


# Additive, not public: it only creates an unpublished draft, which the human publishes on dev.to.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_crosspost_to_devto(blog_slug: str, tags: list[str] | None = None, dry_run: bool = False, publish: bool = False) -> dict[str, Any]:
    """Cross-post a published companion blog post (from podcast_publish, podcast_create_blog, or a standalone blog directory) to dev.to with a canonical URL back to the website (or the post's own `canonical` frontmatter, if it was first published elsewhere) and site-relative links made absolute. Up to 4 tags, lowercase letters and digits only. If publish=True, publishes immediately live; otherwise creates an unpublished draft. If an article of the post already exists, returns it instead of creating another, so retrying is safe. dry_run checks everything without calling dev.to."""
    return crosspost_to_devto(slug=blog_slug, tags=tags, dry_run=dry_run, publish=publish)


# Additive, not public: it only creates an unpublished draft, which the human publishes (and emails) on Substack.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_crosspost_to_substack(blog_slug: str, dry_run: bool = False) -> dict[str, Any]:
    """Cross-post a published companion blog post to Substack as an unpublished draft, opened by an "Originally published at" line linking to the original (Substack has no canonical URL setting), with site-relative links made absolute and the post's tags. Images must be https URLs. Returns warnings for content Substack can't show (tables, raw HTML) so the human can fix the draft. Never publishes or emails: the human does that in Substack's editor. If a draft of the post already exists, returns it instead of creating another, so retrying is safe. dry_run checks everything without contacting Substack. Uses Substack's private API, which can change; report failures as they are."""
    return crosspost_to_substack(slug=blog_slug, dry_run=dry_run)


# Not public: the draft stays unpublished. A rewrite refuses to overwrite edits made in Substack's editor.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_draft_newsletter(
    slug: str,
    dry_run: bool = False,
    discovery_override: str | None = None,
    overwrite_editor_changes: bool = False,
) -> dict[str, Any]:
    """Create an unpublished Substack draft of the newsletter in $DATA_DIR/newsletters/<slug>.md (frontmatter: title, subtitle, tags, and optional seo_title, seo_description, and cover_image, a local image or https URL used as the post thumbnail and social preview; Markdown body with https images only), or rewrite the draft this tool made earlier from the file. Refuses when the SEO title or description (default: title and subtitle; the description must be 50-160 characters) misses the slug's discovery brief, unless discovery_override gives the human's reason. A cover image that can't be set is reported as cover_image_error. A rewrite refuses if the draft was edited in Substack's editor since, unless overwrite_editor_changes=true (only when the human says to discard those edits). Returns the edit_url for the human's review and warnings for content Substack can't show. dry_run checks everything without contacting Substack. Never publishes or emails."""
    return draft_newsletter(
        slug=slug,
        dry_run=dry_run,
        discovery_override=discovery_override,
        overwrite_editor_changes=overwrite_editor_changes,
    )


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_send_newsletter(
    slug: str,
    confirm_title: str,
    send_at: str | None = None,
    send_email: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send the newsletter drafted by podcast_draft_newsletter (only after the human approved the draft and the timing). With send_at (ISO 8601 with a UTC offset, e.g. 2026-11-03T09:00:00-05:00) it schedules the release, cancellable with podcast_unschedule_newsletter until then; email delivery then follows the draft's setting in Substack's editor. Without send_at it publishes at once and emails subscribers (send_email=false publishes to the web only); that can't be undone. confirm_title must equal the draft's current title on Substack. Run with dry_run=true first and show the result. A newsletter already sent is reported, never sent twice. Logged to $DATA_DIR/social/substack_newsletters.jsonl."""
    return send_newsletter(
        slug=slug, confirm_title=confirm_title, send_at=send_at, send_email=send_email, dry_run=dry_run
    )


# Substack is only read; what changes is the local file, whose body is replaced (like podcast_create_blog).
@mcp.tool(annotations={**LOCAL_WRITE, "destructiveHint": True})
def podcast_pull_newsletter(slug: str, dry_run: bool = False) -> dict[str, Any]:
    """Copy the newsletter draft as it stands in Substack's editor (body, title, subtitle) back into $DATA_DIR/newsletters/<slug>.md, keeping the file's SEO fields, cover image, and tags. Use it after the human edits the draft in Substack, before editing the file again, so their edits aren't lost; the next podcast_draft_newsletter then sees no conflict. Refuses, without touching the file, if the draft holds content Markdown can't represent (buttons, embeds, footnotes). dry_run reports how many lines would change."""
    return pull_newsletter(slug=slug, dry_run=dry_run)


# Cancels a pending release only; nothing public changes, and repeating it is harmless.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_unschedule_newsletter(slug: str) -> dict[str, Any]:
    """Cancel the scheduled release of a newsletter scheduled with podcast_send_newsletter; the draft stays on Substack unchanged. Refuses once it has been published."""
    return unschedule_newsletter(slug=slug)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_stage_thumbnail(
    prompt_text: str,
    output_filename: str,
    style: str | None = None,
) -> dict[str, Any]:
    """Step 1 of a thumbnail: builds a 16:9 art prompt (no lettering, empty right side) and returns where to save the rendered art. Does not generate the image; render it with your image model, then call podcast_render_thumbnail."""
    return generate_youtube_thumbnail(prompt_text=prompt_text, output_filename=output_filename, style=style)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_render_thumbnail(
    base_image_path: str,
    headline_lines: list[str],
    slug: str,
    label: str | None = None,
    accent_line: int | None = -1,
) -> dict[str, Any]:
    """Step 2 of a thumbnail: composites 1-4 headline lines (2-3 short words read best; don't repeat the title) and an optional small label onto the art, in the show's style ($DATA_DIR/templates/thumbnail_style.json). accent_line picks the highlighted line (default last; null for none). Writes DATA_DIR/thumbnails/<slug>-final.png (JPEG if over 2 MB). Show the result to the human before setting it."""
    output = settings.thumbnails_dir / f"{validate_slug(slug)}-final.png"
    return render_thumbnail(Path(base_image_path), headline_lines, output, label=label, accent_line=accent_line)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_render_episode_art(
    base_image_path: str,
    headline_lines: list[str],
    slug: str,
    label: str | None = None,
    focus_x: float = 0.33,
) -> dict[str, Any]:
    """Square episode art for podcast apps (the feed's per-episode image), from the same clean art and style as the thumbnail: a square crop centered at focus_x (0-1 of the art's width; raise it if the subject sits right of center), a dark fade across the bottom, the optional label, 1-4 headline lines (use the approved thumbnail headline), and the show's wordmark (`wordmark` in thumbnail_style.json, default the show title). Writes DATA_DIR/thumbnails/<slug>-episode.jpg (2048 px). Show it to the human, then pass it to podcast_publish as episode_image_path."""
    output = settings.thumbnails_dir / f"{validate_slug(slug)}-episode.jpg"
    return render_episode_art(Path(base_image_path), headline_lines, output, label=label, focus_x=focus_x)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_render_newsletter_preview(
    base_image_path: str,
    headline_lines: list[str],
    slug: str,
    label: str | None = None,
    accent_line: int | None = -1,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    mirror: bool = False,
) -> dict[str, Any]:
    """A newsletter's post preview (Substack thumbnail and social card) in the show's thumbnail style: 1456x1048 (14:10), with the label and 1-4 headline lines kept inside the band a 1200x630 social crop shows. Clean art without lettering works best (the show's brand art or a podcast_stage_thumbnail render); focus_x/focus_y (0-1) pick what stays in view, and mirror flips art whose subject sits on the headline side. Writes $DATA_DIR/thumbnails/<slug>-substack.png. Show it to the human, then set it as cover_image in the newsletter file."""
    output = settings.thumbnails_dir / f"{validate_slug(slug)}-substack.png"
    return render_newsletter_preview(
        Path(base_image_path), headline_lines, output,
        label=label, accent_line=accent_line, focus=(focus_x, focus_y), mirror=mirror,
    )


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_make_gif(
    video_path: str,
    name: str,
    start_seconds: float = 0.0,
    duration_seconds: float = 6.0,
    max_mb: float = 2.0,
) -> dict[str, Any]:
    """Cut a short clip (up to 30 s) from a local video into an email-sized GIF at $DATA_DIR/media/<name>.gif, stepping the width (640 down to 320 px, never upscaling) and frame rate down until it's under max_mb (default 2 MB, quick to load in an email). For demo clips in newsletters and blog posts; download the video first. Refuses if even the smallest setting is over budget: use a shorter duration. Needs ffmpeg (FFMPEG_PATH or PATH). Show the result, then host it with podcast_upload_media_asset."""
    return make_gif(
        Path(video_path), name, start_seconds=start_seconds, duration_seconds=duration_seconds, max_mb=max_mb
    )


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_set_youtube_thumbnail(video_id: str, thumbnail_path: str, episode_slug: str | None = None) -> dict[str, Any]:
    """Sets a video's custom thumbnail on YouTube (public; only after the human approved the exact image). JPEG/PNG up to 2 MB. Logs the change so the metrics dashboard marks it; episode_slug defaults to the video's tracked episode."""
    return set_video_thumbnail(video_id=video_id, thumbnail_path=thumbnail_path, episode_slug=episode_slug)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_metrics_collect(sources: list[str] | None = None, days: int | None = None) -> list[dict[str, Any]]:
    """Fetch audience metrics (read-only API calls) into the local metrics store. Default: every configured source, resuming where each left off; `days` re-fetches that many days. Returns per-source status."""
    if days is not None and days < 1:
        raise ValueError("days must be at least 1.")
    return run_collection(sources, days)


@mcp.tool(annotations=READ_ONLY)
def podcast_metrics_summary(days: int = 30) -> dict[str, Any]:
    """Summarize collected audience metrics for the last `days` days: headline numbers vs the previous period, top episodes, top apps/countries, and each source's freshness."""
    return summarize(days=max(1, days))


@mcp.tool(annotations=READ_ONLY)
def podcast_episode_metrics(slug: str) -> dict[str, Any]:
    """All collected metrics for one episode: daily downloads/listens per source and social posts linking to it."""
    return episode_metrics(validate_slug(slug))


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_metrics_report(days: int = 90) -> dict[str, Any]:
    """Write the self-contained HTML metrics dashboard for the last `days` days and return its path."""
    return {"status": "written", "path": str(write_report(days=max(1, days)))}


if __name__ == "__main__":
    # basicConfig logs to stderr, keeping stdout clean for the MCP protocol stream.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run()
