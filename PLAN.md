# Architecture & Roadmap

## Thesis

Podcast hosting services charge monthly for object storage, a database table, and an RSS file behind a dashboard. `podcast_server` keeps those three under the creator's control and replaces the dashboard with MCP tools an agent can drive, with a human approving every release.

## Architecture

```
            Agent (Claude Desktop / Cursor / your own)  ── reads AGENTS.md + $DATA_DIR/PLAYBOOK.md
                              │ MCP (stdio)
                              ▼
┌──────────────────────── mcp_server/server.py ────────────────────────┐
│ stage → (human review) → publish → rebuild → deploy → backup          │
└───────┬───────────────┬─────────────────┬──────────────────┬─────────┘
        ▼               ▼                 ▼                  ▼
  core/storage.py   core/db.py      core/site.py        scripts/backup_to_hf.py
  R2 (S3 API)       SQLite catalog  feed + pages        private HF dataset
  custom domain     ($DATA_DIR)     SITE_MODE=none|astro
```

| Module | Responsibility |
| :--- | :--- |
| `core/config.py` | All settings (pydantic-settings). Show identity is required; no show values in code. |
| `core/db.py` | Catalog schema, insert semantics, exclusion list, triggers. |
| `core/storage.py` | Streaming multipart uploads/downloads; mirroring that records R2's actual byte length. |
| `core/feed_generator.py` | Pure RSS XML generation. |
| `core/site.py` | Publishes the feed (R2 or website repo), syncs episode pages, scoped git deploy. |
| `core/doctor.py` | End-to-end setup check. |
| `importers/` | `rss.py` (any host, GUID-preserving), `descript.py` (staging, chapters). |
| `exporters/` | Astro Markdown pages, blog posts, social templates, X posting (via xurl), YouTube. |
| `metrics/` | Audience metrics: `collectors/` (OP3, Cloudflare, X, YouTube; plugins via `METRICS_EXTRA_COLLECTORS`), `store.py` (`metrics.db`), `analysis.py`, `report.py` (offline HTML dashboard), `collect.py` (CLI + scheduler entry). |

### Metrics Pipeline

```
collectors ──► CollectionResult ──► MetricsStore ($DATA_DIR/metrics.db) ──► analysis ──► dashboard.html
(op3, cloudflare, x, plugins)      (replace day window + upsert)            (generic)     + MCP tools
```

- **Contract:** a collector subclasses `metrics.collectors.base.Collector`, declares `name`, `description`, and `metrics` (`MetricSpec` labels, `headline` flag), and implements `config_problem()` and `collect(ctx)`. The pipeline owns windows, storage, run logging, and failure isolation; analysis and the dashboard pick up new metrics automatically.
- **Windows:** each source resumes from its last successful run minus 3 days of overlap (sources revise recent days), or backfills `METRICS_BACKFILL_DAYS` on its first run. Collectors clamp to their own retention (Cloudflare asks the API for the plan's limits).
- **Episode matching:** `EpisodeIndex` resolves OP3 URLs, audio request paths, and episode page links to catalog slugs, accepting fallbacks only on the show's own hosts.

## Data Model

```sql
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,            -- = slug
    guid TEXT UNIQUE NOT NULL,      -- permanent; copied verbatim on import
    episode_number INTEGER, season INTEGER DEFAULT 1,
    title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
    summary TEXT, content_html TEXT, content_markdown TEXT,
    published_at TEXT NOT NULL,     -- ISO 8601
    original_audio_url TEXT,        -- previous host (imports) or R2
    audio_filename TEXT, audio_url TEXT, audio_op3_url TEXT,
    duration_seconds REAL, duration_formatted TEXT,
    file_size_bytes INTEGER,        -- always R2's stored length
    explicit BOOLEAN, cover_image_url TEXT, tags TEXT,  -- tags: JSON list
    audio_synced BOOLEAN,           -- audio confirmed in R2; unsynced episodes stay out of the feed
    created_at TEXT, updated_at TEXT
);
CREATE TABLE excluded_episodes (guid TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT);
```

Triggers:
- **`block_excluded_episodes`:** rejects inserts whose slug or GUID is excluded. The table is synced from `$DATA_DIR/excluded_episodes.json` on every open.
- **`clear_stale_html`:** updating `content_markdown` clears imported `content_html`, so rewritten notes win in the feed.

Metrics live in a separate, disposable `metrics.db`, one long table for every source:

```sql
CREATE TABLE observations (
    source TEXT, entity_type TEXT, entity_id TEXT,   -- e.g. ('op3', 'episode', '<slug>'), ('x', 'post', '<id>')
    metric TEXT, dimension TEXT,                     -- dimension: '' (total) or 'app=Overcast', 'country=US', ...
    granularity TEXT,                                -- 'day' (amount that day; summable) | 'snapshot' (running total; take latest)
    period TEXT, value REAL, episode_slug TEXT, collected_at TEXT,
    PRIMARY KEY (source, entity_type, entity_id, metric, dimension, granularity, period)
);
CREATE TABLE entities (source, entity_type, entity_id, label, url, created_at, episode_slug, updated_at);
CREATE TABLE collection_runs (id, source, started_at, finished_at, status, window_start, window_end, observations, entities, message);
```

## Invariants

1. GUIDs never change; slugs are never reused.
2. HTTP range requests (206) must work for every enclosure; R2 custom domains support them.
3. All metadata is inspectable: SQLite plus plain Markdown and JSON.
4. Secrets stay in `.env`; agents use scoped tools, not raw credentials.
5. The engine stays show-neutral; show data and editorial rules live in `$DATA_DIR`.

## Status

| Capability | State |
| :--- | :--- |
| Storage, catalog, feed (RSS 2.0 + iTunes + Podcasting 2.0) | Done |
| MCP server (17 tools) with human review gate | Done |
| Site modes `none` / `astro`, scoped deploy | Done |
| GUID-preserving RSS import + audio mirroring | Done |
| Setup doctor, HF backups, exclusion list | Done |
| Audience metrics: OP3 / Cloudflare / X / YouTube collectors, plugin contract, dashboard, MCP tools | Done |
| Metrics: Apple/Spotify CSV import collectors | Planned |
| Transcription, vector index, semantic search | Planned |

## Roadmap

1. **Audio intelligence:** transcription with word-level timestamps (e.g. `faster-whisper`), transcript chunks embedded into a local vector index (e.g. LanceDB or `sqlite-vec`), and MCP tools `podcast_transcribe`, `podcast_search`, `podcast_recommend_similar` for cross-linking episodes. Planned table: `transcript_chunks(id, episode_id, start_time, end_time, speaker, text, embedding)`. Use a package name other than `intelligence/`, which many local setups use as a symlink to show research.
2. **Formats:** non-MP3 enclosures (`audio/x-m4a`), with the enclosure type stored per episode.
3. **Storage backends:** generic S3-compatible endpoints beyond R2.
4. **Sites:** adapters for other static site generators' frontmatter conventions.
