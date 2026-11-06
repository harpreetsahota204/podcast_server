"""Companion blog posts: checks and writes them into the website's blog directory (astro mode) and prepares them for cross-posting.

Before a post goes live it must match its discovery brief and, unless it's a draft,
the site's own rules from `$DATA_DIR/templates/site_rules.json` (`core.site_rules`).

Cross-posting exporters (dev.to, Substack, Hashnode) share `crosspost_source`, so every
platform gets the same checks, the same canonical URL, links that work off-site, and the
post's FAQ (frontmatter `faq`) as a section. dev.to and Hashnode copies also end with the
show's footer (`with_footer`).
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from core.config import settings
from core.discovery import check_blog, gate
from core.formatting import one_line, validate_slug, yaml_str
from core.net import open_https
from core.site_rules import gate_blog_rules

# A Markdown file that opens with a `---` frontmatter block, followed by the body.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
# The whole `pubDate:` line of a post's frontmatter, carried over verbatim when the post is rewritten.
_PUB_DATE_LINE_RE = re.compile(r"^pubDate:[^\r\n]*", re.MULTILINE)
# Inline links and images whose target is a site-relative path, e.g. `](/blog/other-post/)`.
_RELATIVE_LINK_RE = re.compile(r"\]\((/[^)\s]*)")
# Enough questions to cover a post's main searches; more turns the section into filler.
MAX_FAQ_ITEMS = 10
FAQ_HEADING = "Frequently asked questions"


def blog_post_path(slug: str) -> Path:
    """Where the blog post for `slug` lives in the website repo (raises ValueError for an unsafe slug)."""
    return settings.site_blog_dir / f"{validate_slug(slug)}.md"


def check_blog_discovery(slug: str, title: str, description: str, discovery_override: str | None = None) -> str | None:
    """Refuse a blog title or meta description that misses the slug's discovery brief; return any override reason."""
    return gate("blog", slug, discovery_override, lambda brief: check_blog(brief, title, description))


def check_blog_post(
    slug: str,
    title: str,
    description: str,
    tags: list[str],
    body: str,
    draft: bool,
    discovery_override: str | None = None,
    faq_count: int = 0,
) -> list[str]:
    """Every check a blog post must pass before it goes live; raise ValueError on a miss and return warnings.

    The discovery brief always applies; the site's rules (`core.site_rules`) apply only
    to posts the site publishes, so a draft can be saved while it's still being written.
    `faq_count` is how many question and answer pairs the post carries, for the FAQ rule.
    """
    check_blog_discovery(slug, title, description, discovery_override)
    if draft:
        return []
    return gate_blog_rules(slug, tags, body, discovery_override, faq_count)


def check_existing_blog_post(slug: str, discovery_override: str | None = None) -> dict[str, Any]:
    """Run `check_blog_post` on the post already in the website's blog directory (e.g. written by hand) before it's deployed."""
    frontmatter, body = read_blog_post(slug)
    draft = bool(frontmatter.get("draft"))
    warnings = check_blog_post(
        slug,
        title=str(frontmatter["title"]),
        description=str(frontmatter.get("description", "")),
        tags=[str(tag) for tag in frontmatter.get("tags") or []],
        body=body,
        draft=draft,
        discovery_override=discovery_override,
        faq_count=len(validate_faq(frontmatter.get("faq"))),
    )
    return {"slug": slug, "draft": draft, "warnings": warnings}


def create_blog_post(
    title: str,
    description: str,
    content_markdown: str,
    slug: str,
    tags: list[str] | None = None,
    draft: bool = False,
    discovery_override: str | None = None,
    faq: list[dict[str, str]] | None = None,
) -> Path:
    """Write `<WEBSITE_BLOG_DIR>/<slug>.md` with title/description/pubDate/tags/draft (and optional faq) frontmatter.

    Refuses (ValueError) when the title or description misses the discovery brief, or a
    non-draft post breaks the site's rules, unless `discovery_override` gives the human's
    reason. A new post gets now as its `pubDate` (the site requires one). Overwriting an
    existing post keeps its `pubDate` line verbatim and records now as `updatedDate`.
    Returns the written path.
    """
    post_tags = tags or settings.default_tags
    faq_items = validate_faq(faq)
    check_blog_post(slug, title, description, post_tags, content_markdown, draft, discovery_override, len(faq_items))
    output_path = blog_post_path(slug)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # A rewrite keeps the original publish date, so the post doesn't look new to readers or search engines.
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    existing = _FRONTMATTER_RE.match(output_path.read_text(encoding="utf-8")) if output_path.exists() else None
    prior = _PUB_DATE_LINE_RE.search(existing.group(1)) if existing else None
    dates = [prior.group(0), f"updatedDate: {now}"] if prior else [f"pubDate: {now}"]

    frontmatter = [
        f"title: {yaml_str(title)}",
        f"description: {yaml_str(one_line(description))}",
        *dates,
        f"tags: {json.dumps(post_tags, ensure_ascii=False)}",
        f"draft: {str(draft).lower()}",
    ]
    # JSON is valid YAML, and its quoting survives any punctuation in a question or answer.
    if faq_items:
        frontmatter.append(f"faq: {json.dumps(faq_items, ensure_ascii=False)}")

    output_path.write_text("---\n" + "\n".join(frontmatter) + f"\n---\n\n{content_markdown.strip()}\n", encoding="utf-8")
    return output_path


def read_blog_post(slug: str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter and Markdown body of the website's blog post for `slug`."""
    path = blog_post_path(slug)
    if not path.is_file():
        raise FileNotFoundError(f"No blog post at {path}; write it with podcast_publish or podcast_create_blog first.")
    return read_titled_markdown(path)


def read_titled_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """Return the frontmatter and stripped body of a Markdown file whose frontmatter must include a title."""
    match = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    frontmatter = yaml.safe_load(match.group(1)) if match else None
    # Cross-posts and newsletters take their title from the frontmatter, so a file without one can't go out.
    if not isinstance(frontmatter, dict) or not frontmatter.get("title"):
        raise ValueError(f"{path} needs a frontmatter block with a title.")
    return frontmatter, match.group(2).strip()


def absolutize_links(markdown: str, base_url: str) -> str:
    """Point site-relative links and images (`](/path)`) at the website; on another platform they would break."""
    return _RELATIVE_LINK_RE.sub(lambda m: f"]({urljoin(base_url, m.group(1))}", markdown)


def validate_faq(faq: Any) -> list[dict[str, str]]:
    """Return the FAQ as a list of {question, answer} with both stripped and non-empty; raise ValueError otherwise."""
    if faq is None:
        return []
    if not isinstance(faq, list) or len(faq) > MAX_FAQ_ITEMS:
        raise ValueError(f"faq must be a list of at most {MAX_FAQ_ITEMS} {{question, answer}} items.")
    items = []
    for item in faq:
        question = str(item.get("question", "")).strip() if isinstance(item, dict) else ""
        answer = str(item.get("answer", "")).strip() if isinstance(item, dict) else ""
        if not question or not answer:
            raise ValueError("Every faq item needs a non-empty question and answer.")
        items.append({"question": question, "answer": answer})
    return items


def faq_markdown(faq: list[dict[str, str]]) -> str:
    """The FAQ as a Markdown section with one heading per question, the shape answer engines lift answers from."""
    if not faq:
        return ""
    entries = "\n\n".join(f"### {item['question']}\n\n{item['answer']}" for item in faq)
    return f"## {FAQ_HEADING}\n\n{entries}"


@dataclass(frozen=True)
class CrosspostSource:
    """A published blog post prepared for another platform: its metadata, plus a body whose links work off-site."""

    slug: str
    title: str
    description: str
    tags: list[str]
    # The post's body with its FAQ section (if any) appended; the cross-post footer is added per platform.
    body_markdown: str
    post_url: str
    canonical_url: str
    # The frontmatter `pubDate` (or `date`), when present: the original publication date for platforms that can backdate.
    published_on: date | None = None
    # The post's preview image (frontmatter `image`, else BLOG_IMAGE_URL_TEMPLATE): the cover of copies, if reachable.
    image_url: str | None = None


def crosspost_source(slug: str) -> CrosspostSource:
    """Load the website's blog post `slug` for cross-posting; raise if it isn't published or the site URL is unknown."""
    frontmatter, body = read_blog_post(slug)
    # A copy elsewhere would point readers and search engines at a page that doesn't exist yet.
    if frontmatter.get("draft"):
        raise ValueError(f"Blog post '{slug}' is a draft on the website; publish it there first so its URL resolves.")
    if not settings.blog_url_template:
        raise ValueError("Set BLOG_URL_TEMPLATE (e.g. https://example.com/blog/{slug}/) so cross-posts credit the site.")
    post_url = settings.blog_url_template.replace("{slug}", slug)
    # The FAQ lives in frontmatter so the site can render it as structured data; copies get it as a section.
    sections = [body, faq_markdown(validate_faq(frontmatter.get("faq")))]
    published = frontmatter.get("pubDate") or frontmatter.get("date")
    # Only a public https image can be fetched by another platform; anything else is left out.
    image = str(frontmatter.get("image") or "")
    if not image.startswith("https://") and settings.blog_image_url_template:
        image = settings.blog_image_url_template.replace("{slug}", slug)
    return CrosspostSource(
        slug=slug,
        title=str(frontmatter["title"]),
        description=str(frontmatter.get("description", "")),
        tags=[str(tag) for tag in frontmatter.get("tags") or []],
        body_markdown=absolutize_links("\n\n".join(s for s in sections if s), post_url),
        post_url=post_url,
        # A post first published elsewhere declares that original in its frontmatter; it stays the canonical URL.
        canonical_url=str(frontmatter.get("canonical") or post_url),
        published_on=published if isinstance(published, date) else None,
        image_url=image if image.startswith("https://") else None,
    )


def reachable_image(url: str | None) -> str | None:
    """`url` if it answers HTTP 200 with an image now, else None, so a copy never points at a broken cover."""
    if not url:
        return None
    try:
        with open_https(url, method="HEAD") as response:
            ok = response.status == 200 and response.headers.get("Content-Type", "").startswith("image/")
    except Exception:  # unreachable, refused, or not found: the copy simply goes out without a cover
        return None
    return url if ok else None


def with_footer(markdown: str, original_url: str) -> str:
    """Append the show's cross-post footer (`$DATA_DIR/templates/crosspost_footer.md`, if any) to a copy's body.

    `{original_url}` in the template becomes the post's canonical URL, so readers of the copy can find the original.
    """
    path = settings.crosspost_footer_path
    if not path.is_file():
        return markdown
    footer = path.read_text(encoding="utf-8").strip().replace("{original_url}", original_url)
    return f"{markdown}\n\n{footer}" if footer else markdown
