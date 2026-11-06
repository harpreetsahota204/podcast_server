"""Show-specific on-page rules a blog post must meet before it goes live, from `$DATA_DIR/templates/site_rules.json`.

The discovery brief (`core.discovery`) covers search phrases. These rules cover the
parts of a page's findability that differ by site: tags from the site's own taxonomy,
links to related pages on the site (internal links), and a link to what the site offers
(e.g. a services page). Like discovery checks they refuse, and `discovery_override` with
the human's reason skips them. The one rule of thumb, a code block, formula, or measured
number somewhere in the body, only warns. Without a rules file nothing is checked.

File shape (every key optional):
    {"blog": {"allowed_tags": ["AI Engineering"], "min_internal_links": 1,
              "internal_link_prefixes": ["/blog/", "/podcast/"], "cta_link_prefixes": ["/services/"],
              "min_faq_items": 3, "warn_without_evidence": true}}

`min_faq_items` counts the post's `faq` frontmatter (question and answer pairs the site renders as a
visible section and as FAQPage structured data), not headings in the body.
"""

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import PurePosixPath
from urllib.parse import urlparse

from core.config import settings
from core.discovery import override_reason

# Markdown link and image targets, `](target "optional title")`, and HTML `href="target"`.
_MARKDOWN_TARGET_RE = re.compile(r"\]\(\s*<?([^)\s>]+)")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
# Targets with these suffixes are images or video, not pages a reader moves on to.
_MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".mp4", ".webm", ".mov"}
# Proof answer engines can quote: a fenced code block, a formula, or a number with a unit or percent.
_EVIDENCE_RES = [
    re.compile(r"^\s*(```|~~~)", re.MULTILINE),
    re.compile(r"\$\$.+?\$\$|\\\(.+?\\\)|\\\[.+?\\\]", re.DOTALL),
    re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?\s?(?:%|×|x\b|ms\b|µs\b|[kMG]?Hz\b|fps\b|[KMGT]B\b|DoF\b|mm\b|cm\b|kg\b|[KMB]\b)"),
]


@dataclass(frozen=True)
class BlogRules:
    """The site's rules for blog posts; an empty list or zero turns a rule off."""

    allowed_tags: list[str] = field(default_factory=list)
    min_internal_links: int = 0
    # Paths that count as related content; empty means any page on the site except the call-to-action pages.
    internal_link_prefixes: list[str] = field(default_factory=list)
    cta_link_prefixes: list[str] = field(default_factory=list)
    min_faq_items: int = 0
    warn_without_evidence: bool = False


def load_blog_rules() -> BlogRules | None:
    """Read the blog rules from the show's rules file; None when there's no file. Raise ValueError on a malformed one."""
    path = settings.site_rules_path
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    blog = data.get("blog") if isinstance(data, dict) else None
    if not isinstance(blog, dict):
        raise ValueError(f"{path} needs a top-level \"blog\" object.")
    # A misspelled key would silently turn its rule off, so unknown keys are refused.
    known = {f.name for f in fields(BlogRules)}
    if unknown := sorted(set(blog) - known):
        raise ValueError(f"{path}: unknown blog rule(s) {unknown}; known: {sorted(known)}.")
    return BlogRules(**blog)


def _same_site(host: str) -> bool:
    """True when `host` is the website's own host, with or without `www.`."""
    site_host = urlparse(settings.site_origin).netloc
    return host.removeprefix("www.") == site_host.removeprefix("www.")


def site_link_paths(markdown: str) -> list[str]:
    """Paths of the body's links that point at the website itself (site-relative, or absolute on its host), media excluded."""
    paths = []
    for target in _MARKDOWN_TARGET_RE.findall(markdown) + _HREF_RE.findall(markdown):
        parsed = urlparse(target)
        # Absolute links count only on the site's own host; relative ones only when rooted at "/".
        if parsed.scheme in ("http", "https") and _same_site(parsed.netloc):
            path = parsed.path or "/"
        elif not parsed.scheme and not parsed.netloc and target.startswith("/"):
            path = parsed.path
        else:
            continue
        if PurePosixPath(path).suffix.lower() not in _MEDIA_SUFFIXES:
            paths.append(path)
    return paths


def has_evidence(markdown: str) -> bool:
    """True when the body has a code block, a formula, or a number with a unit or percent."""
    return any(pattern.search(markdown) for pattern in _EVIDENCE_RES)


def _starts_with_any(path: str, prefixes: list[str]) -> bool:
    """True when `path` begins with one of `prefixes`."""
    return any(path.startswith(prefix) for prefix in prefixes)


def _related_paths(rules: BlogRules, slug: str, paths: list[str]) -> list[str]:
    """The site links that count as related pages: not the post itself, and inside the rules' prefixes (or, with none set, not a call to action)."""
    # The post's own URL, so a link to itself doesn't count.
    own = urlparse(settings.blog_url_template.replace("{slug}", slug)).path if settings.blog_url_template else None
    related = []
    for path in paths:
        if own and path.rstrip("/") == own.rstrip("/"):
            continue
        if rules.internal_link_prefixes:
            if _starts_with_any(path, rules.internal_link_prefixes):
                related.append(path)
        elif not _starts_with_any(path, rules.cta_link_prefixes):
            related.append(path)
    return related


def check_blog_rules(
    rules: BlogRules, slug: str, tags: list[str], body: str, faq_count: int = 0
) -> tuple[list[str], list[str]]:
    """Return (problems that refuse the post, warnings to pass on) for a blog post's tags, body, and FAQ size."""
    problems: list[str] = []

    # 1. Tags: every tag must come from the site's taxonomy, so tag pages stay few and full.
    if rules.allowed_tags:
        allowed = {tag.casefold() for tag in rules.allowed_tags}
        if not tags:
            problems.append(f"the post has no tags; use the site's: {', '.join(rules.allowed_tags)}")
        elif unknown := [tag for tag in tags if tag.casefold() not in allowed]:
            problems.append(f"tags {unknown} aren't in the site's taxonomy ({', '.join(rules.allowed_tags)})")

    # 2. Internal links: related posts or episodes, which pass readers and crawlers to the rest of the site.
    paths = site_link_paths(body)
    related = _related_paths(rules, slug, paths)
    if len(related) < rules.min_internal_links:
        where = f" (paths starting with {', '.join(rules.internal_link_prefixes)})" if rules.internal_link_prefixes else ""
        problems.append(
            f"the body links to {len(related)} related page(s) on the site{where}; it needs at least {rules.min_internal_links}"
        )

    # 3. Call to action: at least one link to the pages the site exists to sell.
    if rules.cta_link_prefixes and not any(_starts_with_any(path, rules.cta_link_prefixes) for path in paths):
        problems.append(f"there's no call-to-action link to {' or '.join(rules.cta_link_prefixes)}")

    # 4. FAQ: question and answer pairs answer engines can quote; the site renders them and marks them up.
    if faq_count < rules.min_faq_items:
        problems.append(
            f"the post has {faq_count} FAQ item(s); it needs at least {rules.min_faq_items} "
            "(pass faq as a list of {question, answer}: real questions a reader would search, answered in 2-3 sentences)"
        )

    # 5. Evidence is a rule of thumb, so it warns instead of refusing.
    warnings = []
    if rules.warn_without_evidence and not has_evidence(body):
        warnings.append(
            "No code block, formula, or measured number (e.g. '84%', '3 ms', '16 DoF') in the body; "
            "answer engines tend to cite posts with concrete proof."
        )
    return problems, warnings


def gate_blog_rules(
    slug: str, tags: list[str], body: str, discovery_override: str | None = None, faq_count: int = 0
) -> list[str]:
    """Refuse (ValueError) a blog post that breaks the site's rules, unless discovery_override gives the human's reason; return warnings."""
    rules = load_blog_rules()
    if rules is None:
        return []
    problems, warnings = check_blog_rules(rules, slug, tags, body, faq_count)
    if problems and override_reason(discovery_override) is None:
        raise ValueError(
            f"Site rules check failed for blog post '{slug}': {'; '.join(problems)}. Nothing was published. "
            f"Fix the post (rules: {settings.site_rules_path}), or pass discovery_override with the human's reason for skipping."
        )
    return warnings
