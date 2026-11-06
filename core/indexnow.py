"""Tells search engines about website pages a deploy changed, through IndexNow.

IndexNow reaches Bing, Yandex, Seznam, Naver, and the other engines that share
submissions; Google doesn't take part and finds new pages through the site's sitemap.

After `core.site.deploy_paths` pushes the website, the host (e.g. Netlify) still has
to build and publish it, so `start_announce` runs detached: it waits until every URL
answers HTTP 200, submits the live ones in one request, and appends the outcome to
`$DATA_DIR/social/indexnow.jsonl`. An MCP call never waits on the host's build.

Usage:
    uv run python -m core.indexnow URL [URL ...]          # wait until each page is live, then submit
    uv run python -m core.indexnow --now URL [URL ...]    # submit at once (pages already live)
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from core.config import PROJECT_ROOT, settings
from core.jsonl import append_record, read_records
from core.net import open_https
from exporters.blog_post import read_titled_markdown

# The shared endpoint forwards each submission to every participating engine.
API_ENDPOINT = "https://api.indexnow.org/indexnow"
# 200 means accepted; 202 means accepted while the engine is still verifying the key file.
ACCEPTED_STATUSES = {200, 202}
# The protocol's limit for one request.
MAX_URLS_PER_SUBMISSION = 10_000
# A changed page already answers 200 with its old content, so give the host time to publish the new build first.
FIRST_CHECK_DELAY_SECONDS = 120
POLL_INTERVAL_SECONDS = 20
# Long enough for a slow build queue; a page still down after this isn't submitted.
LIVE_TIMEOUT_SECONDS = 15 * 60
MAX_WAIT_MINUTES = (FIRST_CHECK_DELAY_SECONDS + LIVE_TIMEOUT_SECONDS) // 60
# Output of the detached run (tracebacks included), next to the JSONL record it writes.
RUN_LOG_NAME = "indexnow.log"


def config_problem() -> str | None:
    """Why IndexNow can't run with the current settings, or None when it can."""
    if settings.site_mode != "astro":
        return "IndexNow announces website pages; SITE_MODE=none has no website."
    if not settings.indexnow_key:
        return f"Set INDEXNOW_KEY and serve the same key as {settings.site_origin}/<key>.txt to enable search-engine pings."
    return None


def key_location() -> str:
    """Public URL of the key file that proves the engine may submit URLs for this host."""
    return f"{settings.site_origin}/{settings.indexnow_key}.txt"


def validate_urls(urls: list[str]) -> list[str]:
    """Return the URLs deduplicated in order; raise ValueError for any that isn't under the site's origin (scheme and host)."""
    site_origin = settings.site_origin
    unique = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    # Engines reject a submission that mixes hosts, so a stray URL would sink the whole batch.
    if foreign := [url for url in unique if not url.startswith(f"{site_origin}/")]:
        raise ValueError(f"IndexNow only takes URLs under {site_origin}/: {', '.join(foreign)}")
    if len(unique) > MAX_URLS_PER_SUBMISSION:
        raise ValueError(f"IndexNow takes at most {MAX_URLS_PER_SUBMISSION} URLs per submission; got {len(unique)}.")
    return unique


def episode_page_url(slug: str) -> str:
    """Public URL of an episode page in astro mode (the same link the feed gives each item)."""
    return f"{settings.podcast_link.rstrip('/')}/{slug}/"


def _is_public_post(relative_path: str) -> bool:
    """True when the blog post at this repo-relative path has a title and isn't a draft, so the site builds a page for it."""
    try:
        frontmatter, _ = read_titled_markdown(settings.website_path / relative_path)
    except (OSError, ValueError):
        return False
    return not frontmatter.get("draft")


def urls_for_changed_files(changed_files: list[str]) -> list[str]:
    """Map website files a deploy added or changed (repo-relative) to the public pages they produce.

    Episode pages map to `<PODCAST_LINK>/<slug>/`, and any episode change adds the podcast
    landing page, which lists them. Blog posts map through BLOG_URL_TEMPLATE, drafts excluded.
    Other files (the feed, assets) produce no page worth a recrawl.
    """
    episodes_dir = settings.website_episodes_dir.strip("/")
    blog_dir = settings.website_blog_dir.strip("/")
    urls: list[str] = []
    episodes_changed = False
    for relative in changed_files:
        path = PurePosixPath(relative)
        if path.suffix != ".md":
            continue
        # Episode pages: one Markdown file per episode, named by slug.
        if str(path.parent) == episodes_dir:
            urls.append(episode_page_url(path.stem))
            episodes_changed = True
        # Blog posts: only ones the site actually publishes, at the URL cross-posts also use.
        elif str(path.parent) == blog_dir and settings.blog_url_template and _is_public_post(relative):
            urls.append(settings.blog_url_template.replace("{slug}", path.stem))
    if episodes_changed:
        urls.append(f"{settings.podcast_link.rstrip('/')}/")
    return urls


def page_status(url: str) -> int | str:
    """The HTTP status the public URL answers with now, or the error's name if it can't be reached."""
    try:
        with open_https(url) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception as error:  # DNS, TLS, and timeouts all mean "not live yet"
        return type(error).__name__


def wait_until_live(
    urls: list[str],
    first_delay: float = FIRST_CHECK_DELAY_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = LIVE_TIMEOUT_SECONDS,
) -> dict[str, int | str]:
    """Poll each URL until it answers 200 or the timeout passes; return the last status seen for each."""
    statuses: dict[str, int | str] = {}
    time.sleep(first_delay)
    deadline = time.monotonic() + timeout
    pending = list(urls)
    # Re-check only the pages that aren't live yet, until all are or time runs out.
    while pending:
        for url in pending:
            statuses[url] = page_status(url)
        pending = [url for url in pending if statuses[url] != 200]
        if not pending or time.monotonic() + interval > deadline:
            break
        time.sleep(interval)
    return statuses


def submit(urls: list[str]) -> dict[str, Any]:
    """Send one IndexNow submission for `urls`; return the HTTP status, plus the engine's message on a refusal."""
    payload = {
        "host": urlparse(settings.site_origin).netloc,
        "key": settings.indexnow_key,
        "keyLocation": key_location(),
        "urlList": urls,
    }
    try:
        with open_https(
            API_ENDPOINT,
            {"Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(payload).encode("utf-8"),
        ) as response:
            return {"http_status": response.status}
    except urllib.error.HTTPError as error:
        # 403: the key file doesn't match; 422: a URL isn't on the host; 429: too many submissions.
        return {"http_status": error.code, "error": error.read().decode("utf-8", errors="replace")[:300]}


def announce(urls: list[str], wait: bool = True) -> dict[str, Any]:
    """Submit the pages that are live (after waiting for them unless `wait` is False) and log the outcome."""
    if problem := config_problem():
        raise ValueError(problem)
    urls = validate_urls(urls)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    # 1. Only live pages are submitted: a crawler sent to a 404 drops the URL until it finds it again on its own.
    statuses = wait_until_live(urls) if wait else {}
    live = [url for url in urls if not wait or statuses.get(url) == 200]
    not_live = {url: statuses.get(url) for url in urls if url not in live}

    # 2. One request for every live page, then a log record whatever happened.
    record: dict[str, Any] = {"started_at": started_at, "urls": urls, "live": live, "not_live": not_live}
    if live:
        response = submit(live)
        record.update(response)
        record["status"] = "submitted" if response["http_status"] in ACCEPTED_STATUSES else "refused"
    else:
        record["status"] = "nothing_live"
    record["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if log_error := append_record(settings.indexnow_log_path, record):
        record["log_error"] = log_error
    return record


def start_announce(urls: list[str]) -> dict[str, Any]:
    """Start a detached run that waits for `urls` to go live and submits them; return at once with what started.

    Returns `off` (with the reason) when IndexNow isn't configured, so a deploy reports it instead of failing.
    """
    if problem := config_problem():
        return {"status": "off", "reason": problem}
    urls = validate_urls(urls)
    if not urls:
        return {"status": "nothing_to_announce"}

    settings.indexnow_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.indexnow_log_path.parent / RUN_LOG_NAME, "a", encoding="utf-8") as run_log:
        # Own session so the run outlives an MCP client disconnect; output goes to the run log,
        # never to this process's stdout (the MCP stream).
        process = subprocess.Popen(
            [sys.executable, "-m", "core.indexnow", *urls],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=run_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "status": "started",
        "pid": process.pid,
        "urls": urls,
        "log_path": str(settings.indexnow_log_path),
        "next_step": f"Each page is checked until it answers HTTP 200 (up to {MAX_WAIT_MINUTES} minutes), then "
        "submitted; podcast_announce_urls(check_only=true) shows the outcome.",
    }


def latest_announcement() -> dict[str, Any]:
    """The most recent finished IndexNow run from the log, or `never_run`."""
    records = read_records(settings.indexnow_log_path)
    if not records:
        return {"status": "never_run", "log_path": str(settings.indexnow_log_path)}
    return {**records[-1], "log_path": str(settings.indexnow_log_path)}


def main() -> int:
    """Command-line entry point (also what `start_announce` runs detached); print the logged record as JSON."""
    parser = argparse.ArgumentParser(description="Submit website URLs to IndexNow once they are live.")
    parser.add_argument("urls", nargs="+", help="https URLs on the site's own host")
    parser.add_argument("--now", action="store_true", help="submit at once instead of waiting for HTTP 200")
    args = parser.parse_args()
    record = announce(args.urls, wait=not args.now)
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "submitted" else 1


if __name__ == "__main__":
    sys.exit(main())
