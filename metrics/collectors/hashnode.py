"""Hashnode collector: views of the posts this engine cross-posted to the show's Hashnode publication.

Only posts in the engine's Hashnode log (`$DATA_DIR/social/hashnode_posts.jsonl`, written
by `podcast_crosspost_to_hashnode` and by scripted cross-posts) are measured, so posts
written on Hashnode directly stay out of the numbers. One paged read of the publication
covers every post.

Hashnode reports lifetime views, so they're stored as daily snapshots. Drafts are
skipped. A post whose slug is an episode's slug (a companion blog post) is credited to
that episode.
"""

from core.config import settings
from core.jsonl import read_records
from exporters.hashnode import HashnodeError, all_posts, cli_command
from metrics.collectors.base import CollectionContext, Collector, CollectorError
from metrics.models import POST, CollectionResult, Entity, MetricSpec, Observation


class HashnodeCollector(Collector):
    """Lifetime views for every published post in the engine's Hashnode log."""

    name = "hashnode"
    description = "Views of posts cross-posted to Hashnode"
    metrics = {
        "views": MetricSpec("Hashnode views", "Views of cross-posted posts", headline=True),
    }

    def config_problem(self) -> str | None:
        """Needs the same publication host and CLI login cross-posting uses."""
        try:
            cli_command()
        except HashnodeError as e:
            return str(e)
        return None

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot today's view count for each logged, published post."""
        logged = {r["post_id"]: r for r in read_records(settings.hashnode_post_log_path) if r.get("post_id")}
        try:
            posts = [p for p in all_posts() if p.get("id") in logged]
        except HashnodeError as e:
            raise CollectorError(str(e)) from None

        today = ctx.today.isoformat()
        result = CollectionResult()
        for post in posts:
            slug = logged[post["id"]].get("slug")
            episode = slug if slug and ctx.episodes.get(slug) else None
            result.entities.append(Entity(
                "hashnode", POST, post["id"],
                label=post.get("title", ""),
                url=post.get("url", ""),
                created_at=post.get("publishedAt") or "",
                episode_slug=episode,
            ))
            if (views := post.get("views")) is not None:
                result.observations.append(Observation(
                    "hashnode", "views", views, today, granularity="snapshot",
                    entity_type=POST, entity_id=post["id"], episode_slug=episode,
                ))
        result.notes.append(f"{len(posts)} of {len(logged)} logged posts are published and were read")
        return result
