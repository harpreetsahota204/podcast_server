"""Substack collector: views, opens, clicks, and growth on the posts this engine made on Substack.

Only posts in the engine's Substack logs are measured (`$DATA_DIR/social/substack_posts.jsonl`
from `podcast_crosspost_to_substack`, and `substack_newsletters.jsonl` from
`podcast_draft_newsletter`), so the publication's other posts stay out of the numbers.
A logged draft counts once it's published; drafts are skipped.

Substack has no stats API: the numbers come from the per-post `stats` its own dashboard
uses, read through the private web API with the saved session cookie (the same path as
cross-posting). If Substack changes that API, this source fails on its own and the
others keep collecting. Stats are lifetime totals, stored as daily snapshots. A post
whose slug is an episode's slug (a companion blog post) is credited to that episode.
"""

from core.config import settings
from core.jsonl import read_records
from exporters.substack_post import SubstackError, published_posts
from metrics.collectors.base import CollectionContext, Collector, CollectorError
from metrics.models import POST, CollectionResult, Entity, MetricSpec, Observation

# Field inside a post's `stats` → stored metric name.
STATS_METRICS = {
    "views": "views",
    "opened": "opens",
    "delivered": "emails_delivered",
    "clicks": "clicks",
    "shares": "shares",
    "signups": "signups",
    "subscribes": "paid_subscribes",
}
# Top-level post field → stored metric name.
POST_METRICS = {"reaction_count": "reactions", "comment_count": "comments"}


class SubstackCollector(Collector):
    """Lifetime stats for every published post in the engine's Substack logs."""

    name = "substack"
    description = "Views, email opens, clicks, and signups on posts the engine cross-posted or sent on Substack"
    metrics = {
        "views": MetricSpec("Substack views", "Web and app views of the engine's posts", headline=True),
        "opens": MetricSpec("Email opens", "Unique subscribers who opened the email"),
        "emails_delivered": MetricSpec("Emails delivered"),
        "clicks": MetricSpec("Email clicks"),
        "shares": MetricSpec("Shares"),
        "signups": MetricSpec("Signups", "New free subscribers attributed to the post"),
        "paid_subscribes": MetricSpec("Paid subscribes"),
        "reactions": MetricSpec("Likes"),
        "comments": MetricSpec("Comments"),
    }

    def config_problem(self) -> str | None:
        """Needs the publication URL and the session cookie saved by `python -m exporters.substack_post login`."""
        if not settings.substack_publication_url:
            return "Set SUBSTACK_PUBLICATION_URL and run `uv run python -m exporters.substack_post login`."
        if not settings.substack_cookies_path.expanduser().is_file():
            return "No Substack session cookie; run `uv run python -m exporters.substack_post login`."
        return None

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot today's stats for each logged post that's been published."""
        # A newsletter logs several events per draft; the id and slug maps below keep one entry each.
        records = read_records(settings.substack_post_log_path) + read_records(settings.substack_newsletter_log_path)
        log = [r for r in records if r.get("draft_id")]
        # A draft keeps its id when published; the slug (set by the engine) is the fallback match.
        logged_ids = {r["draft_id"]: r for r in log}
        logged_slugs = {r["slug"]: r for r in log if r.get("slug")}
        try:
            posts = published_posts() if log else []
        except SubstackError as e:
            raise CollectorError(str(e)) from None

        today = ctx.today.isoformat()
        result = CollectionResult()
        for post in posts:
            record = logged_ids.get(post.get("id")) or logged_slugs.get(post.get("slug"))
            if record is None:
                continue
            slug = record.get("slug")
            episode = slug if slug and ctx.episodes.get(slug) else None
            post_id = str(post["id"])
            result.entities.append(Entity(
                "substack", POST, post_id,
                label=post.get("title", ""),
                url=post.get("canonical_url", ""),
                created_at=post.get("post_date") or "",
                episode_slug=episode,
            ))
            values = [(metric, (post.get("stats") or {}).get(field)) for field, metric in STATS_METRICS.items()]
            values += [(metric, post.get(field)) for field, metric in POST_METRICS.items()]
            for metric, value in values:
                if value is not None:
                    result.observations.append(Observation(
                        "substack", metric, value, today, granularity="snapshot",
                        entity_type=POST, entity_id=post_id, episode_slug=episode,
                    ))
        result.notes.append(f"{len(result.entities)} of {len(logged_ids)} logged posts are published and were read")
        return result
