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
4. **Secrets:** `.env` is `chmod 600`. Never print, log, or commit its values.
5. **MCP server:** `bin/run-mcp.sh` (stdio).
6. **Health check:** `uv run python -m core.doctor`.
7. **Tests:** `uv run python -m unittest discover -s tests` (uses a fake show; never touches real data).
8. **Commits:** stage files by path, not `git add -A`. CLIs run from the checkout can leave caches with account details (e.g. the Cloudflare CLI's `.cloudflare/`, which `.gitignore` already excludes).

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
| `podcast_publish_to_youtube` | `video_path, title, description, tags, privacy_status, episode_slug` | Resumable upload to the authenticated YouTube channel. Logs each upload (with `episode_slug`) to `$DATA_DIR/social/youtube_uploads.jsonl`; YouTube metrics cover logged uploads plus `$DATA_DIR/youtube_videos.json`. |
| `podcast_draft_social` | `title, guest_name, core_tension, technical_specs, episode_url, youtube_url` | Fills the show's social post templates (`$DATA_DIR/templates/`). |
| `podcast_post_to_x` | `text, media_path, dry_run, episode_slug` | Posts to X as `X_USERNAME` through xurl, with one optional image, GIF, or video. Refuses non-media files before uploading. Logs each accepted post (with `episode_slug`) to `$DATA_DIR/social/x_posts.jsonl`; X metrics cover only logged posts. **Live and public: only post text and media the human approved verbatim.** |
| `podcast_stage_thumbnail` | `prompt_text, output_filename, style` | Thumbnail step 1: builds a 16:9 art prompt (no lettering, empty right side) and returns where to save the art. Does not generate it; render it with your image model. |
| `podcast_render_thumbnail` | `base_image_path, headline_lines, slug, label, accent_line` | Thumbnail step 2: composites the headline (and an optional small label) onto the art in the show's style (`$DATA_DIR/templates/thumbnail_style.json`), auto-fitted and clear of the duration badge. Writes `$DATA_DIR/thumbnails/<slug>-final.png` (JPEG if over 2 MB). |
| `podcast_set_youtube_thumbnail` | `video_id, thumbnail_path, episode_slug` | Thumbnail step 3: sets the custom thumbnail on YouTube. **Public: only an image the human approved.** Logs to `$DATA_DIR/social/youtube_thumbnails.jsonl`; the metrics dashboard pins the change. |
| `podcast_metrics_collect` | `sources, days` | Read-only: fetches audience metrics from every configured source (or the named ones) into `$DATA_DIR/metrics.db`, resuming where each left off. Returns per-source `ok` / `skipped` (with what to configure) / `failed`. |
| `podcast_metrics_summary` | `days=30` | Headline numbers vs the previous period, top episodes, top apps/countries, and each source's freshness. |
| `podcast_episode_metrics` | `slug` | Daily downloads/listens per source for one episode, plus the social posts that link to it. |
| `podcast_metrics_report` | `days=90` | Writes the offline HTML dashboard (`$DATA_DIR/metrics/dashboard.html`) and returns its path. |

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
7. **Promote (optional):** draft with `podcast_draft_social`, get explicit approval of the exact text and media, optionally check with `podcast_post_to_x(..., dry_run=true)`, then post with `episode_slug` set so its metrics are credited to the episode. Return the post URL. If a post call fails, read `$DATA_DIR/social/x_posts.jsonl` before retrying so a post that went through isn't sent twice.
8. **Back up:** `podcast_backup()` (if `HF_BACKUP_REPO` is set). It returns at once; check it later with `podcast_backup(check_only=true)` and report a `failed` status with its log tail.

## 5. Common Operations

- **Edit a published episode** (show notes, title, description, tags, number): get approval of the new text, call `podcast_update_episode(slug, ...)` with only the fields that change, then `podcast_deploy_site("chore(podcast): update show notes for <slug>")`. Don't edit `catalog.db` with SQL for routine edits; the tool rebuilds the feed and pages and can't touch the GUID.
- **Before bulk catalog edits** (anything the tools can't do), back it up: `sqlite3 "$DATA_DIR/catalog.db" ".backup $DATA_DIR/catalog.db.bak_$(date +%Y%m%d_%H%M%S)"`.
- **Deploy skipped or failed (astro mode):** switch the site repo to `WEBSITE_BRANCH`, then call `podcast_deploy_site(message, blog_slugs)` (name any companion blog post). Never stage the site repo with `git add .`; only the generated paths (feed file, episode pages, blog post) may be committed.
- **Import a back catalog:** `uv run python -m importers.rss <old-feed-url> --mirror`.
- **Answer "how is the show doing?":** call `podcast_metrics_summary` (run `podcast_metrics_collect` first if a source's `last_run_at` is more than a day old). Report numbers with their source and basis, because they measure different things: OP3 `downloads` are bot-filtered podcast-app downloads; Cloudflare `est_listens` is bytes served ÷ file size across every client; X `impressions` count views of posts published through `podcast_post_to_x` (never the rest of the account); YouTube `views` / `lifetime_views` cover only the show's tracked videos. Never add them together. A `first 7d` value of `null` means the source's data starts after that episode's release, not zero.
- **Metrics data is disposable:** `metrics.db` can be deleted and re-collected (within each source's retention: Cloudflare keeps about 30 days on the free plan, OP3 keeps everything). Never write to `catalog.db` from metrics code.

## 6. Invariants (never violate)

- **GUIDs are permanent.** Never change, regenerate, or reuse an episode GUID or slug; podcast apps track listening state by GUID.
- **Excluded episodes stay excluded.** Slugs/GUIDs in `$DATA_DIR/excluded_episodes.json` are blocked by a database trigger. Don't work around it.
- **Published R2 objects are immutable.** They're cached for a year; upload under a new key instead of overwriting.
- **Audio headers:** episodes are `audio/mpeg` with `Cache-Control: public, max-age=31536000, immutable`.
- **Use the custom audio domain,** never the bucket's `r2.dev` URL.
- **Scoped deploys only.** Commit only engine-generated files in the website repo, and only from `WEBSITE_BRANCH`.
- **Engine vs. show data.** This repository stays show-neutral. Show-specific scripts, migrations, research notes, templates, playbooks, and data go in `$DATA_DIR` (ideally its own private repo). Show-specific values go in `.env`, never as code defaults or in committed docs.
