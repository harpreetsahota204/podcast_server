"""Instagram collector: followers plus engagement on the posts this engine published, linked to episodes.

Only posts sent with `podcast_post_to_instagram` are measured: their media ids come from
the post log (`$DATA_DIR/social/instagram_posts.jsonl`), minus the posts the engine has
since deleted. The account's other posts stay out of the numbers.

It reads with the token posting already saved (`python -m exporters.instagram login`),
at two levels depending on that token's permissions:

- `instagram_basic` (posting needs it anyway): the account's followers, following, and
  post count, and each post's likes and comments.
- plus `instagram_manage_insights`: each post's views, reach, saves, shares, and total
  interactions, and for feed posts the profile visits and follows they led to. Without
  it, the run keeps the basic counts and its notes say what to add.

Every number is a running total, stored as a daily snapshot. A post is credited to the
episode named when it was posted. Cost: one read for the account, plus up to two per
logged post (its fields, then its insights).
"""

from typing import Any
from urllib.parse import urlencode

from exporters.instagram import InstagramError, call_graph, live_post_log, load_token
from metrics.collectors.base import CollectionContext, Collector, CollectorError
from metrics.episodes import EpisodeIndex
from metrics.models import ACCOUNT, POST, CollectionResult, Entity, MetricSpec, Observation

LABEL_CHARS = 200
# Graph field → stored metric name, for the account and for each post (both need only instagram_basic).
ACCOUNT_FIELDS = {"followers_count": "followers", "follows_count": "following", "media_count": "posts"}
POST_FIELDS = {"like_count": "likes", "comments_count": "comments"}
# Insight metric → stored metric name for feed posts. Meta rejects the whole request if one metric doesn't
# apply to the post's type, and the engine publishes only feed posts, so other types are read without insights.
FEED_INSIGHTS = {
    "views": "views",
    "reach": "reach",
    "saved": "saves",
    "shares": "shares",
    "total_interactions": "interactions",
    "profile_visits": "profile_visits",
    "follows": "follows",
}
INSIGHTS_SCOPE = "instagram_manage_insights"


def insight_values(reply: dict[str, Any], names: dict[str, str]) -> dict[str, float]:
    """Stored metric name → value from an `/insights` reply, for the insight metrics in `names`.

    Meta answers newer metrics as `total_value: {value}` and older ones as `values: [{value}]`;
    both are accepted, and a metric with neither is left out rather than recorded as zero.
    """
    found = {}
    for item in reply.get("data", []):
        value = (item.get("total_value") or {}).get("value")
        if value is None:
            value = ((item.get("values") or [{}])[0]).get("value")
        if value is not None and item.get("name") in names:
            found[names[item["name"]]] = value
    return found


def post_records(
    post: dict[str, Any], insights: dict[str, float], logged: dict[str, Any], episodes: EpisodeIndex, today: str
) -> tuple[Entity, list[Observation]]:
    """One post's entity and today's snapshots of its counts and its insights (keyed by stored metric name).

    The episode named at posting time is kept only if it's still in the catalog. Counts
    the owner hid (e.g. likes) are absent from the reply, so only present values are recorded.
    """
    media_id = str(logged["media_id"])
    slug = logged.get("episode_slug")
    episode = slug if slug and episodes.get(slug) else None
    entity = Entity(
        "instagram", POST, media_id,
        label=" ".join((post.get("caption") or "").split())[:LABEL_CHARS],
        url=post.get("permalink") or logged.get("url") or "",
        created_at=post.get("timestamp") or logged.get("posted_at") or "",
        episode_slug=episode,
    )
    # Counts from the post's own fields, then whatever insights were read.
    values = [(metric, post.get(field_name)) for field_name, metric in POST_FIELDS.items()]
    values += list(insights.items())
    observations = [
        Observation(
            "instagram", metric, value, today, granularity="snapshot",
            entity_type=POST, entity_id=media_id, episode_slug=episode,
        )
        for metric, value in values
        if value is not None
    ]
    return entity, observations


class InstagramCollector(Collector):
    """Followers plus metrics for the posts `podcast_post_to_instagram` published."""

    name = "instagram"
    description = "Engagement on posts published through podcast_post_to_instagram, credited to episodes"
    metrics = {
        "views": MetricSpec(
            "Instagram views", f"Times your engine-published posts were seen (needs {INSIGHTS_SCOPE})", headline=True
        ),
        "reach": MetricSpec("Instagram reach", f"Accounts that saw each post (needs {INSIGHTS_SCOPE})"),
        "interactions": MetricSpec(
            "Instagram interactions", f"Likes, comments, saves, and shares (needs {INSIGHTS_SCOPE})"
        ),
        "likes": MetricSpec("Instagram likes"),
        "comments": MetricSpec("Instagram comments"),
        "saves": MetricSpec("Instagram saves"),
        "shares": MetricSpec("Instagram shares"),
        "profile_visits": MetricSpec("Profile visits", "Visits to the profile from feed posts"),
        "follows": MetricSpec("Follows from posts", "Accounts that followed after seeing a feed post"),
        "followers": MetricSpec("Instagram followers", headline=True),
        "following": MetricSpec("Instagram following"),
        "posts": MetricSpec("Instagram posts", "All posts on the account"),
    }

    def config_problem(self) -> str | None:
        """Needs INSTAGRAM_USERNAME and the token file `login` saved for that account (checked without a request)."""
        try:
            load_token()
        except InstagramError as e:
            return str(e)
        return None

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot the account, then every logged post still live; insights stop at the first refusal."""
        token = load_token()
        access_token, today = token["access_token"], ctx.today.isoformat()
        try:
            # An invalid token fails here, which fails the whole run with Meta's fix-it message.
            account = call_graph(f"{token['user_id']}?fields=username,{','.join(ACCOUNT_FIELDS)}", access_token)
        except InstagramError as e:
            raise CollectorError(str(e)) from None

        username = account.get("username") or token["username"]
        result = CollectionResult(entities=[Entity(
            "instagram", ACCOUNT, token["user_id"], label=f"@{username}", url=f"https://www.instagram.com/{username}/"
        )])
        result.observations += [
            Observation("instagram", metric, account[field_name], today, granularity="snapshot",
                        entity_type=ACCOUNT, entity_id=token["user_id"])
            for field_name, metric in ACCOUNT_FIELDS.items()
            if account.get(field_name) is not None
        ]

        # The log is append-only; the latest record per post wins.
        by_id = {str(record["media_id"]): record for record in live_post_log()}
        unavailable, insights_error = 0, None
        for media_id, record in by_id.items():
            try:
                post = call_graph(f"{media_id}?fields=timestamp,permalink,caption,media_product_type,"
                                  f"{','.join(POST_FIELDS)}", access_token)
            except InstagramError:
                # Deleted in the app, or otherwise gone: the post keeps its last snapshot.
                unavailable += 1
                continue
            insights: dict[str, float] = {}
            # A refusal here is almost always the token's missing permission, which would refuse every post
            # the same way, so insights stop for the run and the basic counts are still recorded.
            if post.get("media_product_type") == "FEED" and insights_error is None:
                try:
                    reply = call_graph(f"{media_id}/insights?{urlencode({'metric': ','.join(FEED_INSIGHTS)})}",
                                       access_token)
                    insights = insight_values(reply, FEED_INSIGHTS)
                except InstagramError as e:
                    insights_error = str(e)
            entity, observations = post_records(post, insights, record, ctx.episodes, today)
            result.entities.append(entity)
            result.observations += observations

        read = len(by_id) - unavailable
        linked = sum(1 for e in result.entities if e.entity_type == POST and e.episode_slug)
        result.notes.append(f"{read} of {len(by_id)} logged posts read, {linked} linked to an episode")
        if unavailable:
            result.notes.append(f"{unavailable} posts Instagram couldn't return (deleted in the app?)")
        if insights_error:
            result.notes.append(
                f"post insights skipped ({insights_error}); views, reach, saves, and shares need {INSIGHTS_SCOPE} "
                "on the token: add it to the app, generate a new token, and run login again"
            )
        return result
