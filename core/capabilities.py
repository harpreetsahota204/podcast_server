"""What the engine can do right now, channel by channel, so an agent can show the human.

Built on the setup doctor's checks, so a channel's status here always matches
`python -m core.doctor`. Statuses, worst first: `broken` (a check failed),
`needs_attention` (set up but something's off), `off` (not set up), `ready`.
"""

from collections.abc import Callable
from typing import Any

from core import doctor
from core.config import settings
from core.doctor import Check  # (status, detail)

# Doctor status → the word the agent shows the human, ordered worst first.
STATUS_WORDS = {"FAIL": "broken", "WARN": "needs_attention", "OFF": "off", "PASS": "ready"}
SEVERITY = list(STATUS_WORDS)


def check_blog() -> Check:
    """Blog posts need a website repo; SITE_MODE=none has nowhere to write them."""
    if settings.site_mode == "none":
        return "OFF", "SITE_MODE=none has no website; set SITE_MODE=astro and WEBSITE_PATH to write blog posts"
    return doctor.check_site()


# (channel, checks that must pass, tools that act on it). Every tool name must exist on the server (tested).
CHANNELS: list[tuple[str, list[Callable[[], Check]], list[str]]] = [
    (
        "Podcast feed and audio",
        [doctor.check_r2_credentials, doctor.check_public_domain, doctor.check_site],
        [
            "podcast_stage_discovery", "podcast_render_episode_art", "podcast_publish",
            "podcast_update_episode", "podcast_rebuild_feed", "podcast_deploy_site",
        ],
    ),
    ("Website blog", [check_blog], ["podcast_stage_discovery", "podcast_create_blog", "podcast_deploy_site"]),
    (
        "YouTube",
        [doctor.check_youtube],
        ["podcast_publish_to_youtube", "podcast_set_youtube_thumbnail", "podcast_list_youtube_videos", "podcast_delete_youtube_video"],
    ),
    ("X", [doctor.check_x_posting], ["podcast_draft_social", "podcast_post_to_x"]),
    (
        "Instagram",
        [doctor.check_instagram],
        ["podcast_post_to_instagram", "podcast_list_instagram_posts", "podcast_delete_instagram_post"],
    ),
    (
        "dev.to",
        [doctor.check_devto],
        ["podcast_crosspost_to_devto", "podcast_list_devto_articles", "podcast_unpublish_devto_article"],
    ),
    (
        "Substack",
        [doctor.check_substack],
        [
            "podcast_crosspost_to_substack", "podcast_render_newsletter_preview", "podcast_make_gif",
            "podcast_draft_newsletter", "podcast_pull_newsletter", "podcast_send_newsletter",
            "podcast_unschedule_newsletter", "podcast_list_substack_posts", "podcast_delete_substack_post",
        ],
    ),
    (
        "Audience metrics",
        [doctor.check_metrics_sources],
        ["podcast_metrics_collect", "podcast_metrics_summary", "podcast_episode_metrics", "podcast_metrics_report"],
    ),
]

# Requests no tool handles, so the agent says so up front instead of improvising a workaround.
MANUAL = [
    "Editing a YouTube video's title, description, or tags: no tool; the human edits it in YouTube Studio.",
    "Video podcast episodes: the feed carries audio only; the human uploads video to Spotify for Creators or Apple.",
    "Instagram carousels, Stories, and Reels: only single-image posts are supported.",
    "Thumbnails on YouTube Shorts: the API can't set them; the human sets them in YouTube Studio.",
    "Publishing dev.to drafts and Substack cross-posts: the tools only create drafts; the human publishes them.",
    "Showing your own transcripts in Apple Podcasts: a one-time Podcasts Connect setting (display transcripts I provide).",
]


def capabilities() -> dict[str, Any]:
    """Each channel's status, the reason, and its tools, plus the requests that need the human."""
    # Several channels share a check (e.g. the website repo); run each check once per call.
    results: dict[Callable[[], Check], Check] = {}
    channels = []
    for name, checks, tools in CHANNELS:
        for check in checks:
            if check not in results:
                results[check] = doctor.run_check(check)
        outcomes = [results[check] for check in checks]
        # A channel is only as ready as its weakest check; report the reasons at that level.
        worst = min((status for status, _ in outcomes), key=SEVERITY.index)
        channels.append({
            "channel": name,
            "status": STATUS_WORDS[worst],
            "detail": "; ".join(detail for status, detail in outcomes if status == worst),
            "tools": tools,
        })
    return {"channels": channels, "manual": MANUAL}
