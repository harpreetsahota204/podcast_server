"""SQLite catalog of episodes (`data/catalog.db`), the engine's source of truth.

The `guid` column must never change once written: Apple and Spotify key
played/unplayed state on it.
"""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from core.config import settings


class CatalogDB:
    """Thin wrapper over one SQLite connection; use as a context manager so it always closes."""

    def __init__(self, db_path: Path, excluded_file: Path | None = None) -> None:
        """Open (creating if needed) the catalog at `db_path`, ensure the schema, and load exclusions.

        Exclusions come from `excluded_file`, defaulting to the show's data dir, so they
        survive a catalog rebuild.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        # Rows behave like dicts so callers can index by column name.
        self.conn.row_factory = sqlite3.Row
        self.init_schema()
        self.load_exclusions(excluded_file or settings.excluded_episodes_file)

    def __enter__(self) -> Self:
        """Return self for `with CatalogDB(...) as db:` usage."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection even when the body raised."""
        self.close()

    def init_schema(self) -> None:
        """Create tables, indexes, and triggers (stale HTML, exclusions) if they don't exist.

        `slug` and `guid` are UNIQUE, so SQLite already indexes them implicitly.
        """
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    guid TEXT UNIQUE NOT NULL,
                    episode_number INTEGER,
                    season INTEGER DEFAULT 1,
                    title TEXT NOT NULL,
                    slug TEXT UNIQUE NOT NULL,
                    summary TEXT,
                    content_html TEXT,
                    content_markdown TEXT,
                    published_at TEXT NOT NULL,
                    original_audio_url TEXT,
                    audio_filename TEXT,
                    audio_url TEXT,
                    audio_op3_url TEXT,
                    duration_seconds REAL DEFAULT 0.0,
                    duration_formatted TEXT,
                    file_size_bytes INTEGER DEFAULT 0,
                    explicit BOOLEAN DEFAULT 0,
                    cover_image_url TEXT,
                    tags TEXT,
                    audio_synced BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_published ON episodes(published_at DESC);")
            # content_html is only the original import from a previous host. Once anyone (including
            # agents writing raw SQL) edits the Markdown notes, the HTML is stale and
            # must not keep winning in the RSS feed, so the database clears it itself.
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS clear_stale_html
                AFTER UPDATE OF content_markdown ON episodes
                WHEN NEW.content_markdown IS NOT OLD.content_markdown AND COALESCE(OLD.content_html, '') <> ''
                BEGIN
                    UPDATE episodes SET content_html = '' WHERE id = NEW.id;
                END;
            """)
            # Episodes deliberately kept out of the public catalog. The trigger rejects
            # them at the database level, so no importer, tool, or agent can re-add one.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS excluded_episodes (
                    guid TEXT PRIMARY KEY,
                    slug TEXT UNIQUE NOT NULL,
                    title TEXT
                );
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS block_excluded_episodes
                BEFORE INSERT ON episodes
                WHEN EXISTS (SELECT 1 FROM excluded_episodes WHERE guid = NEW.guid OR slug = NEW.slug)
                BEGIN
                    SELECT RAISE(ABORT, 'episode is permanently excluded (excluded_episodes.json)');
                END;
            """)

    def load_exclusions(self, excluded_file: Path) -> None:
        """Sync the excluded_episodes table from the tracked JSON list (additive; a no-op if absent)."""
        if not excluded_file.exists():
            return
        episodes = json.loads(excluded_file.read_text(encoding="utf-8"))["episodes"]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO excluded_episodes (guid, slug, title) VALUES (?, ?, ?)",
                [(e["guid"], e["slug"], e["title"]) for e in episodes],
            )

    def is_excluded(self, slug: str, guid: str | None = None) -> bool:
        """True if this slug (or GUID) is on the permanent exclusion list."""
        row = self.conn.execute(
            "SELECT 1 FROM excluded_episodes WHERE slug = ? OR guid = ?", (slug, guid)
        ).fetchone()
        return row is not None

    def _insert(self, episode: dict[str, Any], on_conflict: str) -> bool:
        """Insert one episode row; return True if a row was actually written.

        Column names come from code-defined record dicts, never from tool input,
        so interpolating them into the SQL is safe; values are always bound.
        """
        columns = ", ".join(episode)
        placeholders = ", ".join("?" * len(episode))
        with self.conn:
            cursor = self.conn.execute(
                f"INSERT INTO episodes ({columns}) VALUES ({placeholders}) {on_conflict}",
                list(episode.values()),
            )
        return cursor.rowcount == 1

    def insert_episode(self, episode: dict[str, Any]) -> None:
        """Insert a new episode; raises sqlite3.IntegrityError if its id, slug, or guid exists."""
        self._insert(episode, on_conflict="")

    def insert_episode_if_new(self, episode: dict[str, Any]) -> bool:
        """Insert an episode unless one with the same id, slug, or guid exists; never overwrites."""
        return self._insert(episode, on_conflict="ON CONFLICT DO NOTHING")

    def get_episode_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Return the episode with this slug, or None."""
        row = self.conn.execute("SELECT * FROM episodes WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None

    def list_episodes(self, limit: int = -1, offset: int = 0) -> list[dict[str, Any]]:
        """Return episodes newest-first; SQLite treats a negative LIMIT as "no limit"."""
        rows = self.conn.execute(
            "SELECT * FROM episodes ORDER BY published_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]

    def count_episodes(self) -> int:
        """Total number of episodes in the catalog."""
        return self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    def count_synced_audio(self) -> int:
        """Number of episodes whose audio is confirmed present in R2."""
        return self.conn.execute("SELECT COUNT(*) FROM episodes WHERE audio_synced = 1").fetchone()[0]

    def mark_audio_synced(self, episode_id: str, file_size_bytes: int) -> None:
        """Flag an episode's audio as mirrored to R2 and record the byte length R2 actually stores."""
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET audio_synced = 1, file_size_bytes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (file_size_bytes, episode_id),
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.conn.close()
