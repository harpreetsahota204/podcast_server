"""Publishes the feed (and, in astro mode, episode pages) and deploys them.

SITE_MODE=astro  Writes the feed and one Markdown page per episode into a local
                 checkout of the website repo, then deploys by committing only
                 those generated files and pushing the configured branch (for
                 hosts that build from git: Netlify, Vercel, Cloudflare Pages).
SITE_MODE=none   Uploads the feed to R2 under `feed_object_key` with a short
                 cache lifetime; no website required.

Run `python -m core.site` to rebuild outputs without deploying.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from core.config import settings
from core.db import CatalogDB
from core.feed_generator import build_rss_xml, write_atomic
from core.storage import R2StorageClient
from exporters.astro_markdown import export_astro_markdown

logger = logging.getLogger("site")

# Podcast apps poll feeds; five minutes keeps new episodes visible quickly while still caching.
FEED_CACHE = "public, max-age=300"
# A hung `git push` (e.g. waiting on credentials) must not block an MCP call forever.
GIT_TIMEOUT_SECONDS = 120


def rebuild_site(db: CatalogDB) -> dict[str, Any]:
    """Regenerate the feed once and publish it (plus episode pages) for the configured site mode.

    Episodes whose audio isn't in R2 yet (e.g. imported but not mirrored) are left out,
    so the public feed never advertises a download that would 404.
    """
    xml_content = build_rss_xml([ep for ep in db.list_episodes() if ep["audio_synced"]])
    write_atomic(settings.rss_output_path, xml_content)
    result: dict[str, Any] = {
        "rss_path": str(settings.rss_output_path),
        "rss_size_bytes": len(xml_content.encode("utf-8")),
    }

    if settings.site_mode == "astro":
        write_atomic(settings.site_rss_path, xml_content)
        result["episode_pages_changed"] = export_astro_markdown(db)
    else:
        result["feed_url"] = R2StorageClient().upload_file(
            settings.rss_output_path,
            settings.feed_object_key,
            content_type="application/rss+xml; charset=utf-8",
            cache_control=FEED_CACHE,
        )
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a non-interactive git command in `repo`; with `check`, raise CalledProcessError on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def deploy_paths(paths: list[Path], message: str) -> dict[str, Any]:
    """Commit and push only `paths` in the website repo.

    Only files this engine writes are staged and committed (a pathspec commit), so
    unrelated work-in-progress in the website repo is never published. Deploys are
    skipped unless the repo is on the configured branch.
    """
    repo = settings.website_path
    if repo is None or not (repo / ".git").exists():
        return {"status": "skipped", "reason": f"{repo} is not a git repository"}
    branch_wanted = settings.website_branch

    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != branch_wanted:
            return {"status": "skipped", "reason": f"website repo is on '{branch}', not '{branch_wanted}'"}

        pathspecs = [str(p.relative_to(repo)) for p in paths if p.exists()]
        # An empty pathspec would make the diff/commit below act on the whole index.
        if not pathspecs:
            return {"status": "skipped", "reason": "no generated website files found"}
        _git(repo, "add", "--", *pathspecs)
        # `diff --cached --quiet` exits 0 when nothing under our paths changed.
        if _git(repo, "diff", "--cached", "--quiet", "--", *pathspecs, check=False).returncode == 0:
            return {"status": "no_changes"}

        _git(repo, "commit", "-m", message, "--", *pathspecs)
        _git(repo, "push", "origin", branch_wanted)
    except subprocess.CalledProcessError as e:
        return {"status": "failed", "error": (e.stderr or str(e)).strip()}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": f"git timed out after {GIT_TIMEOUT_SECONDS}s"}
    return {"status": "pushed", "commit": message, "paths": pathspecs}


def deploy_site(message: str, extra_paths: list[Path] | None = None) -> dict[str, Any]:
    """Deploy the generated feed and episode pages (plus `extra_paths`, e.g. a blog post)."""
    if settings.site_mode != "astro":
        return {"status": "not_applicable", "reason": "SITE_MODE=none: the feed is live once uploaded to R2"}
    return deploy_paths([settings.site_rss_path, settings.site_episodes_dir, *(extra_paths or [])], message)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    with CatalogDB(settings.database_path) as catalog:
        logger.info("Rebuilt: %s", rebuild_site(catalog))
