# AGENTS.md — Operating Guide for AI Agents

How AI coding and operations agents (Claude Desktop, Cursor, Codex, or your own) should use `podcast_server`. This file is show-neutral. Everything about a particular show lives in that show's data directory.

---

## 1. Read the Show Playbook First

Each show keeps a private playbook at **`$DATA_DIR/PLAYBOOK.md`** (see `examples/show-data/PLAYBOOK.md` for the template). It defines the show's editorial voice, title and show-notes standards, review checklist, and custom workflows. Infrastructure details (DNS, R2, website wiring, backups, credential locations) are in **`$DATA_DIR/DEPLOYMENT.md`**; read it before changing any infrastructure, and update it afterwards. **When it exists, read it before any podcast task and follow it.** If it conflicts with this file on something show-specific, the playbook wins; on the safety invariants in section 6, this file wins.

Never copy playbook content, show data, or show-specific values into this repository.

## 2. Environment

1. **Python:** this is a uv project. `uv sync` creates `.venv` from `uv.lock`; run everything with `uv run ...` from the repo root.
2. **Dependencies:** add them with `uv add <package>` (or `uv add --dev <package>`), never with pip, so `pyproject.toml` and `uv.lock` stay in sync.
3. **Configuration:** all show identity, domains, website layout, and data paths come from `.env` (template: `.env.example`). Read values in code through `from core.config import settings`, never by hardcoding paths or URLs.
4. **Secrets:** `.env` is `chmod 600`. Never print, log, or commit its values. Credentials kept outside `.env` (xurl's auth file, the YouTube token at `YOUTUBE_TOKEN_PATH`, the Instagram token at `INSTAGRAM_TOKEN_PATH`, the Substack session cookie at `SUBSTACK_COOKIES_PATH`) are never read, printed, or copied: the engine and those CLIs use them directly. Never ask the human to paste a credential into chat.
5. **MCP server:** `bin/run-mcp.sh` (stdio).
6. **Health check:** `uv run python -m core.doctor`.
7. **Tests:** `uv run python -m unittest discover -s tests` (uses a fake show; never touches real data).
8. **Optional integrations:** a tool or metrics source that isn't set up says exactly what's missing (e.g. "set DEVTO_API_KEY"). Relay that message and point the human to the matching section of [`ADOPTING.md`](./ADOPTING.md) instead of working around it. Logins only the human can do: `xurl auth oauth2` (X), the YouTube OAuth step, `uv run python -m exporters.instagram login` (Instagram, again if Meta reports the token invalid), and `uv run python -m exporters.substack_post login` (Substack, again whenever its cookie expires). `core.doctor` lists what's ready.
9. **Commits:** stage files by path, not `git add -A`. CLIs run from the checkout can leave caches with account details (e.g. the Cloudflare CLI's `.cloudflare/`, which `.gitignore` already excludes).

## 3. MCP Tools

The server also sends these rules to every client on connect, and annotates each tool (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so clients can auto-approve reads and ask before public actions. `tests/test_mcp_server.py` fails if this table and the server's tool list differ.

| Tool | Parameters | Purpose |
| :--- | :--- | :--- |
| `podcast_status` | *(none)* | Episode counts, sync state, R2 readiness. |
| `podcast_inspect` | `audio_path` | Exact duration, byte length, bitrate, sample rate of an MP3. |
| `podcast_list_episodes` | `limit=10, offset=0` | Newest-first episode list (max 200 per call). |
| `podcast_get_episode` | `slug` | Full metadata and show notes for one episode. |
| `podcast_rebuild_feed` | *(none)* | Regenerates the feed (and, in astro mode, episode pages). Astro mode: does **not** deploy; follow with `podcast_deploy_site`. None mode: the feed upload is live. Episodes without audio in R2 are left out. |
| `podcast_create_blog` | `slug, title, description, content_markdown, tags, draft` | Writes a companion blog post into the website's blog directory (astro mode). |
| `podcast_stage_from_descript` | `transcript_markdown, slug, audio_download_url, local_audio_path, guest_name, summary, links` | Downloads/inspects the master audio, extracts chapters from markers, drafts show notes. Returns a package for human review. |
| `podcast_publish` | `audio_file_path, title, description, show_notes_markdown, slug, episode_number, season, explicit, tags, blog_content_markdown, blog_title, blog_draft` | Validates (rejects bad, existing, or excluded slugs before uploading), uploads to R2, inserts into the catalog with a new permanent GUID, rebuilds outputs, and deploys (astro mode: commits only generated files). |
| `podcast_update_episode` | `slug, title, description, show_notes_markdown, tags, episode_number, season, explicit` | Edits a published episode; omitted fields stay as they are. GUID, slug, and audio can't change. Rewritten notes replace stale imported HTML. Rebuilds the feed and pages (astro mode: then `podcast_deploy_site`). **Public: only text the human approved.** |
| `podcast_deploy_site` | `message, blog_slugs` | Astro mode: commits only engine-generated files (feed, episode pages, and the named blog posts) on `WEBSITE_BRANCH` and pushes. None mode: `not_applicable`. Also the retry for a publish whose `git_deploy` was `skipped` or `failed`. |
| `podcast_backup` | `check_only=false` | Starts the Hugging Face backup in the background and returns at once (`started`, or `running` if one is going). `check_only=true` reports `running` / `completed` / `failed` / `never_run` with the end of `$DATA_DIR/backup/last_run.log`. |
| `podcast_upload_media_asset` | `file_path, target_filename, prefix` | Uploads images/video (GIF, PNG, JPEG, WebP, MP4, WebM, MOV) to R2. Refuses other types and never overwrites an existing key. |
| `podcast_publish_to_youtube` | `video_path, title, description, tags, privacy_status, episode_slug, expect_short` | Resumable upload to the authenticated YouTube channel. Logs each upload (with `episode_slug`) to `$DATA_DIR/social/youtube_uploads.jsonl`; YouTube metrics cover logged uploads plus `$DATA_DIR/youtube_videos.json`. **Shorts:** there's no separate upload; YouTube files any square or vertical video of up to 3 minutes as a Short. `expect_short=true` measures the file with ffprobe (`FFPROBE_PATH`) and refuses, before uploading, one that would land as a regular video; the result's `short` holds the measured size and length. Use `privacy_status="public"` for Shorts (unlisted ones never reach the Shorts feed); custom thumbnails can't be set on Shorts through the API. |
| `podcast_draft_social` | `title, guest_name, core_tension, technical_specs, episode_url, youtube_url` | Fills the show's social post templates (`$DATA_DIR/templates/`). |
| `podcast_post_to_x` | `text, media_path, dry_run, episode_slug` | Posts to X as `X_USERNAME` through xurl, with one optional image, GIF, or video. Refuses non-media files before uploading. Logs each accepted post (with `episode_slug`) to `$DATA_DIR/social/x_posts.jsonl`; X metrics cover only logged posts. **Live and public: only post text and media the human approved verbatim.** |
| `podcast_post_to_instagram` | `media_paths, caption, post_type, dry_run, episode_slug` | Publishes a feed post to `INSTAGRAM_USERNAME`'s professional account. `post_type="image"`: one PNG, JPEG, or WebP with an aspect ratio between 4:5 and 1.91:1, converted to JPEG and staged on the public R2 domain (Instagram fetches it from there). Captions up to 2,200 characters and 30 hashtags; links in captions aren't clickable. Refuses non-images, bad shapes, and a token for another account before uploading. Logs each post (with `episode_slug`) to `$DATA_DIR/social/instagram_posts.jsonl` (Instagram metrics cover only logged posts) and returns the logged post instead of posting the same image and caption twice. **Live and public: only an image and caption the human approved verbatim.** |
| `podcast_list_instagram_posts` | `limit=25, after` | Read-only: the Instagram account's posts, newest first (up to 100 per call), with media id, type, date, URL, and the start of the caption. Pass `next_cursor` as `after` for the next page. |
| `podcast_delete_instagram_post` | `media_id, dry_run` | **Permanently** deletes one of the account's posts (no undo, no Recently Deleted). Reads it first and refuses a non-numeric id or another account's post. Carousels go whole; ads can't be deleted. Logs the deleted post, full caption included, to `$DATA_DIR/social/instagram_deletions.jsonl`. Needs `instagram_manage_contents` on the token. **Only posts the human explicitly named: dry run first, one call per post.** |
| `podcast_list_youtube_videos` | `limit=25, page_token` | Read-only: the channel's uploads, newest first (up to 50 per call), with video id, title, date, privacy, URL, and tracked episode. Pass `next_page_token` for the next page. |
| `podcast_delete_youtube_video` | `video_id, dry_run` | **Permanently** deletes one of the channel's videos. Reads it first and refuses malformed ids and other channels' videos. Logs it, description included, to `$DATA_DIR/social/youtube_deletions.jsonl`. Needs the `youtube.force-ssl` scope; costs 50 quota units. **Only a video the human named: dry run first.** |
| `podcast_list_devto_articles` | *(none)* | Read-only: the dev.to account's articles, published and drafts, with id, title, published flag and date, URL, and canonical URL. |
| `podcast_unpublish_devto_article` | `article_id, dry_run` | Turns one published article back into a draft (dev.to's API can't delete; the human deletes for good on dev.to). Refuses other accounts' articles; a draft returns `already_unpublished`. Logged to `$DATA_DIR/social/devto_unpublished.jsonl`. **Only an article the human named.** |
| `podcast_list_substack_posts` | *(none)* | Read-only: the publication's published posts and drafts (private API), with post id, title, `state`, date, and URL. |
| `podcast_delete_substack_post` | `post_id, dry_run` | **Permanently** deletes one post or draft from the publication (private API, like the editor's Delete). Reads it from the publication first. Logged to `$DATA_DIR/social/substack_deletions.jsonl`; a deleted cross-post draft no longer blocks `podcast_crosspost_to_substack`. **Only a post the human named: dry run first.** |
| `podcast_crosspost_to_devto` | `blog_slug, tags, dry_run` | Creates an **unpublished** dev.to draft of a published companion blog post, with `canonical_url` from `BLOG_URL_TEMPLATE` (or the post's own `canonical` frontmatter, if it was first published elsewhere) and site-relative links made absolute. Up to 4 tags, lowercase letters and digits only. Never publishes: the human publishes the draft on dev.to. If a draft of the post already exists (in `$DATA_DIR/social/devto_posts.jsonl` or on the account), returns it instead of creating another. |
| `podcast_crosspost_to_substack` | `blog_slug, dry_run` | Creates an **unpublished** Substack draft of a published companion blog post through Substack's private API (python-substack, signed in with the session cookie in `SUBSTACK_COOKIES_PATH`). Substack has no canonical URL setting, so the draft opens with an "Originally published at" line; links are made absolute and the post's tags are kept. Refuses images that aren't https URLs; returns `warnings` for tables and raw HTML, which Substack can't show. Never publishes or emails. Returns an existing draft (from `$DATA_DIR/social/substack_posts.jsonl`, or matched by slug or title on Substack) instead of creating another. |
| `podcast_stage_thumbnail` | `prompt_text, output_filename, style` | Thumbnail step 1: builds a 16:9 art prompt (no lettering, empty right side) and returns where to save the art. Does not generate it; render it with your image model. |
| `podcast_render_thumbnail` | `base_image_path, headline_lines, slug, label, accent_line` | Thumbnail step 2: composites the headline (and an optional small label) onto the art in the show's style (`$DATA_DIR/templates/thumbnail_style.json`), auto-fitted and clear of the duration badge. Writes `$DATA_DIR/thumbnails/<slug>-final.png` (JPEG if over 2 MB). |
| `podcast_set_youtube_thumbnail` | `video_id, thumbnail_path, episode_slug` | Thumbnail step 3: sets the custom thumbnail on YouTube. **Public: only an image the human approved.** Logs to `$DATA_DIR/social/youtube_thumbnails.jsonl`; the metrics dashboard pins the change. |
| `podcast_metrics_collect` | `sources, days` | Read-only: fetches audience metrics from every configured source (or the named ones) into `$DATA_DIR/metrics.db`, resuming where each left off. Returns per-source `ok` / `skipped` (with what to configure) / `failed`. |
| `podcast_metrics_summary` | `days=30` | Headline numbers vs the previous period, top episodes, top apps/countries, and each source's freshness. |
| `podcast_episode_metrics` | `slug` | Daily downloads/listens per source for one episode, plus the social posts that link to it. |
| `podcast_metrics_report` | `days=90` | Writes the offline HTML dashboard (`$DATA_DIR/metrics/dashboard.html`: an Overview tab plus one tab per platform with every collected metric) and returns its path. |

## 4. Publishing Runbook (generic)

1. **Stage.** From Descript: `podcast_stage_from_descript(...)`. From a local file: `podcast_inspect(audio_path)` and confirm `audio/mpeg`, a plausible duration, and size.
2. **Draft the package** following the show playbook: title options, summary, chapters, and any companion content. Chapters in show notes use bold timestamps so players can link them:
   ```markdown
   ### Chapters

   - **00:00** — Introduction
   - **06:40** — Main topic
   ```
3. **Human review gate. Stop and present the package. Do not publish without explicit approval.**
4. **Publish:** `podcast_publish(...)`. Check the response: `git_deploy.status` should be `pushed` (astro) or `not_applicable` (none mode, where the feed is live once uploaded). If it's `skipped` or `failed`, see section 5.
5. **Verify:** the audio URL returns HTTP 206 for a range request, and `PODCAST_FEED_URL` returns 200 and lists the episode.
6. **Thumbnail (video episodes):** `podcast_stage_thumbnail` → render the art with your image model → `podcast_render_thumbnail` → show the result and get approval → `podcast_set_youtube_thumbnail`.
7. **Promote (optional):** draft with `podcast_draft_social`, get explicit approval of the exact text and media, optionally check with `podcast_post_to_x(..., dry_run=true)`, then post with `episode_slug` set so its metrics are credited to the episode. Return the post URL. If a post call fails, read `$DATA_DIR/social/x_posts.jsonl` before retrying so a post that went through isn't sent twice. Instagram works the same way with `podcast_post_to_instagram(..., dry_run=true)` first and `$DATA_DIR/social/instagram_posts.jsonl` as its log; put the episode link in the profile or say where to find it, because caption links aren't clickable.
8. **Cross-post the blog (optional):** once the companion blog post is live on the site (`git_deploy.status` is `pushed`):
   - dev.to: `podcast_crosspost_to_devto(blog_slug, tags)` with up to 4 dev.to tags such as `["robotics", "ai"]`.
   - Substack: `podcast_crosspost_to_substack(blog_slug)`. Pass any `warnings` (tables, raw HTML) on to the human, who fixes them in the draft. If sign-in fails, the session cookie has expired: ask the human to run `uv run python -m exporters.substack_post login`; never ask for the cookie in chat.
   - Both return draft URLs; the human reviews and publishes there. If the human deleted a draft and wants a fresh one, remove that slug's line from `$DATA_DIR/social/devto_posts.jsonl` or `substack_posts.jsonl` first.
9. **Back up:** `podcast_backup()` (if `HF_BACKUP_REPO` is set). It returns at once; check it later with `podcast_backup(check_only=true)` and report a `failed` status with its log tail.

## 5. Common Operations

- **Edit a published episode** (show notes, title, description, tags, number): get approval of the new text, call `podcast_update_episode(slug, ...)` with only the fields that change, then `podcast_deploy_site("chore(podcast): update show notes for <slug>")`. Don't edit `catalog.db` with SQL for routine edits; the tool rebuilds the feed and pages and can't touch the GUID.
- **Before bulk catalog edits** (anything the tools can't do), back it up: `sqlite3 "$DATA_DIR/catalog.db" ".backup $DATA_DIR/catalog.db.bak_$(date +%Y%m%d_%H%M%S)"`.
- **Deploy skipped or failed (astro mode):** switch the site repo to `WEBSITE_BRANCH`, then call `podcast_deploy_site(message, blog_slugs)` (name any companion blog post). Never stage the site repo with `git add .`; only the generated paths (feed file, episode pages, blog post) may be committed.
- **Import a back catalog:** `uv run python -m importers.rss <old-feed-url> --mirror`.
- **Answer "how is the show doing?":** call `podcast_metrics_summary` (run `podcast_metrics_collect` first if a source's `last_run_at` is more than a day old). Report numbers with their source and basis, because they measure different things: OP3 `downloads` are bot-filtered podcast-app downloads; Cloudflare `est_listens` is bytes served ÷ file size across every client; X `impressions` count views of posts published through `podcast_post_to_x` (never the rest of the account); Instagram `views`, `reach`, `likes`, and the rest cover only posts published through `podcast_post_to_instagram` (`followers` is account-wide; views and reach need `instagram_manage_insights` on the token); YouTube `views` / `lifetime_views` cover only the show's tracked videos; dev.to `views` and Substack `views` / `opens` cover only articles the engine cross-posted (from their logs), and Substack's come from its private API. Never add them together. A `first 7d` value of `null` means the source's data starts after that episode's release, not zero.
- **Remove a post, video, or article:** list with the platform's `podcast_list_*` tool, have the human name the exact item, call the remove tool with `dry_run=true` and show the result, then call it again without `dry_run` after approval. One item per call; never sweep a list. `podcast_delete_instagram_post`, `podcast_delete_youtube_video`, and `podcast_delete_substack_post` are permanent; `podcast_unpublish_devto_article` only turns an article back into a draft (dev.to's API can't delete). Removals are logged in `$DATA_DIR/social/`.
- **Metrics data is disposable:** `metrics.db` can be deleted and re-collected (within each source's retention: Cloudflare keeps about 30 days on the free plan, OP3 keeps everything). Never write to `catalog.db` from metrics code.

## 6. Invariants (never violate)

- **GUIDs are permanent.** Never change, regenerate, or reuse an episode GUID or slug; podcast apps track listening state by GUID.
- **Excluded episodes stay excluded.** Slugs/GUIDs in `$DATA_DIR/excluded_episodes.json` are blocked by a database trigger. Don't work around it.
- **Published R2 objects are immutable.** They're cached for a year; upload under a new key instead of overwriting.
- **Audio headers:** episodes are `audio/mpeg` with `Cache-Control: public, max-age=31536000, immutable`.
- **Use the custom audio domain,** never the bucket's `r2.dev` URL.
- **Scoped deploys only.** Commit only engine-generated files in the website repo, and only from `WEBSITE_BRANCH`.
- **The site owns the original; cross-posts are drafts.** Cross-post a blog post only after it's live on the site, and never publish a dev.to or Substack draft yourself: the human publishes (and, on Substack, decides whether subscribers are emailed).
- **Engine vs. show data.** This repository stays show-neutral. Show-specific scripts, migrations, research notes, templates, playbooks, and data go in `$DATA_DIR` (ideally its own private repo). Show-specific values go in `.env`, never as code defaults or in committed docs.
