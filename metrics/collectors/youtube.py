"""YouTube collector: channel subscribers plus views and watch time for the show's own videos.

Only tracked videos are measured: uploads made with `podcast_publish_to_youtube`
(logged automatically) and any listed in `$DATA_DIR/youtube_videos.json` (for videos
uploaded another way). The rest of the channel stays out of the numbers.

Two levels, depending on the saved OAuth token's scopes:

- `youtube.readonly` (Data API): lifetime views, likes, and comments per video, and
  channel subscribers, stored as daily snapshots.
- plus `yt-analytics.readonly` (Analytics API): daily views, watch minutes, and
  subscribers gained per video. YouTube finalizes these a few days late, so the last
  week is re-fetched on every run.

Quota: one Data API call per 50 videos, plus one Analytics query per video.
"""

from collections import Counter
from datetime import timedelta
from typing import Any

from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from core.config import settings
from exporters.youtube import ANALYTICS_SCOPE, load_credentials, read_tracked_videos
from metrics.collectors.base import CollectionContext, Collector, CollectorError
from metrics.models import ACCOUNT, CollectionResult, Entity, MetricSpec, Observation

VIDEO = "video"
# videos.list accepts at most 50 ids per call.
DATA_API_BATCH = 50
# YouTube Analytics revises recent days; always re-fetch at least this many.
ANALYTICS_SETTLE_DAYS = 7
# Analytics API metric name → stored metric name.
ANALYTICS_METRICS = {"views": "views", "estimatedMinutesWatched": "watch_minutes", "subscribersGained": "subscribers_gained"}
VIDEO_STATS = {"viewCount": "lifetime_views", "likeCount": "likes", "commentCount": "comments"}
CHANNEL_STATS = {"subscriberCount": "subscribers", "viewCount": "channel_views", "videoCount": "videos"}


def _execute(request: Any, what: str) -> dict[str, Any]:
    """Run a Google API request; HTTP errors become CollectorError with YouTube's reason (no credentials)."""
    try:
        return request.execute()
    except HttpError as e:
        raise CollectorError(f"YouTube refused {what}: HTTP {e.status_code} {e.reason}") from None


class YouTubeCollector(Collector):
    """Subscribers and per-video performance for the show's tracked YouTube videos."""

    name = "youtube"
    description = "Views and watch time for the show's YouTube videos (uploads via the engine + youtube_videos.json)"
    metrics = {
        "views": MetricSpec("YouTube views", "Daily views of the show's videos (needs yt-analytics.readonly)", headline=True),
        "watch_minutes": MetricSpec("Watch minutes", "Estimated minutes watched (needs yt-analytics.readonly)"),
        "subscribers_gained": MetricSpec("Subscribers gained", "From the show's videos (needs yt-analytics.readonly)"),
        "lifetime_views": MetricSpec("Lifetime views", "All-time views per video (Data API)", headline=True),
        "likes": MetricSpec("Likes"),
        "comments": MetricSpec("Comments"),
        "subscribers": MetricSpec("Subscribers", "Channel subscribers", headline=True),
        "channel_views": MetricSpec("Channel views", "All-time views across the whole channel"),
        "videos": MetricSpec("Channel videos"),
    }

    def config_problem(self) -> str | None:
        """Needs the saved OAuth token that YouTube uploads already use."""
        if not settings.youtube_token_path.exists():
            return f"No YouTube OAuth token at {settings.youtube_token_path} (set YOUTUBE_TOKEN_PATH; see ADOPTING.md)."
        return None

    def services(self) -> tuple[Resource, Resource | None]:
        """Data API client, plus an Analytics client when the token carries that scope."""
        creds = load_credentials()
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        analytics = (
            build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
            if creds.has_scopes([ANALYTICS_SCOPE]) else None
        )
        return youtube, analytics

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Snapshot the channel and every tracked video; add daily analytics when the scope allows."""
        youtube, analytics = self.services()
        today = ctx.today.isoformat()
        result = CollectionResult()

        channel = _execute(youtube.channels().list(part="id,snippet,statistics", mine=True), "the channel lookup")
        if not channel.get("items"):
            raise CollectorError("The YouTube token isn't linked to a channel.")
        channel = channel["items"][0]
        result.entities.append(Entity(
            "youtube", ACCOUNT, channel["id"], label=channel["snippet"]["title"],
            url=f"https://www.youtube.com/channel/{channel['id']}",
        ))
        for field_name, metric in CHANNEL_STATS.items():
            if field_name in channel["statistics"]:
                result.observations.append(Observation(
                    "youtube", metric, float(channel["statistics"][field_name]), today,
                    granularity="snapshot", entity_type=ACCOUNT, entity_id=channel["id"],
                ))

        tracked = read_tracked_videos()
        ids = sorted(tracked)
        for start in range(0, len(ids), DATA_API_BATCH):
            reply = _execute(
                youtube.videos().list(part="snippet,statistics", id=",".join(ids[start:start + DATA_API_BATCH])),
                "the video lookup",
            )
            for video in reply.get("items", []):
                self._add_video(result, video, tracked.get(video["id"]), today)
        found = sum(1 for e in result.entities if e.entity_type == VIDEO)
        result.notes.append(f"{found} of {len(tracked)} tracked videos found")

        if analytics is None:
            result.notes.append(f"daily views and watch time need the {ANALYTICS_SCOPE} scope; re-authorize to add it")
        else:
            self._add_analytics(result, analytics, tracked, ctx)
        return result

    @staticmethod
    def _add_video(result: CollectionResult, video: dict[str, Any], slug: str | None, today: str) -> None:
        """One video's entity and its lifetime-count snapshots."""
        result.entities.append(Entity(
            "youtube", VIDEO, video["id"], label=video["snippet"]["title"], url=f"https://youtu.be/{video['id']}",
            created_at=video["snippet"].get("publishedAt", ""), episode_slug=slug,
        ))
        for field_name, metric in VIDEO_STATS.items():
            # Counts the owner hid (e.g. likes) are absent from the reply; record only what's present.
            if field_name in video["statistics"]:
                result.observations.append(Observation(
                    "youtube", metric, float(video["statistics"][field_name]), today,
                    granularity="snapshot", entity_type=VIDEO, entity_id=video["id"], episode_slug=slug,
                ))

    @staticmethod
    def _add_analytics(
        result: CollectionResult, analytics: Resource, tracked: dict[str, str | None], ctx: CollectionContext
    ) -> None:
        """Daily metrics per tracked video (one query each), with show totals summed over them."""
        start = min(ctx.window.start, ctx.today - timedelta(days=ANALYTICS_SETTLE_DAYS))
        end = ctx.window.end - timedelta(days=1)  # the API's endDate is inclusive
        result.replace_start, result.replace_end = start.isoformat(), ctx.window.end_iso
        show_totals: Counter[tuple[str, str]] = Counter()
        for video_id, slug in tracked.items():
            reply = _execute(
                analytics.reports().query(
                    ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                    metrics=",".join(ANALYTICS_METRICS), dimensions="day", filters=f"video=={video_id}",
                ),
                f"analytics for video {video_id}",
            )
            # Columns come back in request order after the `day` dimension.
            for day, *values in reply.get("rows", []):
                for metric, value in zip(ANALYTICS_METRICS.values(), values, strict=True):
                    result.observations.append(Observation(
                        "youtube", metric, float(value), day, entity_type=VIDEO, entity_id=video_id, episode_slug=slug,
                    ))
                    show_totals[(day, metric)] += float(value)
        result.observations += [Observation("youtube", metric, value, day) for (day, metric), value in show_totals.items()]
