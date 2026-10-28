"""X (Twitter) collector: engagement on the posts this engine published, linked to episodes.

Only posts sent with `podcast_post_to_x` are measured: their ids come from the post
log (`$DATA_DIR/social/x_posts.jsonl`), and the collector looks them up directly.
It never scans the account's timeline, so unrelated posts stay out of the numbers
and each run costs a few API reads (one per 100 posts, plus one for followers).

Two ways to authenticate, checked in this order:

1. `X_USERNAME` with the `xurl` CLI logged in as that account (the same setup
   posting uses). OAuth 2.0 user context adds the owner-only metrics: engagements,
   link clicks, and profile clicks (X reports those for 30 days after posting).
   xurl holds the tokens; the engine never sees them.
2. `X_BEARER_TOKEN` + `X_USERNAME`: an app-only token. Public counts only.

Post metrics are running totals, so they're stored as daily snapshots. A post is
credited to the episode named when it was posted, or else to the episode it links to.
Posts older than `X_LOOKBACK_DAYS` keep their last snapshot and aren't re-read.
"""

import subprocess
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

from core.config import settings
from exporters.x_post import XPostError, decode_xurl_json, read_post_log, xurl_command
from metrics.collectors.base import CollectionContext, Collector, CollectorError, fetch_json
from metrics.episodes import EpisodeIndex
from metrics.models import ACCOUNT, POST, CollectionResult, Entity, MetricSpec, Observation

API_BASE = "https://api.x.com"
XURL_TIMEOUT_SECONDS = 60
# X's maximum ids per post lookup request.
LOOKUP_BATCH = 100
# X reports owner-only metrics for this many days after posting; asking for them on
# older posts makes X return per-post errors instead.
OWNER_METRICS_DAYS = 30
LABEL_CHARS = 200

# X field name → stored metric name.
PUBLIC_POST_METRICS = {
    "impression_count": "impressions",
    "like_count": "likes",
    "retweet_count": "reposts",
    "reply_count": "replies",
    "quote_count": "quotes",
    "bookmark_count": "bookmarks",
}
OWNER_POST_METRICS = {
    "engagements": "engagements",
    "url_link_clicks": "link_clicks",
    "user_profile_clicks": "profile_clicks",
}
ACCOUNT_METRICS = {"followers_count": "followers", "tweet_count": "posts", "listed_count": "listed"}


class XTransport(Protocol):
    """Performs an authenticated GET against the X API v2 and returns the decoded JSON."""

    user_context: bool

    def get(self, path: str) -> dict[str, Any]:
        """GET `path` (e.g. `/2/users/me?...`)."""
        ...


class XurlTransport:
    """Delegates auth to the `xurl` CLI, which stores and refreshes the user's OAuth tokens itself."""

    user_context = True

    def get(self, path: str) -> dict[str, Any]:
        """Run `xurl [--app APP] --username USER PATH` and decode its JSON output (X errors left in place)."""
        try:
            command = xurl_command()
        except XPostError as e:
            raise CollectorError(str(e)) from None
        proc = subprocess.run(
            [*command, path], capture_output=True, text=True, timeout=XURL_TIMEOUT_SECONDS, check=False
        )
        try:
            return decode_xurl_json(proc.stdout or "")
        except XPostError:
            # Non-JSON output means xurl itself failed (not logged in, network); show its own words.
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise CollectorError(f"xurl failed (exit {proc.returncode}): {detail}") from None


class BearerTransport:
    """App-only auth with a bearer token: public metrics only."""

    user_context = False

    def get(self, path: str) -> dict[str, Any]:
        """GET `path` from api.x.com with the bearer token."""
        return fetch_json(f"{API_BASE}{path}", {"Authorization": f"Bearer {settings.x_bearer_token}"})


def _check(reply: dict[str, Any], what: str) -> dict[str, Any]:
    """Raise if X answered with an error document instead of data."""
    if "data" not in reply:
        errors = reply.get("errors") or [reply]
        detail = "; ".join(str(e.get("detail") or e.get("title") or e) for e in errors)
        raise CollectorError(f"X refused {what}: {detail}")
    return reply


def post_links(post: dict[str, Any]) -> list[str]:
    """Every expanded link in a post (X wraps them in t.co; `unwound_url` follows redirects too)."""
    links = []
    for url in post.get("entities", {}).get("urls", []):
        links += [u for u in (url.get("unwound_url"), url.get("expanded_url")) if u]
    return links


def posts_to_refresh(log: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Logged posts from the last X_LOOKBACK_DAYS, one per post id (the log is append-only)."""
    since = (today - timedelta(days=settings.x_lookback_days - 1)).isoformat()
    by_id = {r["tweet_id"]: r for r in log if (r.get("posted_at") or "")[:10] >= since}
    return list(by_id.values())


def post_records(
    posts: list[dict[str, Any]], log_slugs: dict[str, str | None], username: str, episodes: EpisodeIndex, today: str
) -> tuple[list[Entity], list[Observation]]:
    """Entities and today's metric snapshots for looked-up posts.

    The slug recorded at posting time wins; otherwise the first link that resolves to an
    episode is used.
    """
    entities, observations = [], []
    for post in posts:
        post_id = post["id"]
        slug = log_slugs.get(post_id) or next((s for s in map(episodes.slug_for_url, post_links(post)) if s), None)
        entities.append(Entity(
            "x", POST, post_id,
            label=" ".join(post.get("text", "").split())[:LABEL_CHARS],
            url=f"https://x.com/{username}/status/{post_id}",
            created_at=post.get("created_at", ""),
            episode_slug=slug,
        ))
        # Owner-only metrics are absent when X won't report them (app-only auth, older
        # posts); only what's present is recorded.
        for group, mapping in (("public_metrics", PUBLIC_POST_METRICS), ("non_public_metrics", OWNER_POST_METRICS)):
            for field_name, metric in mapping.items():
                value = (post.get(group) or {}).get(field_name)
                if value is not None:
                    observations.append(Observation(
                        "x", metric, value, today, granularity="snapshot",
                        entity_type=POST, entity_id=post_id, episode_slug=slug,
                    ))
    return entities, observations


class XCollector(Collector):
    """Followers plus metrics for the posts `podcast_post_to_x` published."""

    name = "x"
    description = "Engagement on posts published through podcast_post_to_x, credited to episodes"
    metrics = {
        "impressions": MetricSpec("Post impressions", "Times your engine-published posts were seen", headline=True),
        "link_clicks": MetricSpec("Link clicks", "Clicks on links in your posts (needs xurl user login)"),
        "engagements": MetricSpec("Engagements", "All interactions with your posts (needs xurl user login)"),
        "likes": MetricSpec("Likes"),
        "reposts": MetricSpec("Reposts"),
        "replies": MetricSpec("Replies"),
        "quotes": MetricSpec("Quotes"),
        "bookmarks": MetricSpec("Bookmarks"),
        "profile_clicks": MetricSpec("Profile clicks"),
        "followers": MetricSpec("Followers", headline=True),
        "posts": MetricSpec("Total posts"),
        "listed": MetricSpec("Listed"),
    }

    def config_problem(self) -> str | None:
        """Needs X_USERNAME plus either xurl (logged in as that account) or a bearer token."""
        if not settings.x_username:
            return "Set X_USERNAME (no @), with xurl logged in as that account or X_BEARER_TOKEN."
        if self._xurl_ready() or settings.x_bearer_token:
            return None
        return "xurl not found (install it or set XURL_PATH), and no X_BEARER_TOKEN fallback is set."

    @staticmethod
    def _xurl_ready() -> bool:
        """True when the shared xurl setup resolves to a binary."""
        try:
            xurl_command()
        except XPostError:
            return False
        return True

    def transport(self) -> XTransport:
        """The configured auth method; xurl wins because it unlocks owner-only metrics."""
        return XurlTransport() if self._xurl_ready() else BearerTransport()

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot the account's followers, then every logged post from the lookback window."""
        api = self.transport()
        today = ctx.today.isoformat()
        user_fields = urlencode({"user.fields": "public_metrics"})
        lookup = "/2/users/me" if api.user_context else f"/2/users/by/username/{settings.x_username}"
        user = _check(api.get(f"{lookup}?{user_fields}"), "the account lookup")["data"]

        result = CollectionResult(entities=[
            Entity("x", ACCOUNT, user["id"], label=f"@{user['username']}", url=f"https://x.com/{user['username']}")
        ])
        for field_name, metric in ACCOUNT_METRICS.items():
            value = user.get("public_metrics", {}).get(field_name)
            if value is not None:
                result.observations.append(
                    Observation("x", metric, value, today, granularity="snapshot", entity_type=ACCOUNT, entity_id=user["id"])
                )

        logged = posts_to_refresh(read_post_log(), ctx.today)
        posts, partial_errors = self._lookup(api, logged, ctx.today)
        log_slugs = {r["tweet_id"]: r.get("episode_slug") for r in logged}
        entities, observations = post_records(posts, log_slugs, user["username"], ctx.episodes, today)
        result.entities += entities
        result.observations += observations
        linked = sum(1 for e in entities if e.episode_slug)
        result.notes.append(f"{len(posts)} of {len(logged)} logged posts read, {linked} linked to an episode")
        if partial_errors:
            result.notes.append(f"{partial_errors} posts X couldn't return (deleted, or metrics unavailable)")
        return result

    def _lookup(self, api: XTransport, logged: list[dict[str, Any]], today: date) -> tuple[list[dict[str, Any]], int]:
        """Fetch logged posts by id in batches; owner-only metrics are requested only where X reports them."""
        owner_since = (today - timedelta(days=OWNER_METRICS_DAYS - 1)).isoformat()
        recent = {r["tweet_id"] for r in logged if api.user_context and r["posted_at"][:10] >= owner_since}
        older = [r["tweet_id"] for r in logged if r["tweet_id"] not in recent]
        posts: list[dict[str, Any]] = []
        partial_errors = 0
        for ids, owner_fields in ((sorted(recent), True), (older, False)):
            fields = "created_at,public_metrics,entities" + (",non_public_metrics" if owner_fields else "")
            for start in range(0, len(ids), LOOKUP_BATCH):
                batch = ",".join(ids[start:start + LOOKUP_BATCH])
                reply = api.get(f"/2/tweets?{urlencode({'ids': batch, 'tweet.fields': fields})}")
                # Per-post errors (e.g. deleted posts) arrive with `data`, or alone when every post in the
                # batch is gone. A reply with neither is an error document (auth, rate limit): fail the run.
                if "data" not in reply and "errors" not in reply:
                    _check(reply, "the post lookup")
                posts += reply.get("data", [])
                partial_errors += len(reply.get("errors", []))
        return posts, partial_errors
