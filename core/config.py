"""Central settings for the podcast engine, loaded from `.env` via pydantic-settings.

Everything show-specific (identity, domains, website layout, data location) is a
setting, so the code carries no single show's values. Required fields have no
defaults: a fresh clone fails fast with a list of exactly what to configure.

Set `PODCAST_SERVER_ENV_FILE` to load a different env file (e.g. one per show).
"""

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, resolved from this file so the engine works from any checkout location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.environ.get("PODCAST_SERVER_ENV_FILE", PROJECT_ROOT / ".env"))


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

    @model_validator(mode="after")
    def _check_and_normalize(self) -> Self:
        """Fill derived defaults and reject combinations that can't work."""
        self.r2_public_domain = self.r2_public_domain.rstrip("/")
        if self.database_path is None:
            self.database_path = self.data_dir / "catalog.db"
        if self.site_mode == "astro" and self.website_path is None:
            raise ValueError("SITE_MODE=astro needs WEBSITE_PATH (the local checkout of your site repo).")
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
    def excluded_episodes_file(self) -> Path:
        """Episodes permanently barred from the catalog (slug + GUID)."""
        return self.data_dir / "excluded_episodes.json"


settings = Settings()
