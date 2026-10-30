"""Setup checker: verifies a show's configuration end to end before the first publish.

Usage:
    uv run python -m core.doctor

Each check prints PASS, OFF (an optional integration not set up), WARN, or FAIL with the
fix. Exit code 1 if anything failed. `core.capabilities` groups the same checks by channel
for agents.
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
from core.credentials import readable_by_others
from core.net import open_https
from core.storage import R2StorageClient
from exporters.devto import DevtoError, devto_username
from exporters.instagram import InstagramError, verify_account
from exporters.substack_post import SubstackError, substack_publication
from exporters.x_post import XPostError, xurl_command

Check = tuple[str, str]  # (status, detail)


def check_r2_credentials() -> Check:
    """The S3 API keys can reach the bucket."""
    client = R2StorageClient()
    if not client.is_configured():
        return "FAIL", client.config_error or "R2 is not configured."
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
            ("HF backup", bool(settings.hf_backup_repo)),
            ("OP3 analytics", settings.use_op3_prefix),
        ]
        if on
    ]
    return "PASS", f"enabled: {', '.join(enabled) or 'none'}"


def check_youtube() -> Check:
    """YouTube uploads are off until the OAuth token file exists (the token itself is checked on first use)."""
    if not settings.youtube_token_path.exists():
        return "OFF", f"no token at {settings.youtube_token_path} (run the YouTube OAuth step in ADOPTING.md)"
    return "PASS", f"token at {settings.youtube_token_path}"


def check_x_posting() -> Check:
    """X posting is off, or xurl resolves for X_USERNAME (the login itself is checked with `xurl whoami`)."""
    if not settings.x_username:
        return "OFF", "set X_USERNAME and log in with xurl to enable"
    try:
        command = xurl_command()
    except XPostError as e:
        return "WARN", str(e)
    return "PASS", f"posting as @{settings.x_username} via {command[0]}; verify the login with `xurl whoami`"


def check_instagram() -> Check:
    """Instagram posting is off, or the saved token is private, belongs to INSTAGRAM_USERNAME, and still works."""
    if not settings.instagram_username:
        return "OFF", "set INSTAGRAM_USERNAME and run `python -m exporters.instagram login`"
    token_file = settings.instagram_token_path.expanduser()
    # The token controls the whole Instagram account, so a file others can read is a failure, not a warning.
    if readable_by_others(token_file):
        return "FAIL", f"{token_file} is readable by other users; run `chmod 600 {token_file}`."
    try:
        account = verify_account()
    except InstagramError as e:
        return "WARN", str(e)
    saved = account["saved_at"][:10]
    return "PASS", f"posting as @{account['username']} (token saved {saved})"


def check_devto() -> Check:
    """dev.to cross-posting is off, or the API key works and drafts get a canonical URL."""
    if not settings.devto_api_key:
        return "OFF", "set DEVTO_API_KEY and BLOG_URL_TEMPLATE to enable"
    if not settings.blog_url_template:
        return "WARN", "DEVTO_API_KEY is set but BLOG_URL_TEMPLATE isn't; cross-posting refuses without a canonical URL."
    try:
        username = devto_username()
    except DevtoError as e:
        return "WARN", str(e)
    return "PASS", f"drafts go to dev.to/{username}; canonical URL {settings.blog_url_template}"


def check_substack() -> Check:
    """Substack drafts are off, or the saved session cookie is private and still signs in."""
    if not settings.substack_publication_url:
        return "OFF", "set SUBSTACK_PUBLICATION_URL and run `python -m exporters.substack_post login`"
    if not settings.blog_url_template:
        return "WARN", "SUBSTACK_PUBLICATION_URL is set but BLOG_URL_TEMPLATE isn't; cross-posting refuses without it."
    cookies = settings.substack_cookies_path.expanduser()
    # The cookie controls the whole Substack account, so a file others can read is a failure, not a warning.
    if readable_by_others(cookies):
        return "FAIL", f"{cookies} is readable by other users; run `chmod 600 {cookies}`."
    try:
        publication = substack_publication()
    except SubstackError as e:
        return "WARN", str(e)
    return "PASS", f"drafts go to {publication} (private API; the session cookie expires, re-run login then)"


def check_metrics_sources() -> Check:
    """Report which metric collectors can run; WARN if one named in METRICS_SOURCES can't."""
    # Imported here so a broken extra collector only fails this check, not the whole doctor.
    from metrics.registry import available_collectors

    ready, missing = [], []
    for name, cls in available_collectors().items():
        problem = cls().config_problem()
        if problem is None:
            ready.append(name)
        else:
            missing.append((name, problem))
    blocked = [(n, p) for n, p in missing if n in settings.metrics_sources]
    if blocked:
        return "WARN", "; ".join(f"{n}: {p}" for n, p in blocked)
    unset = f"; not configured: {', '.join(n for n, _ in missing)}" if missing else ""
    return "PASS", f"ready: {', '.join(ready) or 'none'}{unset}"


CHECKS: list[tuple[str, Callable[[], Check]]] = [
    ("R2 credentials", check_r2_credentials),
    ("Audio domain", check_public_domain),
    ("Website / feed hosting", check_site),
    ("Cover art", check_artwork),
    ("Feed URL", check_feed_url),
    ("Optional integrations", check_optional),
    ("YouTube", check_youtube),
    ("X posting", check_x_posting),
    ("Instagram posting", check_instagram),
    ("dev.to cross-posting", check_devto),
    ("Substack cross-posting", check_substack),
    ("Metrics sources", check_metrics_sources),
]


def run_check(check: Callable[[], Check]) -> Check:
    """Run one check, turning a crash into a FAIL: a checker must report problems, not raise them (e.g. a malformed R2 account id)."""
    try:
        return check()
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"


def main() -> int:
    """Run every check, print a report, and return the process exit code."""
    print(f"Show: {settings.podcast_title}  (SITE_MODE={settings.site_mode}, data: {settings.data_dir})\n")
    failed = False
    for name, check in CHECKS:
        status, detail = run_check(check)
        failed |= status == "FAIL"
        print(f"  [{status}] {name}: {detail}")
    print("\nReady to publish." if not failed else "\nFix the FAIL items above, then re-run.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
