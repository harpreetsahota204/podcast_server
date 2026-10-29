"""Tests for the metrics pipeline: store semantics, episode matching, collectors, pipeline, analysis, report.

Collectors are exercised against canned API replies (no network); every store and
catalog lives in a temp directory.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlparse

from core.config import settings
from core.db import CatalogDB
from metrics.collect import OVERLAP_DAYS, plan_window, run_one
from metrics.collectors.base import CollectionContext, CollectionWindow, Collector, CollectorError, fetch_json
from metrics.collectors.cloudflare import CloudflareCollector, aggregate_traffic, classify_agent
from metrics.collectors.op3 import OP3Collector, aggregate_downloads
from exporters.x_post import XPostError
from metrics.collectors.x import BearerTransport, XCollector, XurlTransport, post_records
from metrics.episodes import EpisodeIndex
from metrics.models import ACCOUNT, EPISODE, POST, CollectionResult, Entity, MetricSpec, Observation
from metrics.registry import load_collector_class, select_collectors
from metrics.store import MetricsStore

TODAY = date(2026, 3, 10)


def catalog_row(slug: str, published_at: str = "2026-03-01T00:00:00+00:00", size: int = 1000) -> dict[str, Any]:
    """A catalog row with the fields the metrics layer reads, using the fake show's domains."""
    return {
        "id": slug, "guid": f"g-{slug}", "title": slug.replace("-", " ").title(), "slug": slug,
        "published_at": published_at, "audio_url": f"https://audio.example.com/episodes/{slug}.mp3",
        "audio_op3_url": f"https://op3.dev/e/audio.example.com/episodes/{slug}.mp3",
        "duration_seconds": 60.0, "file_size_bytes": size, "audio_synced": 1,
    }


def make_index() -> EpisodeIndex:
    """Two-episode index: ep-a (1000 bytes) and ep-b (500 bytes)."""
    return EpisodeIndex([catalog_row("ep-a"), catalog_row("ep-b", "2026-03-05T00:00:00+00:00", 500)])


def make_ctx(start: date = date(2026, 3, 1), end: date = date(2026, 3, 11)) -> CollectionContext:
    """Collection context over [start, end) with the two-episode index."""
    return CollectionContext(window=CollectionWindow(start, end), episodes=make_index(), today=TODAY)


class TempDirTestCase(unittest.TestCase):
    """Gives each test a temp dir with settings pointed at a fresh catalog and metrics store."""

    def setUp(self) -> None:
        """Create the temp dir and redirect the catalog and metrics paths into it."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (settings.database_path, settings.metrics_db_path)
        settings.database_path = self.tmp / "catalog.db"
        settings.metrics_db_path = self.tmp / "metrics.db"

    def tearDown(self) -> None:
        """Restore settings and delete the temp dir."""
        settings.database_path, settings.metrics_db_path = self._saved
        shutil.rmtree(self.tmp)


class StoreTests(TempDirTestCase):
    """Re-collecting must replace numbers, never add to them."""

    def test_replaced_window_drops_stale_rows_and_keeps_older_days(self) -> None:
        """A re-fetched range replaces its day rows (even vanished breakdowns); days outside it survive."""
        with MetricsStore(settings.metrics_db_path) as store:
            store.save("op3", CollectionResult(observations=[
                Observation("op3", "downloads", 5, "2026-03-01"),
                Observation("op3", "downloads", 3, "2026-03-02"),
                Observation("op3", "downloads", 1, "2026-03-02", dimension="app=Gone"),
            ]))
            store.save("op3", CollectionResult(
                observations=[Observation("op3", "downloads", 4, "2026-03-02")],
                replace_start="2026-03-02", replace_end="2026-03-03",
            ))
            self.assertEqual(store.daily_series("op3", "downloads", "2026-03-01", "2026-03-03"),
                             {"2026-03-01": 5, "2026-03-02": 4})
            self.assertEqual(store.dimension_totals("op3", "downloads", "2026-03-01", "2026-03-03", "app"), [])

    def test_snapshots_update_in_place_and_latest_wins(self) -> None:
        """Two readings on one day collapse to the newer value; the newest day is 'latest'."""
        with MetricsStore(settings.metrics_db_path) as store:
            for value, period in [(10, "2026-03-01"), (12, "2026-03-01"), (20, "2026-03-02")]:
                store.save("x", CollectionResult(observations=[
                    Observation("x", "impressions", value, period, "snapshot", POST, "p1")
                ]))
            self.assertEqual(store.snapshot_series("x", "impressions", POST, "p1"), {"2026-03-01": 12, "2026-03-02": 20})
            self.assertEqual(store.latest_snapshots("x", "impressions", POST), {"p1": (20, "2026-03-02")})

    def test_resume_point_ignores_failed_runs(self) -> None:
        """Only successful runs move the cursor forward."""
        with MetricsStore(settings.metrics_db_path) as store:
            store.finish_run(store.start_run("op3", "2026-03-01", "2026-03-05"), "ok")
            store.finish_run(store.start_run("op3", "2026-03-01", "2026-03-09"), "failed", message="boom")
            self.assertEqual(store.last_successful_window_end("op3"), "2026-03-05")
            self.assertIsNone(store.last_successful_window_end("x"))


class EpisodeIndexTests(unittest.TestCase):
    """Every source's way of naming an episode resolves to the same slug."""

    def test_url_forms_resolve_to_slug(self) -> None:
        """Audio, OP3, and page URLs match regardless of scheme, query, or trailing slash."""
        index = make_index()
        for url in [
            "https://audio.example.com/episodes/ep-a.mp3",
            "https://op3.dev/e/audio.example.com/episodes/ep-a.mp3?_from=app",
            "http://example.com/podcast/ep-a/",
            "https://EXAMPLE.com/podcast/ep-a#chapters",
        ]:
            with self.subTest(url=url):
                self.assertEqual(index.slug_for_url(url), "ep-a")

    def test_foreign_hosts_never_match(self) -> None:
        """A slug-shaped path on someone else's site isn't credited to the episode."""
        index = make_index()
        self.assertIsNone(index.slug_for_url("https://elsewhere.com/podcast/ep-a/"))
        self.assertEqual(index.slug_for_url("https://example.com/blog/ep-b"), "ep-b")
        self.assertIsNone(index.slug_for_url("https://example.com/blog/not-an-episode"))

    def test_paths_and_prefix(self) -> None:
        """Request paths map to slugs, and the shared audio directory is detected."""
        index = make_index()
        self.assertEqual(index.slug_for_path("/episodes/ep-b.mp3?x=1"), "ep-b")
        self.assertIsNone(index.slug_for_path("/robots.txt"))
        self.assertEqual(index.audio_path_prefix(), "/episodes/")


class OP3Tests(unittest.TestCase):
    """Download records become daily totals, listeners, and breakdowns."""

    ROWS = [
        {"time": "2026-03-02T10:00:00Z", "url": "https://op3.dev/e/audio.example.com/episodes/ep-a.mp3",
         "audienceId": "u1", "agentName": "Overcast", "countryCode": "US", "deviceType": "mobile"},
        {"time": "2026-03-02T11:00:00Z", "url": "https://op3.dev/e/audio.example.com/episodes/ep-b.mp3",
         "audienceId": "u1", "agentName": "Apple Podcasts", "countryCode": "SE", "deviceType": "mobile"},
        {"time": "2026-03-03T09:00:00Z", "url": "https://op3.dev/e/audio.example.com/episodes/old-ep.mp3",
         "audienceId": "u2", "countryCode": "US"},
    ]

    def test_aggregate_downloads(self) -> None:
        """Show totals count everything; episodes only matched URLs; listeners are distinct per day."""
        observations, unmatched = aggregate_downloads(self.ROWS, make_index())
        values = {(o.metric, o.period, o.entity_id, o.dimension): o.value for o in observations}
        self.assertEqual(values[("downloads", "2026-03-02", "", "")], 2)
        self.assertEqual(values[("listeners", "2026-03-02", "", "")], 1)
        self.assertEqual(values[("downloads", "2026-03-02", "ep-a", "")], 1)
        self.assertEqual(values[("downloads", "2026-03-03", "", "app=Unknown")], 1)
        self.assertEqual(unmatched, 1)

    def test_collect_falls_back_to_feed_url_and_follows_pages(self) -> None:
        """A 404 on the podcast:guid lookup tries the feed URL; continuation tokens are followed."""
        calls: list[tuple[str, dict[str, Any] | None]] = []

        def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            """Stand-in for OP3: 404 on the guid lookup, a show for the feed URL, two download pages."""
            calls.append((path, dict(params) if params else None))
            if path == f"shows/{settings.podcast_guid}":
                raise CollectorError("HTTP 404 from https://op3.dev/api/1/shows/x: not found")
            if path.startswith("shows/"):
                return {"showUuid": "abc", "title": "Test Show", "statsPageUrl": "https://op3.dev/show/abc"}
            if params and params.get("continuationToken"):
                return {"rows": self.ROWS[2:]}
            return {"rows": self.ROWS[:2], "continuationToken": "next"}

        collector = OP3Collector()
        with mock.patch.object(settings, "op3_api_token", "t"), mock.patch.object(collector, "_get", fake_get):
            result = collector.collect(make_ctx())
        self.assertEqual(sum(1 for path, _ in calls if path.startswith("downloads/show/abc")), 2)
        self.assertEqual((result.replace_start, result.replace_end), ("2026-03-01", "2026-03-11"))
        self.assertEqual(result.entities[0].url, "https://op3.dev/show/abc")
        self.assertIn("1 downloads matched no catalog episode", result.notes[-1])

    def test_config_problems(self) -> None:
        """Without the prefix or a token the collector explains what to set."""
        with mock.patch.object(settings, "op3_api_token", None):
            self.assertIn("OP3_API_TOKEN", OP3Collector().config_problem())
        with mock.patch.object(settings, "use_op3_prefix", False):
            self.assertIn("USE_OP3_PREFIX", OP3Collector().config_problem())


class CloudflareTests(unittest.TestCase):
    """Edge traffic becomes per-episode requests, bytes, and estimated listens."""

    APP = "PodcastAddict/v5 (+https://podcastaddict.com/; Android podcast app)"
    PATHS = [
        {"count": 4, "sum": {"edgeResponseBytes": 2500},
         "dimensions": {"date": "2026-03-05", "clientRequestPath": "/episodes/ep-a.mp3", "userAgent": APP}},
        {"count": 2, "sum": {"edgeResponseBytes": 500},
         "dimensions": {"date": "2026-03-05", "clientRequestPath": "/episodes/ep-b.mp3", "userAgent": APP}},
        {"count": 9, "sum": {"edgeResponseBytes": 99},
         "dimensions": {"date": "2026-03-05", "clientRequestPath": "/episodes/unknown.mp3", "userAgent": APP}},
        {"count": 50, "sum": {"edgeResponseBytes": 50000},
         "dimensions": {"date": "2026-03-05", "clientRequestPath": "/episodes/ep-a.mp3", "userAgent": "Spotify/1.0"}},
    ]
    COUNTRIES = [
        {"count": 6, "dimensions": {"date": "2026-03-05", "clientCountryName": "CA", "userAgent": APP}},
        {"count": 50, "dimensions": {"date": "2026-03-05", "clientCountryName": "SE", "userAgent": "Spotify/1.0"}},
    ]

    def test_aggregate_traffic(self) -> None:
        """Listens = bytes / file size from listener apps only; ingest traffic is counted separately."""
        observations, unmatched = aggregate_traffic(self.PATHS, self.COUNTRIES, make_index())
        values = {(o.metric, o.entity_id, o.dimension): o.value for o in observations}
        self.assertEqual(values[("est_listens", "ep-a", "")], 2.5)
        self.assertEqual(values[("est_listens", "", "")], 3.5)
        self.assertEqual(values[("requests", "", "")], 6)
        self.assertEqual(values[("requests", "", "country=CA")], 6)
        self.assertNotIn(("requests", "", "country=SE"), values)
        self.assertEqual(values[("requests", "", "client=PodcastAddict")], 6)
        self.assertEqual(values[("filtered_requests", "", "client=Spotify (ingest)")], 50)
        self.assertEqual(values[("filtered_requests", "", "")], 50)
        self.assertEqual(unmatched, 1)

    def test_classify_agent(self) -> None:
        """Ingest servers and scripts aren't listeners; apps and browsers are, with readable labels."""
        cases = {
            "Spotify/1.0": ("Spotify (ingest)", False),
            "iTMS": ("Apple (ingest)", False),
            "curl/8.5.0": ("Scripts and tools", False),
            "podcast_server/1.0": ("Scripts and tools", False),
            "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0 Safari/537.36": ("Web browser", True),
            "AppleCoreMedia/1.0.0.21E236 (iPhone; U; CPU OS 17_4 like Mac OS X)": ("AppleCoreMedia", True),
            "": ("Unknown", True),
        }
        for ua, expected in cases.items():
            with self.subTest(ua=ua):
                self.assertEqual(classify_agent(ua), expected)

    def test_collect_clamps_to_retention_and_chunks(self) -> None:
        """A 90-day window on a plan keeping 8 days becomes one clamped query split by the max duration."""
        queries: list[dict[str, Any]] = []

        def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            """Stand-in for Cloudflare: plan limits for the settings query, one traffic row otherwise."""
            if "settings" in query:
                return {"settings": {"httpRequestsAdaptiveGroups": {"enabled": True, "maxDuration": 3 * 86400, "notOlderThan": 8 * 86400}}}
            queries.append(variables)
            return {"paths": self.PATHS[:1], "countries": []}

        # Retention is measured from the current UTC time, so "today" must be the UTC date too.
        today = datetime.now(UTC).date()
        ctx = CollectionContext(CollectionWindow(today - timedelta(days=89), today + timedelta(days=1)), make_index(), today)
        collector = CloudflareCollector()
        with mock.patch.object(collector, "_graphql", fake_graphql):
            result = collector.collect(ctx)
        self.assertEqual(result.replace_start, (today - timedelta(days=7)).isoformat())
        self.assertEqual(len(queries), 3)  # 8 days in chunks of at most 3
        self.assertEqual(queries[0]["pathLike"], "/episodes/%")
        self.assertTrue(any("keeps 8 days" in n for n in result.notes))

    def test_graphql_errors_raise(self) -> None:
        """GraphQL failures arrive with HTTP 200, so the body's `errors` must be checked."""
        reply = {"data": None, "errors": [{"message": "not authorized"}]}
        with mock.patch("metrics.collectors.cloudflare.fetch_json", return_value=reply), \
                self.assertRaisesRegex(CollectorError, "not authorized"):
            CloudflareCollector()._graphql("query", {})


class FakeXTransport:
    """Serves canned X API replies keyed by path prefix and records requested paths."""

    user_context = True

    def __init__(self, posts: dict[str, dict[str, Any]]) -> None:
        """`posts` maps post id → the post object X would return for a lookup."""
        self.posts = posts
        self.paths: list[str] = []

    def get(self, path: str) -> dict[str, Any]:
        """Return the account for /users/me, else the requested posts (unknown ids become errors)."""
        self.paths.append(path)
        if path.startswith("/2/users/me"):
            return {"data": {"id": "42", "username": "show", "public_metrics": {"followers_count": 100, "tweet_count": 7}}}
        ids = parse_qs(urlparse(path).query)["ids"][0].split(",")
        reply: dict[str, Any] = {}
        found = [self.posts[i] for i in ids if i in self.posts]
        if found:
            reply["data"] = found
        missing = [{"resource_id": i, "title": "Not Found Error"} for i in ids if i not in self.posts]
        if missing:
            reply["errors"] = missing
        return reply


class XTests(TempDirTestCase):
    """Only posts in the engine's post log are measured; they become snapshots linked to episodes."""

    POST = {
        "id": "1", "text": "New episode\nout now", "created_at": "2026-03-09T12:00:00.000Z",
        "public_metrics": {"impression_count": 50, "like_count": 3},
        "non_public_metrics": {"url_link_clicks": 4},
        "entities": {"urls": [{"expanded_url": "https://example.com/podcast/ep-a/"}]},
    }

    def setUp(self) -> None:
        """Point the post log into the temp dir."""
        super().setUp()
        self._saved_data_dir = settings.data_dir
        settings.data_dir = self.tmp

    def tearDown(self) -> None:
        """Restore the data dir."""
        settings.data_dir = self._saved_data_dir
        super().tearDown()

    def write_log(self, *records: dict[str, Any]) -> None:
        """Write post-log lines the way podcast_post_to_x does (plus one torn line)."""
        settings.x_post_log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records] + ['{"tweet_id": "torn']
        settings.x_post_log_path.write_text("\n".join(lines) + "\n")

    def test_post_records_link_episode(self) -> None:
        """A link resolves the episode when posting didn't name one; only present metrics are recorded."""
        entities, observations = post_records([self.POST], {}, "show", make_index(), "2026-03-10")
        self.assertEqual((entities[0].episode_slug, entities[0].label), ("ep-a", "New episode out now"))
        self.assertEqual(entities[0].url, "https://x.com/show/status/1")
        self.assertEqual({o.metric: o.value for o in observations}, {"impressions": 50, "likes": 3, "link_clicks": 4})
        self.assertTrue(all(o.granularity == "snapshot" and o.episode_slug == "ep-a" for o in observations))
        named, _ = post_records([self.POST], {"1": "ep-b"}, "show", make_index(), "2026-03-10")
        self.assertEqual(named[0].episode_slug, "ep-b")

    def test_collect_reads_only_logged_posts(self) -> None:
        """Logged ids are looked up directly; old, duplicate, and deleted posts are handled."""
        self.write_log(
            {"tweet_id": "1", "posted_at": "2026-03-09T12:00:00+00:00", "episode_slug": "ep-b"},
            {"tweet_id": "1", "posted_at": "2026-03-09T12:00:00+00:00", "episode_slug": "ep-b"},
            {"tweet_id": "gone", "posted_at": "2026-03-08T12:00:00+00:00"},
            {"tweet_id": "ancient", "posted_at": "2025-01-01T00:00:00+00:00"},
        )
        transport = FakeXTransport({"1": self.POST})
        collector = XCollector()
        with mock.patch.object(collector, "transport", return_value=transport):
            result = collector.collect(make_ctx())
        lookup = parse_qs(urlparse(transport.paths[1]).query)
        self.assertEqual(sorted(lookup["ids"][0].split(",")), ["1", "gone"])
        self.assertIn("non_public_metrics", lookup["tweet.fields"][0])
        self.assertEqual(len(transport.paths), 2)
        self.assertEqual(result.notes[0], "1 of 2 logged posts read, 1 linked to an episode")
        self.assertIn("1 posts X couldn't return", result.notes[1])
        self.assertEqual({e.episode_slug for e in result.entities if e.entity_type == POST}, {"ep-b"})
        followers = [o for o in result.observations if o.entity_type == ACCOUNT and o.metric == "followers"]
        self.assertEqual(followers[0].value, 100)

    def test_no_logged_posts_reads_only_the_account(self) -> None:
        """Without engine-published posts, the timeline is never touched: one read for followers."""
        transport = FakeXTransport({})
        collector = XCollector()
        with mock.patch.object(collector, "transport", return_value=transport):
            result = collector.collect(make_ctx())
        self.assertEqual(len(transport.paths), 1)
        self.assertEqual(result.notes[0], "0 of 0 logged posts read, 0 linked to an episode")

    def test_xurl_failure_is_reported(self) -> None:
        """Non-JSON xurl output (e.g. not logged in) becomes a CollectorError with xurl's message."""
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="no OAuth2 token for app")
        with mock.patch("metrics.collectors.x.xurl_command", return_value=["/usr/bin/xurl"]), \
                mock.patch("metrics.collectors.x.subprocess.run", return_value=failed), \
                self.assertRaisesRegex(CollectorError, "no OAuth2 token"):
            XurlTransport().get("/2/users/me")

    def test_xurl_reads_keep_partial_errors(self) -> None:
        """Unlike posting, a read with per-post errors still returns its data (colored output included)."""
        ok = subprocess.CompletedProcess([], 0, stdout='\x1b[32m{"data": [], "errors": [{"title": "x"}]}\x1b[0m', stderr="")
        with mock.patch("metrics.collectors.x.xurl_command", return_value=["/usr/bin/xurl"]), \
                mock.patch("metrics.collectors.x.subprocess.run", return_value=ok):
            self.assertEqual(XurlTransport().get("/2/users/42/tweets")["errors"], [{"title": "x"}])

    def test_config_requires_the_shared_username(self) -> None:
        """X metrics read the posting account, so X_USERNAME is required whatever the auth method."""
        with mock.patch.object(settings, "x_username", None), mock.patch.object(settings, "x_bearer_token", "t"):
            self.assertIn("X_USERNAME", XCollector().config_problem())
        with mock.patch.object(settings, "x_username", "show"), mock.patch.object(settings, "x_bearer_token", "t"), \
                mock.patch("metrics.collectors.x.xurl_command", side_effect=XPostError("no xurl")):
            self.assertIsNone(XCollector().config_problem())
            self.assertIsInstance(XCollector().transport(), BearerTransport)


class CrosspostCollectorTests(TempDirTestCase):
    """dev.to and Substack metrics cover only logged cross-posts, published ones, credited to episodes by slug."""

    def setUp(self) -> None:
        """Point the cross-post logs into the temp dir."""
        super().setUp()
        self._saved_data_dir = settings.data_dir
        settings.data_dir = self.tmp

    def tearDown(self) -> None:
        """Restore the data dir."""
        settings.data_dir = self._saved_data_dir
        super().tearDown()

    @staticmethod
    def write_log(path: Path, *records: dict[str, Any]) -> None:
        """Write log lines the way the cross-post tools do."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def test_devto_reads_logged_published_articles_only(self) -> None:
        """Unlogged articles and drafts are skipped; an episode-slug article is credited to that episode."""
        from metrics.collectors.devto import DevtoCollector

        self.write_log(settings.devto_post_log_path, {"slug": "ep-a", "article_id": 1}, {"slug": "other", "article_id": 2},
                       {"slug": "draft", "article_id": 3})
        articles = [
            {"id": 1, "published": True, "title": "A", "url": "https://dev.to/x/a", "published_at": "2026-03-09T00:00:00Z",
             "page_views_count": 40, "public_reactions_count": 2, "comments_count": 1},
            {"id": 2, "published": True, "title": "B", "url": "https://dev.to/x/b", "page_views_count": 7},
            {"id": 3, "published": False, "title": "Draft", "page_views_count": 0},
            {"id": 9, "published": True, "title": "Not ours", "page_views_count": 999},
        ]
        with mock.patch("metrics.collectors.devto.my_articles", return_value=articles):
            result = DevtoCollector().collect(make_ctx())
        self.assertEqual({e.entity_id: e.episode_slug for e in result.entities}, {"1": "ep-a", "2": None})
        views = {o.entity_id: o.value for o in result.observations if o.metric == "views"}
        self.assertEqual(views, {"1": 40, "2": 7})
        self.assertTrue(all(o.granularity == "snapshot" for o in result.observations))
        self.assertEqual(result.notes, ["2 of 3 logged articles are published and were read"])

    def test_substack_matches_by_id_or_slug_and_reads_stats(self) -> None:
        """Published logged posts are matched by id (or the engine-set slug); others are ignored."""
        from metrics.collectors.substack import SubstackCollector

        self.write_log(settings.substack_post_log_path, {"slug": "ep-a", "draft_id": 11}, {"slug": "by-slug", "draft_id": 12})
        posts = [
            {"id": 11, "slug": "ep-a", "title": "A", "canonical_url": "https://s.example/p/ep-a", "post_date": "2026-03-09",
             "stats": {"views": 120, "opened": 80, "clicks": 5}, "reaction_count": 3, "comment_count": 0},
            {"id": 99, "slug": "by-slug", "title": "B", "stats": {"views": 10}},
            {"id": 50, "slug": "older-post", "title": "Not ours", "stats": {"views": 10000}},
        ]
        with mock.patch("metrics.collectors.substack.published_posts", return_value=posts):
            result = SubstackCollector().collect(make_ctx())
        self.assertEqual({e.entity_id: e.episode_slug for e in result.entities}, {"11": "ep-a", "99": None})
        first = {o.metric: o.value for o in result.observations if o.entity_id == "11"}
        self.assertEqual(first, {"views": 120, "opens": 80, "clicks": 5, "reactions": 3, "comments": 0})

    def test_substack_without_logged_posts_never_signs_in(self) -> None:
        """No cross-posts means no call to Substack's private API at all."""
        from metrics.collectors.substack import SubstackCollector

        with mock.patch("metrics.collectors.substack.published_posts") as listing:
            result = SubstackCollector().collect(make_ctx())
        listing.assert_not_called()
        self.assertEqual(result.entities, [])

    def test_source_failures_become_collector_errors(self) -> None:
        """A dev.to or Substack failure fails only that source, with the platform's message."""
        from exporters.devto import DevtoError
        from exporters.substack_post import SubstackError
        from metrics.collectors.devto import DevtoCollector
        from metrics.collectors.substack import SubstackCollector

        self.write_log(settings.substack_post_log_path, {"slug": "ep-a", "draft_id": 11})
        with mock.patch("metrics.collectors.devto.my_articles", side_effect=DevtoError("dev.to HTTP 401")), \
                self.assertRaisesRegex(CollectorError, "401"):
            DevtoCollector().collect(make_ctx())
        with mock.patch("metrics.collectors.substack.published_posts", side_effect=SubstackError("sign-in failed")), \
                self.assertRaisesRegex(CollectorError, "sign-in failed"):
            SubstackCollector().collect(make_ctx())


class FetchJsonTests(unittest.TestCase):
    """Errors must be useful without exposing credentials."""

    def test_http_error_message_drops_query_string(self) -> None:
        """A token passed as a query parameter never appears in the error."""
        error = urllib.error.HTTPError("https://api.example.com/x?token=SECRET", 401, "no", {}, None)
        error.read = lambda n=-1: b'{"message":"bad token"}'  # type: ignore[method-assign]
        with mock.patch("metrics.collectors.base.open_https", side_effect=error), \
                self.assertRaises(CollectorError) as caught:
            fetch_json("https://api.example.com/x?token=SECRET")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("HTTP 401", str(caught.exception))


class BrokenCollector(Collector):
    """Test double whose collect() always fails."""

    name = "broken"
    description = "always fails"

    def config_problem(self) -> str | None:
        """Always ready."""
        return None

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Simulate an API outage."""
        raise CollectorError("service down")


class ReadyCollector(Collector):
    """Test double that returns one day observation and one post."""

    name = "ready"
    description = "returns fixed data"
    metrics = {"widgets": MetricSpec("Widgets", headline=True)}

    def config_problem(self) -> str | None:
        """Always ready."""
        return None

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """One show-level day value inside the window."""
        return CollectionResult(observations=[Observation("ready", "widgets", 7, ctx.window.start_iso)])


class PipelineTests(TempDirTestCase):
    """Window planning, failure isolation, and plugin loading."""

    def test_plan_window(self) -> None:
        """First run backfills, later runs resume with overlap, and --days overrides both."""
        with MetricsStore(settings.metrics_db_path) as store:
            first = plan_window(store, "op3", TODAY, None)
            self.assertEqual((first.end - first.start).days, settings.metrics_backfill_days)
            store.finish_run(store.start_run("op3", "2026-03-01", "2026-03-08"), "ok")
            resumed = plan_window(store, "op3", TODAY, None)
            self.assertEqual(resumed.start, date(2026, 3, 8) - timedelta(days=OVERLAP_DAYS))
            self.assertEqual(plan_window(store, "op3", TODAY, 7).start, date(2026, 3, 4))

    def test_failures_are_recorded_not_raised(self) -> None:
        """A failing source returns 'failed' and logs the run; a ready one stores its data."""
        with MetricsStore(settings.metrics_db_path) as store:
            with self.assertLogs("metrics.collect", "WARNING"):
                failed = run_one(store, BrokenCollector(), make_index(), TODAY, 3)
            ok = run_one(store, ReadyCollector(), make_index(), TODAY, 3)
            self.assertEqual((failed["status"], ok["status"]), ("failed", "ok"))
            self.assertIn("service down", store.latest_runs()["broken"]["message"])
            self.assertEqual(store.daily_series("ready", "widgets", "2026-03-01", "2026-03-11"), {"2026-03-08": 7})

    def test_unconfigured_source_is_skipped(self) -> None:
        """Missing settings produce 'skipped' with the reason, and no run row."""
        with mock.patch.object(settings, "cloudflare_api_token", None), MetricsStore(settings.metrics_db_path) as store:
            result = run_one(store, CloudflareCollector(), make_index(), TODAY, None)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("CLOUDFLARE_API_TOKEN", result["message"])
            self.assertEqual(store.latest_runs(), {})

    def test_extra_collectors_load_by_import_path(self) -> None:
        """METRICS_EXTRA_COLLECTORS plugs in classes; bad paths and unknown names fail clearly."""
        with mock.patch.object(settings, "metrics_extra_collectors", ["test_metrics:ReadyCollector"]):
            self.assertEqual([c.name for c in select_collectors(["ready"])], ["ready"])
            with self.assertRaisesRegex(ValueError, "Unknown metrics source"):
                select_collectors(["nope"])
        with self.assertRaises(ValueError):
            load_collector_class("no_colon_here")
        with self.assertRaises(TypeError):
            load_collector_class("metrics.models:Observation")


class YouTubeTests(TempDirTestCase):
    """Only tracked videos are measured; daily analytics appear only when the token has that scope."""

    CHANNEL = {"items": [{"id": "UC1", "snippet": {"title": "Show Channel"},
                          "statistics": {"subscriberCount": "900", "viewCount": "50000", "videoCount": "40"}}]}

    def setUp(self) -> None:
        """Point the data dir (mapping file and upload log) into the temp dir."""
        super().setUp()
        self._saved_data_dir = settings.data_dir
        settings.data_dir = self.tmp

    def tearDown(self) -> None:
        """Restore the data dir."""
        settings.data_dir = self._saved_data_dir
        super().tearDown()

    def track(self) -> None:
        """v1 → ep-a from the mapping file; v2 gains ep-b from the upload log; v3 is an upload with no episode."""
        settings.youtube_videos_file.write_text(json.dumps({"videos": [
            {"video_id": "v1", "episode_slug": "ep-a"}, {"video_id": "v2", "episode_slug": None}]}))
        settings.youtube_upload_log_path.parent.mkdir(parents=True)
        settings.youtube_upload_log_path.write_text(
            json.dumps({"video_id": "v2", "episode_slug": "ep-b"}) + "\n"
            + json.dumps({"video_id": "v3"}) + "\n" + '{"video_id": "torn\n'
        )

    def youtube_mock(self, videos: list[dict[str, Any]]) -> mock.MagicMock:
        """A Data API client returning the test channel and `videos` from one videos.list call."""
        youtube = mock.MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = self.CHANNEL
        youtube.videos.return_value.list.return_value.execute.return_value = {"items": videos}
        return youtube

    @staticmethod
    def video(video_id: str, **stats: str) -> dict[str, Any]:
        """A videos.list item with the given statistics."""
        return {"id": video_id, "snippet": {"title": f"Video {video_id}", "publishedAt": "2026-03-01T00:00:00Z"},
                "statistics": stats}

    def test_tracked_videos_merge_mapping_and_upload_log(self) -> None:
        """The upload log adds videos and fills slugs; torn lines are skipped."""
        from exporters.youtube import read_tracked_videos

        self.track()
        self.assertEqual(read_tracked_videos(), {"v1": "ep-a", "v2": "ep-b", "v3": None})

    def test_without_analytics_scope_records_lifetime_counts(self) -> None:
        """Data API counts become snapshots linked to episodes; hidden counts are skipped; the scope gap is noted."""
        from metrics.collectors.youtube import YouTubeCollector

        self.track()
        youtube = self.youtube_mock([self.video("v1", viewCount="120", commentCount="4")])
        collector = YouTubeCollector()
        with mock.patch.object(collector, "services", return_value=(youtube, None)):
            result = collector.collect(make_ctx())
        values = {(o.entity_id, o.metric): o.value for o in result.observations}
        self.assertEqual(values[("UC1", "subscribers")], 900)
        self.assertEqual(values[("v1", "lifetime_views")], 120)
        self.assertNotIn(("v1", "likes"), values)
        self.assertEqual([e.episode_slug for e in result.entities if e.entity_type == "video"], ["ep-a"])
        self.assertEqual(result.notes[0], "1 of 3 tracked videos found")
        self.assertIn("yt-analytics.readonly", result.notes[1])
        self.assertIsNone(result.replace_start)
        # Only tracked ids are requested from the Data API.
        self.assertEqual(youtube.videos.return_value.list.call_args.kwargs["id"], "v1,v2,v3")

    def test_with_analytics_scope_adds_daily_metrics(self) -> None:
        """One query per tracked video; show totals sum them; the last week is always re-fetched."""
        from metrics.collectors.youtube import YouTubeCollector

        settings.youtube_videos_file.write_text(json.dumps({"videos": [
            {"video_id": "v1", "episode_slug": "ep-a"}, {"video_id": "v2", "episode_slug": "ep-b"}]}))
        analytics = mock.MagicMock()
        analytics.reports.return_value.query.return_value.execute.side_effect = [
            {"rows": [["2026-03-08", 5, 12.5, 1]]}, {"rows": [["2026-03-08", 3, 4, 0]]},
        ]
        collector = YouTubeCollector()
        ctx = make_ctx(start=date(2026, 3, 9))
        with mock.patch.object(collector, "services", return_value=(self.youtube_mock([]), analytics)):
            result = collector.collect(ctx)
        query = analytics.reports.return_value.query.call_args_list[0].kwargs
        self.assertEqual((query["startDate"], query["endDate"], query["filters"]), ("2026-03-03", "2026-03-10", "video==v1"))
        self.assertEqual((result.replace_start, result.replace_end), ("2026-03-03", "2026-03-11"))
        values = {(o.entity_id, o.metric, o.period): o.value for o in result.observations if o.granularity == "day"}
        self.assertEqual(values[("v1", "watch_minutes", "2026-03-08")], 12.5)
        self.assertEqual(values[("", "views", "2026-03-08")], 8)

    def test_analytics_failure_keeps_lifetime_counts(self) -> None:
        """A disabled Analytics API becomes a note; snapshots survive and no day window is replaced."""
        from metrics.collectors.youtube import YouTubeCollector

        settings.youtube_videos_file.write_text(json.dumps({"videos": [{"video_id": "v1", "episode_slug": "ep-a"}]}))
        analytics = mock.MagicMock()
        analytics.reports.return_value.query.return_value.execute.side_effect = CollectorError("HTTP 403 disabled")
        collector = YouTubeCollector()
        youtube = self.youtube_mock([self.video("v1", viewCount="7")])
        with mock.patch.object(collector, "services", return_value=(youtube, analytics)):
            result = collector.collect(make_ctx())
        self.assertIn("daily analytics skipped", result.notes[-1])
        self.assertIsNone(result.replace_start)
        self.assertIn(("v1", "lifetime_views"), {(o.entity_id, o.metric) for o in result.observations})

    def test_upload_logs_video_with_episode(self) -> None:
        """A finished upload is logged with its episode; a bad slug is refused before YouTube is contacted."""
        from exporters.youtube import upload_video_to_youtube

        video_file = self.tmp / "ep.mp4"
        video_file.write_bytes(b"\x00" * 10)
        service = mock.MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (
            None, {"id": "new1", "snippet": {"title": "T"}, "status": {"privacyStatus": "unlisted"}})
        with mock.patch("exporters.youtube.get_authenticated_youtube_service", return_value=service) as get_service, \
                mock.patch("exporters.youtube.MediaFileUpload"):
            with self.assertRaises(ValueError):
                upload_video_to_youtube(str(video_file), "T", "D", episode_slug="../bad")
            get_service.assert_not_called()
            upload_video_to_youtube(str(video_file), "T", "D", episode_slug="ep-a")
        logged = json.loads(settings.youtube_upload_log_path.read_text())
        self.assertEqual((logged["video_id"], logged["episode_slug"]), ("new1", "ep-a"))


class AnalysisAndReportTests(TempDirTestCase):
    """Headline numbers, launch columns, and safe HTML embedding, over a small realistic store."""

    def setUp(self) -> None:
        """Catalog with ep-a (released 03-01) and ep-b (03-05); OP3 data from 03-02; one X post."""
        super().setUp()
        with CatalogDB(settings.database_path) as db:
            for row in [catalog_row("ep-a"), catalog_row("ep-b", "2026-03-05T00:00:00+00:00", 500)]:
                db.insert_episode(row)
        observations = [
            Observation("op3", "downloads", 10, "2026-03-02"),
            Observation("op3", "downloads", 10, "2026-03-02", entity_type=EPISODE, entity_id="ep-a", episode_slug="ep-a"),
            Observation("op3", "downloads", 6, "2026-03-06"),
            Observation("op3", "downloads", 6, "2026-03-06", entity_type=EPISODE, entity_id="ep-b", episode_slug="ep-b"),
            Observation("op3", "downloads", 4, "2026-03-06", dimension="app=Overcast"),
            Observation("x", "impressions", 80, "2026-03-09", "snapshot", POST, "p1", episode_slug="ep-b"),
            Observation("x", "followers", 90, "2026-02-01", "snapshot", ACCOUNT, "acct"),
            Observation("x", "followers", 100, "2026-03-09", "snapshot", ACCOUNT, "acct"),
        ]
        entities = [
            Entity("x", POST, "p1", label="</script><script>alert(1)</script>", url="javascript:alert(1)",
                   created_at="2026-03-08T00:00:00Z", episode_slug="ep-b"),
            Entity("x", ACCOUNT, "acct", label="@show"),
        ]
        with MetricsStore(settings.metrics_db_path) as store:
            store.save("op3", CollectionResult(observations=[o for o in observations if o.source == "op3"]))
            store.save("x", CollectionResult(observations=[o for o in observations if o.source == "x"], entities=entities))

    def test_summary_headlines(self) -> None:
        """Downloads sum over the window, followers report the latest with change, impressions sum recent posts."""
        from metrics.analysis import summarize

        summary = summarize(days=30, today=TODAY)
        by_metric = {(h["source"], h["metric"]): h for h in summary["headlines"]}
        self.assertEqual(by_metric[("op3", "downloads")]["current"], 16)
        self.assertEqual(by_metric[("x", "followers")]["current"], 100)
        self.assertEqual(by_metric[("x", "impressions")]["current"], 80)
        self.assertEqual(summary["breakdowns"][0]["top"], [["Overcast", 4]])

    def test_launch_column_only_when_data_covers_release(self) -> None:
        """ep-a was released before OP3 data starts, so its first-7-days value is unknown, not zero."""
        from metrics.analysis import episode_metrics, episode_table
        from metrics.registry import metric_specs

        with MetricsStore(settings.metrics_db_path) as store:
            table = episode_table(store, metric_specs(), EpisodeIndex.from_catalog())
        rows = {r["slug"]: r["values"] for r in table["rows"]}
        self.assertIsNone(rows["ep-a"]["op3.downloads.launch"])
        self.assertEqual(rows["ep-b"]["op3.downloads.launch"], 6)
        self.assertEqual(rows["ep-b"]["x.impressions"], 80)
        self.assertEqual(episode_metrics("ep-b")["linked_items"][0]["values"], {"impressions": 80})

    def test_report_embeds_hostile_text_safely(self) -> None:
        """Post text can't close the data script; the page still carries the data."""
        from metrics.report import write_report

        html = write_report(days=30, path=self.tmp / "dash.html").read_text()
        data_block = html.split('id="dashboard-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<script>", data_block)
        self.assertEqual(json.loads(data_block)["items"][0]["rows"][0]["label"], "</script><script>alert(1)</script>")

    def test_report_is_self_contained(self) -> None:
        """The font is embedded and nothing is fetched: no external stylesheets, scripts, or font URLs."""
        from metrics.report import FONT_PLACEHOLDER, write_report

        html = write_report(days=30, path=self.tmp / "dash.html").read_text()
        self.assertIn("url(data:font/woff2;base64,", html)
        self.assertNotIn(FONT_PLACEHOLDER, html)
        for external in ("<link", 'src="http', "url(http", "@import"):
            self.assertNotIn(external, html)


if __name__ == "__main__":
    unittest.main()
