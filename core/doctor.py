"""Setup checker: verifies a show's configuration end to end before the first publish.

Usage:
    PYTHONPATH=. .venv/bin/python -m core.doctor

Each check prints PASS, WARN, or FAIL with the fix. Exit code 1 if anything failed.
Settings themselves are validated on import, so a missing required variable
fails before this module runs, with pydantic listing exactly which ones.
"""

import json
import socket
import subprocess
import sys
from collections.abc import Callable
from urllib.parse import urlparse

from core.config import settings
from core.net import open_https
from core.storage import R2StorageClient

Check = tuple[str, str]  # (status, detail)


def check_r2_credentials() -> Check:
    """The S3 API keys can reach the bucket."""
    client = R2StorageClient()
    if not client.is_configured():
        return "FAIL", "R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not all set."
    try:
        client.s3_client.head_bucket(Bucket=settings.r2_bucket_name)
    except Exception as e:
        return "FAIL", f"Cannot access bucket '{settings.r2_bucket_name}': {e}"
    return "PASS", f"bucket '{settings.r2_bucket_name}' reachable"


def _resolves_publicly(host: str) -> bool:
    """Ask Cloudflare's DNS-over-HTTPS resolver, bypassing the local resolver's cache."""
    try:
        with open_https(f"https://cloudflare-dns.com/dns-query?name={host}&type=A", {"Accept": "application/dns-json"}) as resp:
            return bool(json.load(resp).get("Answer"))
    except Exception:
        return False


def check_public_domain() -> Check:
    """The public audio domain resolves, serves TLS, and isn't the rate-limited r2.dev URL."""
    host = urlparse(settings.r2_public_domain).hostname or ""
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        # Distinguish "not set up" from a local resolver still caching an old NXDOMAIN.
        if _resolves_publicly(host):
            return "WARN", f"{host} resolves publicly but not on this machine (local DNS cache); it will clear on its own."
        return "FAIL", f"{host} does not resolve. Connect it under R2 bucket → Settings → Custom Domains."
    if host.endswith(".r2.dev"):
        return "WARN", "r2.dev URLs are rate-limited and not for production; connect a custom domain."
    return "PASS", f"{host} resolves"


def check_site() -> Check:
    """The website repo (astro) or feed hosting (none) is set up consistently."""
    if settings.site_mode == "none":
        expected = f"{settings.r2_public_domain}/{settings.feed_object_key}"
        if settings.podcast_feed_url != expected:
            return "WARN", f"SITE_MODE=none uploads the feed to {expected}, but PODCAST_FEED_URL is {settings.podcast_feed_url}."
        return "PASS", f"feed will be hosted at {expected}"

    repo = settings.website_path
    if repo is None or not (repo / ".git").exists():
        return "FAIL", f"WEBSITE_PATH={repo} is not a git checkout."
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    ).stdout.strip()
    if branch != settings.website_branch:
        return "WARN", f"website repo is on '{branch}'; publishes only deploy from '{settings.website_branch}'."
    return "PASS", f"{repo} on '{branch}'; episodes → {settings.website_episodes_dir}, feed → {settings.website_rss_file}"


def check_feed_url() -> Check:
    """The public feed URL answers (expected to fail before the first publish)."""
    try:
        with open_https(settings.podcast_feed_url) as resp:
            return "PASS", f"{settings.podcast_feed_url} → HTTP {resp.status}"
    except Exception as e:
        return "WARN", f"{settings.podcast_feed_url} not reachable yet ({e}); normal before the first publish."


def check_artwork() -> Check:
    """Cover art is reachable; Apple rejects feeds with broken artwork."""
    try:
        with open_https(settings.podcast_image_url) as resp:
            kind = resp.headers.get("Content-Type", "")
    except Exception as e:
        return "FAIL", f"PODCAST_IMAGE_URL not reachable ({e}). Apple requires 1400-3000 px square JPEG/PNG."
    if not kind.startswith("image/"):
        return "FAIL", f"PODCAST_IMAGE_URL returned {kind or 'no content type'}, not an image."
    return "PASS", f"artwork reachable ({kind})"


def check_optional() -> Check:
    """Report which optional integrations are configured."""
    enabled = [
        name
        for name, on in [
            ("Descript", bool(settings.descript_api_bearer or settings.descript_api_key)),
            ("YouTube", settings.youtube_token_path.exists()),
            ("HF backup", bool(settings.hf_backup_repo)),
            ("OP3 analytics", settings.use_op3_prefix),
        ]
        if on
    ]
    return "PASS", f"enabled: {', '.join(enabled) or 'none'}"


CHECKS: list[tuple[str, Callable[[], Check]]] = [
    ("R2 credentials", check_r2_credentials),
    ("Audio domain", check_public_domain),
    ("Website / feed hosting", check_site),
    ("Cover art", check_artwork),
    ("Feed URL", check_feed_url),
    ("Optional integrations", check_optional),
]


def main() -> int:
    """Run every check, print a report, and return the process exit code."""
    print(f"Show: {settings.podcast_title}  (SITE_MODE={settings.site_mode}, data: {settings.data_dir})\n")
    failed = False
    for name, check in CHECKS:
        # A checker must report problems, not crash on them (e.g. a malformed R2 account id).
        try:
            status, detail = check()
        except Exception as e:
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
        failed |= status == "FAIL"
        print(f"  [{status}] {name}: {detail}")
    print("\nReady to publish." if not failed else "\nFix the FAIL items above, then re-run.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
