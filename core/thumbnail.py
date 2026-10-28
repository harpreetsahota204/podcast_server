"""YouTube thumbnails: stage an art prompt, then composite the headline onto the rendered art.

Two steps, split so the engine never calls an image model:

1. `generate_youtube_thumbnail` stages a prompt. The agent renders it with its own
   image model and saves the art (no lettering) to the returned `local_path`.
2. `render_thumbnail` lays the headline over that art deterministically: a fade
   behind the text, the headline auto-sized to its box, and a stroke for small-screen
   legibility. Image models garble text; code doesn't.

The look (fonts, colors, layout) comes from `$DATA_DIR/templates/thumbnail_style.json`,
so each show keeps its own identity outside the engine. Without that file, plain
defaults apply (Pillow's built-in font, white text, gold accent).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

from core.config import settings
from core.formatting import validate_slug

THUMB_SIZE = (1280, 720)
# YouTube rejects custom thumbnails above 2 MB.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_HEADLINE_LINES = 4
# Search bounds for the headline font size, in pixels.
MIN_FONT_PX, MAX_FONT_PX = 12, 400


class ThumbnailStyle(BaseModel):
    """A show's thumbnail look; fractions are of the 1280×720 canvas."""

    headline_font: Path | None = Field(default=None, description="TTF/OTF for the headline; relative to the style file")
    label_font: Path | None = Field(default=None, description="TTF/OTF for the small label above the headline")
    text_color: str = "#FFFFFF"
    accent_color: str = "#FFCC00"
    stroke_color: str = "#000000"
    stroke_ratio: float = Field(default=0.06, description="Outline width as a fraction of the headline size")
    scrim_color: str = "#000000"
    scrim_opacity: float = Field(default=0.8, ge=0, le=1, description="Fade strength behind the text (0 disables it)")
    # left, top, right, bottom. The default keeps the right 40% for text and stops above
    # the bottom-right corner, where YouTube overlays the duration badge.
    text_box: tuple[float, float, float, float] = (0.57, 0.14, 0.95, 0.78)
    line_spacing: float = Field(default=0.08, description="Gap between headline lines, as a fraction of the font size")
    label_ratio: float = Field(default=0.22, description="Label size as a fraction of the headline size")
    uppercase: bool = True


def load_style(path: Path | None = None) -> ThumbnailStyle:
    """Read the show's style file (default `$DATA_DIR/templates/thumbnail_style.json`); defaults if absent."""
    style_file = path or settings.templates_dir / "thumbnail_style.json"
    if not style_file.exists():
        return ThumbnailStyle()
    style = ThumbnailStyle.model_validate_json(style_file.read_text(encoding="utf-8"))
    # Font paths in the file are relative to it, so a show's data folder stays relocatable.
    for field_name in ("headline_font", "label_font"):
        font = getattr(style, field_name)
        if font is not None and not font.is_absolute():
            setattr(style, field_name, style_file.parent / font)
    return style


def generate_youtube_thumbnail(
    prompt_text: str,
    output_filename: str,
    style: str | None = None,
) -> dict[str, Any]:
    """Return a 16:9 art prompt plus where to save the rendered art (style defaults to THUMBNAIL_STYLE).

    The prompt asks for empty space on the right and no lettering, because
    `render_thumbnail` adds the headline there afterwards.
    """
    validate_slug(output_filename)
    # Create the staging dir up front so the image generator can write straight into it.
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    full_prompt = (
        f"{prompt_text}. YouTube video thumbnail composition, 16:9 wide, clean focal subject on the left "
        f"two-thirds, {style or settings.thumbnail_style}, uncluttered dark negative space across the right 40% "
        "reserved for a headline, no text, letters, or logos anywhere in the image, dramatic lighting, high detail."
    )

    return {
        "status": "staged",
        "prompt": full_prompt,
        "local_path": str(settings.thumbnails_dir / f"{output_filename}.png"),
        "next_step": "Render the art to local_path, then call podcast_render_thumbnail to add the headline.",
    }


def _font(path: Path | None, size: int) -> ImageFont.FreeTypeFont:
    """The style's font at `size` px, or Pillow's built-in scalable font when the style names none."""
    return ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size)


def _apply_scrim(canvas: Image.Image, style: ThumbnailStyle) -> Image.Image:
    """Darken the text side with a horizontal fade, so the headline reads over busy art."""
    if style.scrim_opacity == 0:
        return canvas
    width, height = canvas.size
    # The fade starts a little left of the text box and reaches full strength inside it.
    fade_start = max(0.0, style.text_box[0] - 0.15) * width
    fade_end = min(1.0, style.text_box[0] + 0.05) * width
    peak = int(255 * style.scrim_opacity)
    row = [
        0 if x <= fade_start else peak if x >= fade_end else int(peak * (x - fade_start) / (fade_end - fade_start))
        for x in range(width)
    ]
    # Build one row of the mask and stretch it vertically: much cheaper than per-pixel work.
    mask = Image.new("L", (width, 1))
    mask.putdata(row)
    mask = mask.resize((width, height))
    return Image.composite(Image.new("RGB", canvas.size, style.scrim_color), canvas, mask)


@dataclass(frozen=True, slots=True)
class _Row:
    """One line of text with its font, outline width, and measured box."""

    text: str
    font: ImageFont.FreeTypeFont
    stroke: int
    top: int  # bbox top offset (the font's internal leading), subtracted so rows stack tightly
    height: int


@dataclass(frozen=True, slots=True)
class _Layout:
    """Measured rows (label first, if any) for one candidate headline size."""

    headline_px: int
    gap: int
    rows: list[_Row]
    width: int
    height: int


def _layout(draw: ImageDraw.ImageDraw, lines: list[str], label: str | None, style: ThumbnailStyle, size: int) -> _Layout:
    """Measure the label + headline block with the headline at `size` px."""
    font = _font(style.headline_font, size)
    stroke, gap = round(size * style.stroke_ratio), round(size * style.line_spacing)
    specs = [(line, font, stroke) for line in lines]
    if label:
        # The label sits on the faded side, so it needs no outline.
        specs.insert(0, (label, _font(style.label_font, max(MIN_FONT_PX, round(size * style.label_ratio))), 0))
    rows, widths = [], []
    for text, row_font, row_stroke in specs:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=row_font, stroke_width=row_stroke)
        rows.append(_Row(text, row_font, row_stroke, top, bottom - top))
        widths.append(right - left)
    height = sum(row.height for row in rows) + gap * (len(rows) - 1)
    return _Layout(headline_px=size, gap=gap, rows=rows, width=max(widths), height=height)


def _fit(draw: ImageDraw.ImageDraw, lines: list[str], label: str | None, style: ThumbnailStyle, box: tuple[int, int]) -> _Layout:
    """Largest layout whose block fits the text box (binary search on the headline size)."""
    low, high = MIN_FONT_PX, MAX_FONT_PX
    while low < high:
        size = (low + high + 1) // 2
        candidate = _layout(draw, lines, label, style, size)
        if candidate.width <= box[0] and candidate.height <= box[1]:
            low = size
        else:
            high = size - 1
    return _layout(draw, lines, label, style, low)


def render_thumbnail(
    base_image_path: Path,
    headline_lines: list[str],
    output_path: Path,
    label: str | None = None,
    accent_line: int | None = -1,
    style: ThumbnailStyle | None = None,
) -> dict[str, Any]:
    """Composite the headline (and optional small label) onto base art; return the file path and layout facts.

    `accent_line` picks the line drawn in the accent color (default: the last; None for none).
    The art is cover-cropped to 1280×720. Output is PNG, or JPEG when PNG would exceed
    YouTube's 2 MB limit.
    """
    base_image_path, output_path = Path(base_image_path).resolve(), Path(output_path).resolve()
    if base_image_path == output_path:
        raise ValueError("output_path must differ from base_image_path, or the clean art would be overwritten.")
    lines = [line.strip() for line in headline_lines if line.strip()]
    if not 1 <= len(lines) <= MAX_HEADLINE_LINES:
        raise ValueError(f"Give 1-{MAX_HEADLINE_LINES} non-empty headline lines (2-3 short words read best).")
    style = style or load_style()
    if style.uppercase:
        lines = [line.upper() for line in lines]

    with Image.open(base_image_path) as art:
        canvas = _apply_scrim(ImageOps.fit(art.convert("RGB"), THUMB_SIZE, Image.Resampling.LANCZOS), style)
    draw = ImageDraw.Draw(canvas)
    width, height = THUMB_SIZE
    left, top, right, bottom = (round(f * d) for f, d in zip(style.text_box, (width, height, width, height), strict=True))

    layout = _fit(draw, lines, label, style, (right - left, bottom - top))
    colors = [style.text_color] * len(layout.rows)
    if accent_line is not None:
        # Headline rows follow the label row, when there is one.
        colors[(1 if label else 0) + accent_line % len(lines)] = style.accent_color

    # Center the block vertically in the text box.
    y = top + (bottom - top - layout.height) // 2
    for row, color in zip(layout.rows, colors, strict=True):
        draw.text((left, y - row.top), row.text, font=row.font, fill=color,
                  stroke_width=row.stroke, stroke_fill=style.stroke_color)
        y += row.height + layout.gap

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "PNG", optimize=True)
    if output_path.stat().st_size > MAX_THUMBNAIL_BYTES:
        jpeg_path = output_path.with_suffix(".jpg")
        canvas.save(jpeg_path, "JPEG", quality=90, optimize=True)
        output_path.unlink()
        output_path = jpeg_path
    return {
        "status": "rendered",
        "path": str(output_path),
        "file_size_bytes": output_path.stat().st_size,
        "headline_font_px": layout.headline_px,
        "lines": lines,
        "next_step": "Show the image for approval; then podcast_set_youtube_thumbnail(video_id, path).",
    }
