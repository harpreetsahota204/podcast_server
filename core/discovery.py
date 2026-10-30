"""Discovery briefs and the per-channel checks that make published content findable.

A brief is the one approved set of search phrases for a piece of content
(`$DATA_DIR/discovery/<slug>.json`). Every publish path checks its own fields
against it through `gate`, which refuses before anything goes public unless the
caller passes `discovery_override` with the human's reason for skipping.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import settings
from core.formatting import parse_chapters, validate_slug

# Brief shape: enough phrases to vary wording across channels, few enough to stay focused.
MIN_SECONDARY_PHRASES, MAX_SECONDARY_PHRASES = 3, 5
MAX_ENTITIES = 10
MIN_PHRASE_CHARS, MAX_PHRASE_CHARS = 3, 80
MAX_TEXT_CHARS = 300

# Where each platform cuts text off in search results and feeds.
YOUTUBE_HOOK_CHARS = 150
INSTAGRAM_HOOK_CHARS = 125
BLOG_DESCRIPTION_CHARS = (50, 160)
MAX_ALT_TEXT_CHARS = 1000
# YouTube only shows chapters when there are at least 3, starting at 0:00, each at least 10 seconds long.
MIN_YOUTUBE_CHAPTERS = 3
MIN_CHAPTER_SECONDS = 10

# Apple asks that episode titles carry no episode numbers ("Ep. 12", "Episode 12", "#12").
_EPISODE_NUMBER_RE = re.compile(r"\bep(?:isode)?\.?\s*#?\d+\b|(?:^|\s)#\d+\b", re.IGNORECASE)

# Returned with every staged brief so the agent knows what each channel will be checked for.
CHANNEL_RULES = {
    "feed": "Episode title names a brief phrase or entity, with no episode number or show title; the summary uses a brief phrase.",
    "blog": f"Description is {BLOG_DESCRIPTION_CHARS[0]}-{BLOG_DESCRIPTION_CHARS[1]} characters and contains the primary phrase; the title names a brief phrase or entity.",
    "youtube": f"Title names a brief phrase or entity; the primary phrase is in the first {YOUTUBE_HOOK_CHARS} characters of the description; tags include a brief phrase; regular videos have at least {MIN_YOUTUBE_CHAPTERS} chapters starting at 00:00, each at least {MIN_CHAPTER_SECONDS}s.",
    "instagram": f"Alt text of 1-{MAX_ALT_TEXT_CHARS} characters; a brief phrase in the first {INSTAGRAM_HOOK_CHARS} characters of the caption.",
    "x": "Alt text on any attached image or video.",
    "newsletter": f"SEO description (default: the subtitle) is {BLOG_DESCRIPTION_CHARS[0]}-{BLOG_DESCRIPTION_CHARS[1]} characters and contains the primary phrase; the SEO title (default: the title) names a brief phrase or entity.",
}


@dataclass(frozen=True)
class DiscoveryBrief:
    """The approved search phrases and framing for one piece of content."""

    slug: str
    primary_phrase: str
    secondary_phrases: list[str]
    audience: str
    question: str
    entities: list[str]

    @property
    def phrases(self) -> list[str]:
        """The primary phrase followed by the secondary ones."""
        return [self.primary_phrase, *self.secondary_phrases]

    @property
    def phrases_and_entities(self) -> list[str]:
        """Everything a title may name to count as on-topic: the phrases plus the entities."""
        return [*self.phrases, *self.entities]


def _clean_phrase(phrase: str, label: str) -> str:
    """Strip a phrase and check its length; raise ValueError naming the field otherwise."""
    text = phrase.strip()
    if not MIN_PHRASE_CHARS <= len(text) <= MAX_PHRASE_CHARS:
        raise ValueError(f"{label} must be {MIN_PHRASE_CHARS}-{MAX_PHRASE_CHARS} characters: {phrase!r}.")
    return text


def _clean_text(text: str, label: str) -> str:
    """Strip a free-text field and check it's present and short enough."""
    value = text.strip()
    if not 1 <= len(value) <= MAX_TEXT_CHARS:
        raise ValueError(f"{label} must be 1-{MAX_TEXT_CHARS} characters.")
    return value


def make_brief(
    slug: str,
    primary_phrase: str,
    secondary_phrases: list[str],
    audience: str,
    question: str,
    entities: list[str],
) -> DiscoveryBrief:
    """Validate and normalize a brief; raise ValueError describing the first problem."""
    primary = _clean_phrase(primary_phrase, "primary_phrase")
    secondary = [_clean_phrase(p, "Each secondary phrase") for p in secondary_phrases]
    if not MIN_SECONDARY_PHRASES <= len(secondary) <= MAX_SECONDARY_PHRASES:
        raise ValueError(f"Give {MIN_SECONDARY_PHRASES}-{MAX_SECONDARY_PHRASES} secondary phrases; got {len(secondary)}.")
    # Repeats waste a slot and make the checks easier to pass than they should be.
    lowered = [p.lower() for p in (primary, *secondary)]
    if len(set(lowered)) != len(lowered):
        raise ValueError("Phrases must all differ from each other (and from the primary phrase).")
    names = [_clean_phrase(e, "Each entity") for e in entities]
    if not 1 <= len(names) <= MAX_ENTITIES:
        raise ValueError(f"Give 1-{MAX_ENTITIES} entities (guest, tools, papers people search for).")
    return DiscoveryBrief(
        slug=validate_slug(slug),
        primary_phrase=primary,
        secondary_phrases=secondary,
        audience=_clean_text(audience, "audience"),
        question=_clean_text(question, "question"),
        entities=names,
    )


def brief_path(slug: str) -> Path:
    """Where the brief for `slug` is stored."""
    return settings.discovery_dir / f"{validate_slug(slug)}.json"


def save_brief(brief: DiscoveryBrief) -> Path:
    """Write the brief (replacing an earlier version for the same slug) and return its path."""
    path = brief_path(brief.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**asdict(brief), "saved_at": datetime.now(UTC).isoformat()}
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_brief(slug: str) -> DiscoveryBrief:
    """Read the brief for `slug`; raise ValueError saying how to stage one if there isn't one."""
    path = brief_path(slug)
    if not path.is_file():
        raise ValueError(f"No discovery brief for '{slug}'; stage one with podcast_stage_discovery first.")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("saved_at", None)
    return DiscoveryBrief(**record)


def _mentions(text: str, terms: list[str]) -> bool:
    """True if any term appears in `text`, ignoring case."""
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def check_feed(brief: DiscoveryBrief, title: str, summary: str) -> list[str]:
    """Problems with a podcast episode's title and summary."""
    problems = []
    if not _mentions(title, brief.phrases_and_entities):
        problems.append("the episode title names no brief phrase or entity")
    if _EPISODE_NUMBER_RE.search(title):
        problems.append("the episode title contains an episode number (Apple asks for it in the episode tag instead)")
    if settings.podcast_title.lower() in title.lower():
        problems.append("the episode title repeats the show title")
    if not _mentions(summary, brief.phrases):
        problems.append("the summary uses no brief phrase")
    return problems


def check_blog(brief: DiscoveryBrief, title: str, description: str) -> list[str]:
    """Problems with a blog post's title and meta description."""
    problems = []
    low, high = BLOG_DESCRIPTION_CHARS
    if not low <= len(description.strip()) <= high:
        problems.append(f"the description is {len(description.strip())} characters; search results show {low}-{high}")
    if not _mentions(description, [brief.primary_phrase]):
        problems.append(f"the description doesn't contain the primary phrase {brief.primary_phrase!r}")
    if not _mentions(title, brief.phrases_and_entities):
        problems.append("the title names no brief phrase or entity")
    return problems


def check_youtube(brief: DiscoveryBrief, title: str, description: str, tags: list[str], is_short: bool) -> list[str]:
    """Problems with a YouTube upload's title, description hook, tags, and chapters."""
    problems = []
    if not _mentions(title, brief.phrases_and_entities):
        problems.append("the title names no brief phrase or entity")
    if not _mentions(description[:YOUTUBE_HOOK_CHARS], [brief.primary_phrase]):
        problems.append(f"the primary phrase {brief.primary_phrase!r} isn't in the first {YOUTUBE_HOOK_CHARS} characters of the description")
    if not any(_mentions(tag, brief.phrases) for tag in tags):
        problems.append("no tag contains a brief phrase")
    # Shorts can't show chapters, so only regular videos need them.
    if not is_short:
        problems.extend(_chapter_problems(parse_chapters(description)))
    return problems


def _chapter_problems(chapters: list[dict[str, Any]]) -> list[str]:
    """Why YouTube wouldn't show these description chapters, if it wouldn't."""
    if len(chapters) < MIN_YOUTUBE_CHAPTERS:
        return [f"the description has {len(chapters)} chapter timestamps; YouTube needs at least {MIN_YOUTUBE_CHAPTERS}, starting at 00:00"]
    problems = []
    if chapters[0]["seconds"] != 0:
        problems.append("the first chapter must start at 00:00")
    starts = [c["seconds"] for c in chapters]
    if any(later - earlier < MIN_CHAPTER_SECONDS for earlier, later in zip(starts, starts[1:], strict=False)):
        problems.append(f"chapters must be in order and at least {MIN_CHAPTER_SECONDS}s apart")
    return problems


def clean_alt_text(alt_text: str | None) -> str | None:
    """Stripped alt text, or None when blank; over 1,000 characters is refused (Instagram's and X's limit)."""
    text = (alt_text or "").strip()
    if len(text) > MAX_ALT_TEXT_CHARS:
        raise ValueError(f"Alt text allows {MAX_ALT_TEXT_CHARS} characters; this has {len(text)}.")
    return text or None


def check_instagram(brief: DiscoveryBrief, caption: str, alt_text: str | None) -> list[str]:
    """Problems with an Instagram image post's alt text and caption hook."""
    problems = []
    if not alt_text:
        problems.append("there's no alt text (Instagram search and screen readers use it)")
    if not _mentions(caption[:INSTAGRAM_HOOK_CHARS], brief.phrases):
        problems.append(f"no brief phrase in the first {INSTAGRAM_HOOK_CHARS} characters of the caption (the rest is hidden behind 'more')")
    return problems


def override_reason(discovery_override: str | None) -> str | None:
    """The stripped override reason, or None; a blank override is refused because it records nothing."""
    if discovery_override is None:
        return None
    if not discovery_override.strip():
        raise ValueError("discovery_override needs the reason the human gave for skipping the discovery checks.")
    return discovery_override.strip()


def gate(
    channel: str,
    slug: str | None,
    discovery_override: str | None,
    check: Callable[[DiscoveryBrief], list[str]],
) -> str | None:
    """Run `check` against the brief for `slug`, raising ValueError on any problem, before anything goes public.

    Returns the override reason when the caller skipped the checks (to record with the action), else None.
    """
    if (reason := override_reason(discovery_override)) is not None:
        return reason
    if not slug:
        raise ValueError(
            f"{channel}: pass the episode slug so its discovery brief can be checked, "
            "or discovery_override with the human's reason for skipping."
        )
    if problems := check(load_brief(slug)):
        raise ValueError(
            f"Discovery check failed for {channel} ('{slug}'): {'; '.join(problems)}. Nothing was published. "
            "Fix these fields, or pass discovery_override with the human's reason for skipping."
        )
    return None
