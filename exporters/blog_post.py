"""Companion blog posts: writes them into the website's blog directory (astro mode) and prepares them for cross-posting.

Cross-posting exporters (dev.to, Substack) share `crosspost_source`, so every
platform gets the same checks, the same canonical URL, and links that work off-site.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from core.config import settings
from core.formatting import one_line, validate_slug, yaml_str

# A Markdown file that opens with a `---` frontmatter block, followed by the body.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
# Inline links and images whose target is a site-relative path, e.g. `](/blog/other-post/)`.
_RELATIVE_LINK_RE = re.compile(r"\]\((/[^)\s]*)")


def blog_post_path(slug: str) -> Path:
    """Where the blog post for `slug` lives in the website repo (raises ValueError for an unsafe slug)."""
    return settings.site_blog_dir / f"{validate_slug(slug)}.md"


def create_blog_post(
    title: str,
    description: str,
    content_markdown: str,
    slug: str,
    tags: list[str] | None = None,
    draft: bool = False,
) -> Path:
    """Write `<WEBSITE_BLOG_DIR>/<slug>.md` with title/description/tags/draft frontmatter.

    Overwrites an existing file with the same slug. Returns the written path.
    """
    output_path = blog_post_path(slug)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = [
        f"title: {yaml_str(title)}",
        f"description: {yaml_str(one_line(description))}",
        f"tags: {json.dumps(tags or settings.default_tags, ensure_ascii=False)}",
        f"draft: {str(draft).lower()}",
    ]

    output_path.write_text("---\n" + "\n".join(frontmatter) + f"\n---\n\n{content_markdown.strip()}\n", encoding="utf-8")
    return output_path


def read_blog_post(slug: str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter and Markdown body of the website's blog post for `slug`."""
    path = blog_post_path(slug)
    if not path.is_file():
        raise FileNotFoundError(f"No blog post at {path}; write it with podcast_publish or podcast_create_blog first.")
    match = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    frontmatter = yaml.safe_load(match.group(1)) if match else None
    # A cross-post's title comes from the frontmatter, so a post without one can't go out.
    if not isinstance(frontmatter, dict) or not frontmatter.get("title"):
        raise ValueError(f"{path} needs a frontmatter block with a title.")
    return frontmatter, match.group(2).strip()


def absolutize_links(markdown: str, base_url: str) -> str:
    """Point site-relative links and images (`](/path)`) at the website; on another platform they would break."""
    return _RELATIVE_LINK_RE.sub(lambda m: f"]({urljoin(base_url, m.group(1))}", markdown)


@dataclass(frozen=True)
class CrosspostSource:
    """A published blog post prepared for another platform: its metadata, plus a body whose links work off-site."""

    slug: str
    title: str
    description: str
    tags: list[str]
    body_markdown: str
    post_url: str
    canonical_url: str


def crosspost_source(slug: str) -> CrosspostSource:
    """Load the website's blog post `slug` for cross-posting; raise if it isn't published or the site URL is unknown."""
    frontmatter, body = read_blog_post(slug)
    # A copy elsewhere would point readers and search engines at a page that doesn't exist yet.
    if frontmatter.get("draft"):
        raise ValueError(f"Blog post '{slug}' is a draft on the website; publish it there first so its URL resolves.")
    if not settings.blog_url_template:
        raise ValueError("Set BLOG_URL_TEMPLATE (e.g. https://example.com/blog/{slug}/) so cross-posts credit the site.")
    post_url = settings.blog_url_template.replace("{slug}", slug)
    return CrosspostSource(
        slug=slug,
        title=str(frontmatter["title"]),
        description=str(frontmatter.get("description", "")),
        tags=[str(tag) for tag in frontmatter.get("tags") or []],
        body_markdown=absolutize_links(body, post_url),
        post_url=post_url,
        # A post first published elsewhere declares that original in its frontmatter; it stays the canonical URL.
        canonical_url=str(frontmatter.get("canonical") or post_url),
    )
