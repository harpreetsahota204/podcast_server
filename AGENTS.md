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

## 3. MCP Tools

| Tool | Parameters | Purpose |
| :--- | :--- | :--- |
| `podcast_status` | *(none)* | Episode counts, sync state, R2 readiness. |
| `podcast_inspect` | `audio_path` | Exact duration, byte length, bitrate, sample rate of an MP3. |
| `podcast_list_episodes` | `limit=10, offset=0` | Newest-first episode list (max 200 per call). |
| `podcast_get_episode` | `slug` | Full metadata and show notes for one episode. |
| `podcast_rebuild_feed` | *(none)* | Regenerates the feed (and, in astro mode, episode pages). Does **not** deploy. Episodes without audio in R2 are left out. |
| `podcast_create_blog` | `slug, title, description, content_markdown, tags, draft` | Writes a companion blog post into the website's blog directory (astro mode). |
| `podcast_stage_from_descript` | `transcript_markdown, slug, audio_download_url, local_audio_path, guest_name, summary, links` | Downloads/inspects the master audio, extracts chapters from markers, drafts show notes. Returns a package for human review. |
| `podcast_publish` | `audio_file_path, title, description, show_notes_markdown, slug, episode_number, season, explicit, tags, blog_content_markdown, blog_title, blog_draft` | Validates (rejects bad, existing, or excluded slugs before uploading), uploads to R2, inserts into the catalog with a new permanent GUID, rebuilds outputs, and deploys (astro mode: commits only generated files). |
| `podcast_upload_media_asset` | `file_path, target_filename, prefix` | Uploads images/video (GIF, PNG, JPEG, WebP, MP4, WebM, MOV) to R2. Refuses other types and never overwrites an existing key. |
| `podcast_publish_to_youtube` | `video_path, title, description, tags, privacy_status` | Resumable upload to the authenticated YouTube channel. |
| `podcast_draft_social` | `title, guest_name, core_tension, technical_specs, episode_url, youtube_url` | Fills the show's social post templates (`$DATA_DIR/templates/`). |
| `podcast_stage_thumbnail` | `prompt_text, output_filename, style` | Builds a 16:9 thumbnail prompt and returns where to save the image. Does not generate it. |

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
6. **Back up:** `uv run python scripts/backup_to_hf.py` (if `HF_BACKUP_REPO` is set).

## 5. Common Operations

- **Rewrite show notes:** update `content_markdown` in the catalog (never `guid`). A trigger clears any stale imported `content_html`. Then rebuild and deploy:
  ```bash
  uv run python -m core.site
  uv run python -c "from core.site import deploy_site; print(deploy_site('chore(podcast): update show notes'))"
  ```
- **Before bulk catalog edits,** back it up: `sqlite3 "$DATA_DIR/catalog.db" ".backup $DATA_DIR/catalog.db.bak_$(date +%Y%m%d_%H%M%S)"`.
- **Deploy skipped or failed (astro mode):** switch the site repo to `WEBSITE_BRANCH`, then re-run `deploy_site(...)`. Never stage the site repo with `git add .`; only the generated paths (feed file, episode pages, blog post) may be committed.
- **Import a back catalog:** `uv run python -m importers.rss <old-feed-url> --mirror`.

## 6. Invariants (never violate)

- **GUIDs are permanent.** Never change, regenerate, or reuse an episode GUID or slug; podcast apps track listening state by GUID.
- **Excluded episodes stay excluded.** Slugs/GUIDs in `$DATA_DIR/excluded_episodes.json` are blocked by a database trigger. Don't work around it.
- **Published R2 objects are immutable.** They're cached for a year; upload under a new key instead of overwriting.
- **Audio headers:** episodes are `audio/mpeg` with `Cache-Control: public, max-age=31536000, immutable`.
- **Use the custom audio domain,** never the bucket's `r2.dev` URL.
- **Scoped deploys only.** Commit only engine-generated files in the website repo, and only from `WEBSITE_BRANCH`.
- **Engine vs. show data.** This repository stays show-neutral. Show-specific scripts, migrations, research notes, templates, playbooks, and data go in `$DATA_DIR` (ideally its own private repo). Show-specific values go in `.env`, never as code defaults or in committed docs.
