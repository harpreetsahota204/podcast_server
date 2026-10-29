"""dev.to collector: views, reactions, and comments on the articles this engine cross-posted.

Only articles in the engine's dev.to log (`$DATA_DIR/social/devto_posts.jsonl`, written
by `podcast_crosspost_to_devto` and by scripted cross-posts) are measured, so the rest
of the account stays out of the numbers. One API read covers every article.

dev.to reports lifetime totals, so they're stored as daily snapshots. Unpublished
drafts are skipped. An article whose slug is an episode's slug (a companion blog post)
is credited to that episode.
"""

from core.config import settings
from core.jsonl import read_records
from exporters.devto import DevtoError, my_articles
from metrics.collectors.base import CollectionContext, Collector, CollectorError
from metrics.models import POST, CollectionResult, Entity, MetricSpec, Observation

# dev.to article field → stored metric name.
ARTICLE_METRICS = {
    "page_views_count": "views",
    "public_reactions_count": "reactions",
    "comments_count": "comments",
}


class DevtoCollector(Collector):
    """Lifetime counts for every published article in the engine's dev.to log."""

    name = "devto"
    description = "Views, reactions, and comments on articles cross-posted to dev.to"
    metrics = {
        "views": MetricSpec("dev.to views", "Page views of cross-posted articles", headline=True),
        "reactions": MetricSpec("dev.to reactions"),
        "comments": MetricSpec("dev.to comments"),
    }

    def config_problem(self) -> str | None:
        """Needs the same API key cross-posting uses."""
        return None if settings.devto_api_key else "Set DEVTO_API_KEY (dev.to → Settings → Extensions)."

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot today's totals for each logged, published article."""
        logged = {r["article_id"]: r for r in read_records(settings.devto_post_log_path) if r.get("article_id")}
        try:
            articles = [a for a in my_articles() if a.get("published") and a.get("id") in logged]
        except DevtoError as e:
            raise CollectorError(str(e)) from None

        today = ctx.today.isoformat()
        result = CollectionResult()
        for article in articles:
            slug = logged[article["id"]].get("slug")
            episode = slug if slug and ctx.episodes.get(slug) else None
            article_id = str(article["id"])
            result.entities.append(Entity(
                "devto", POST, article_id,
                label=article.get("title", ""),
                url=article.get("url", ""),
                created_at=article.get("published_at") or "",
                episode_slug=episode,
            ))
            for field_name, metric in ARTICLE_METRICS.items():
                if (value := article.get(field_name)) is not None:
                    result.observations.append(Observation(
                        "devto", metric, value, today, granularity="snapshot",
                        entity_type=POST, entity_id=article_id, episode_slug=episode,
                    ))
        result.notes.append(f"{len(articles)} of {len(logged)} logged articles are published and were read")
        return result
