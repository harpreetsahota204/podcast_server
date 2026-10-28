"""Template-based social post drafts (X / LinkedIn) for a published episode.

Built-in templates are deliberately plain. A show overrides them by placing
`social_long.txt` and/or `social_short.txt` in `DATA_DIR/templates/`, using the
placeholders {hook}, {specs}, {guest_name}, {link}, and {title} (literal braces
must be doubled: `{{` and `}}`).
"""

from typing import Any

from core.config import settings

# Cap spec bullets so the long post stays scannable.
MAX_SPEC_LINES = 4

DEFAULT_TEMPLATES = {
    "social_long": "{hook}\n\nkey points:\n{specs}\n\nnew episode with {guest_name}:\n{link}",
    "social_short": "{hook}\n\nnew episode with {guest_name}:\n{link}",
}


def _template(name: str) -> str:
    """Return the show's override template if present, else the built-in one."""
    override = settings.templates_dir / f"{name}.txt"
    return override.read_text(encoding="utf-8") if override.exists() else DEFAULT_TEMPLATES[name]


def draft_social_package(
    title: str,
    guest_name: str,
    core_tension: str,
    technical_specs: list[str],
    episode_url: str,
    youtube_url: str | None = None,
) -> dict[str, Any]:
    """Return a long and a short post; the YouTube link wins over the episode page when both exist."""
    fields = {
        "hook": core_tension.strip(),
        "specs": "\n".join(f"- {spec}" for spec in technical_specs[:MAX_SPEC_LINES]),
        "guest_name": guest_name,
        "link": youtube_url or episode_url,
        "title": title,
    }
    return {
        "title": title,
        "guest_name": guest_name,
        "post_long": _template("social_long").format(**fields).strip(),
        "post_short": _template("social_short").format(**fields).strip(),
    }
