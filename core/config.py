"""Central settings for the podcast engine, loaded from `.env` via pydantic-settings.

Everything show-specific (identity, domains, website layout, data location) is a
setting, so the code carries no single show's values. Required fields have no
defaults: a fresh clone fails fast with a list of exactly what to configure.

Set `PODCAST_SERVER_ENV_FILE` to load a different env file (e.g. one per show).
"""

import os
import uuid
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, resolved from this file so the engine works from any checkout location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.environ.get("PODCAST_SERVER_ENV_FILE", PROJECT_ROOT / ".env"))
# Namespace the Podcasting 2.0 spec fixes for deriving <podcast:guid> from a feed URL,
# so every host (and OP3, Podcast Index) computes the same id for the same feed.
PODCAST_GUID_NAMESPACE = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")


def podcast_guid_for_feed(feed_url: str) -> str:
    """Derive the Podcasting 2.0 show GUID: UUIDv5 of the feed URL without scheme or trailing slashes."""
    bare = feed_url.split("://", 1)[-1].rstrip("/")
    return str(uuid.uuid5(PODCAST_GUID_NAMESPACE, bare))


class Settings(BaseSettings):
    """Typed configuration; every field maps to the env var of the same name (case-insensitive)."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # --- Show identity (required) ---
    podcast_title: str
    podcast_description: str
    podcast_author: str
    podcast_owner_email: str = Field(description="Apple contact; also the owner in <podcast:locked>")
    podcast_link: str = Field(description="Show homepage; episode pages live under it in astro mode")
    podcast_feed_url: str = Field(description="Public URL of the RSS feed")
    podcast_image_url: str = Field(description="Square artwork, 1400-3000 px, JPEG or PNG")
    episode_guid_prefix: str = Field(description="Prefix for GUIDs minted at publish, e.g. 'myshow'")
    podcast_guid: str | None = Field(
        default=None,
        description="Show-level <podcast:guid>; derived from PODCAST_FEED_URL when unset. Keep it fixed if the feed moves.",
    )

    # --- Show metadata (optional) ---
    podcast_category: str = "Technology"
    podcast_subcategories: list[str] = Field(default_factory=list)
    podcast_language: str = "en-us"
    podcast_explicit: bool = False
    default_tags: list[str] = Field(default_factory=list, description="Tags used when a tool call gives none")

    # --- Storage (R2 credentials are only needed for uploads) ---
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket_name: str
    r2_public_domain: str = Field(description="Public audio base URL; use a custom domain, not r2.dev")
    use_op3_prefix: bool = Field(default=False, description="Wrap enclosures in OP3 analytics (stats are public)")

    # --- Website ---
    site_mode: Literal["astro", "none"] = Field(
        default="none",
        description="astro: write episode pages + feed into a git-deployed site; none: host the feed on R2",
    )
    website_path: Path | None = None
    website_rss_file: str = "public/podcast/rss.xml"
    website_episodes_dir: str = "src/content/podcast"
    website_blog_dir: str = "src/content/blog"
    website_branch: str = "main"
    feed_object_key: str = Field(default="feed.xml", description="R2 key for the feed in site_mode=none")

    # --- Local data ---
    data_dir: Path = PROJECT_ROOT / "data"
    database_path: Path | None = None

    # --- Optional integrations ---
    descript_api_bearer: str | None = None
    descript_api_key: str | None = None
    youtube_token_path: Path = Path.home() / ".config" / "podcast_server" / "youtube_token.json"
    hf_backup_repo: str | None = Field(default=None, description="Private HF dataset id for backups")
    thumbnail_style: str = "clean, high-contrast studio aesthetic"
    # X posting goes through the xurl CLI, which keeps its own OAuth tokens; the engine never sees them.
    x_username: str | None = Field(default=None, description="X handle to post as (no @); unset disables posting")
    xurl_app: str | None = Field(default=None, description="App name inside the xurl auth file")
    xurl_path: Path | None = Field(default=None, description="xurl binary; default: first xurl on PATH")
    # dev.to cross-posting creates drafts only; the human publishes them from the dev.to dashboard.
    devto_api_key: str | None = Field(default=None, description="dev.to API key; unset disables cross-posting")
    blog_url_template: str | None = Field(
        default=None,
        description="Public URL of a blog post with a {slug} placeholder; the canonical URL of cross-posts",
    )
    # Substack has no official publishing API: drafts go through its private web API with a browser
    # session cookie, which controls the whole account, so it lives in its own 0600 file, never in .env.
    substack_publication_url: str | None = Field(
        default=None, description="Your publication, e.g. https://name.substack.com; unset disables Substack drafts"
    )
    substack_cookies_path: Path = Field(
        default=Path.home() / ".config" / "podcast_server" / "substack_cookies.json",
        description="Session cookie file written by `python -m exporters.substack_post login`",
    )

    # --- Metrics (all optional; each source is collected only once it's configured) ---
    metrics_db_path: Path | None = None
    metrics_sources: list[str] = Field(
        default_factory=list, description="Collector names to run; empty means every configured collector"
    )
    metrics_extra_collectors: list[str] = Field(
        default_factory=list, description="Extra collectors as 'module:ClassName' import paths"
    )
    metrics_backfill_days: int = Field(default=90, ge=1, description="History fetched on a source's first run")
    op3_api_token: str | None = Field(default=None, description="Bearer token from op3.dev/api/keys")
    op3_show_uuid: str | None = Field(
        default=None, description="OP3 show id; looked up by <podcast:guid>, then feed URL, when unset"
    )
    cloudflare_api_token: str | None = Field(default=None, description="API token with Zone > Analytics > Read")
    cloudflare_zone_id: str | None = Field(default=None, description="Zone that serves R2_PUBLIC_DOMAIN")
    # X metrics use the posting account above (X_USERNAME via xurl, which adds owner-only
    # metrics); X_BEARER_TOKEN is the fallback for reading public counts without xurl.
    x_bearer_token: str | None = Field(default=None, description="X app-only token: public post metrics only")
    x_lookback_days: int = Field(default=30, ge=1, description="Re-read metrics for engine-published posts this recent")

    @model_validator(mode="after")
    def _check_and_normalize(self) -> Self:
        """Fill derived defaults and reject combinations that can't work."""
        self.r2_public_domain = self.r2_public_domain.rstrip("/")
        if self.podcast_guid is None:
            self.podcast_guid = podcast_guid_for_feed(self.podcast_feed_url)
        if self.database_path is None:
            self.database_path = self.data_dir / "catalog.db"
        if self.metrics_db_path is None:
            self.metrics_db_path = self.data_dir / "metrics.db"
        if self.site_mode == "astro" and self.website_path is None:
            raise ValueError("SITE_MODE=astro needs WEBSITE_PATH (the local checkout of your site repo).")
        # The template becomes a public canonical URL, so a typo must fail at startup, not on dev.to.
        if self.blog_url_template and (
            not self.blog_url_template.startswith("https://") or "{slug}" not in self.blog_url_template
        ):
            raise ValueError("BLOG_URL_TEMPLATE must be an https:// URL containing {slug}.")
        if self.substack_publication_url:
            if not self.substack_publication_url.startswith("https://"):
                raise ValueError("SUBSTACK_PUBLICATION_URL must be an https:// URL.")
            self.substack_publication_url = self.substack_publication_url.rstrip("/")
        return self

    # --- Derived paths ---

    @property
    def rss_output_path(self) -> Path:
        """Build copy of the RSS feed inside the data dir."""
        return self.data_dir / "dist" / "rss.xml"

    @property
    def site_rss_path(self) -> Path:
        """Feed location inside the website repo (astro mode)."""
        return self._site_root / self.website_rss_file

    @property
    def site_episodes_dir(self) -> Path:
        """Content collection holding one Markdown file per episode (astro mode)."""
        return self._site_root / self.website_episodes_dir

    @property
    def site_blog_dir(self) -> Path:
        """Directory for companion blog posts (astro mode)."""
        return self._site_root / self.website_blog_dir

    @property
    def _site_root(self) -> Path:
        """Website repo root; only meaningful in astro mode."""
        if self.website_path is None:
            raise RuntimeError("No website configured (SITE_MODE=none).")
        return self.website_path

    @property
    def raw_audio_dir(self) -> Path:
        """Staging directory for downloaded master audio."""
        return self.data_dir / "raw"

    @property
    def thumbnails_dir(self) -> Path:
        """Staging directory for generated YouTube thumbnails."""
        return self.data_dir / "thumbnails"

    @property
    def transcripts_dir(self) -> Path:
        """Plain-text episode transcripts."""
        return self.data_dir / "transcripts"

    @property
    def backup_dir(self) -> Path:
        """Staging folder mirrored into the backup dataset."""
        return self.data_dir / "backup"

    @property
    def templates_dir(self) -> Path:
        """Optional per-show text templates (e.g. social posts) overriding the built-in ones."""
        return self.data_dir / "templates"

    @property
    def metrics_report_path(self) -> Path:
        """Self-contained HTML metrics dashboard written by `python -m metrics.report`."""
        return self.data_dir / "metrics" / "dashboard.html"

    @property
    def youtube_upload_log_path(self) -> Path:
        """Append-only record of every video podcast_publish_to_youtube uploaded (one JSON object per line)."""
        return self.data_dir / "social" / "youtube_uploads.jsonl"

    @property
    def youtube_thumbnail_log_path(self) -> Path:
        """Append-only record of every custom thumbnail set on YouTube, so metrics can mark the change."""
        return self.data_dir / "social" / "youtube_thumbnails.jsonl"

    @property
    def youtube_videos_file(self) -> Path:
        """Optional mapping of the show's other YouTube videos to episodes, for metrics on a back catalog."""
        return self.data_dir / "youtube_videos.json"

    @property
    def x_post_log_path(self) -> Path:
        """Append-only record of every post X accepted (one JSON object per line)."""
        return self.data_dir / "social" / "x_posts.jsonl"

    @property
    def devto_post_log_path(self) -> Path:
        """Append-only record of every dev.to draft the engine created (one JSON object per line)."""
        return self.data_dir / "social" / "devto_posts.jsonl"

    @property
    def substack_post_log_path(self) -> Path:
        """Append-only record of every Substack draft the engine created (one JSON object per line)."""
        return self.data_dir / "social" / "substack_posts.jsonl"

    @property
    def excluded_episodes_file(self) -> Path:
        """Episodes permanently barred from the catalog (slug + GUID)."""
        return self.data_dir / "excluded_episodes.json"


settings = Settings()
