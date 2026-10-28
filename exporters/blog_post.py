"""Writes companion blog posts into the website's blog directory (astro mode)."""

import json
from pathlib import Path

from core.config import settings
from core.formatting import one_line, validate_slug, yaml_str


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
    validate_slug(slug)
    settings.site_blog_dir.mkdir(parents=True, exist_ok=True)
    output_path = settings.site_blog_dir / f"{slug}.md"

    frontmatter = [
        f"title: {yaml_str(title)}",
        f"description: {yaml_str(one_line(description))}",
        f"tags: {json.dumps(tags or settings.default_tags, ensure_ascii=False)}",
        f"draft: {str(draft).lower()}",
    ]

    output_path.write_text("---\n" + "\n".join(frontmatter) + f"\n---\n\n{content_markdown.strip()}\n", encoding="utf-8")
    return output_path
