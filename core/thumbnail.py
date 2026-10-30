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

`render_episode_art` reuses the same art and style for the episode's square icon in
podcast apps (the feed's per-episode `itunes:image`): a square crop around the subject,
the headline in a band across the bottom, and the show's wordmark.
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
    wordmark: str | None = Field(
        default=None, description="Show name at the bottom of episode art; default PODCAST_TITLE, empty string for none"
    )


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
    size: tuple[int, int] = THUMB_SIZE,
    focus: tuple[float, float] = (0.5, 0.5),
    mirror: bool = False,
) -> dict[str, Any]:
    """Composite the headline (and optional small label) onto base art; return the file path and layout facts.

    `accent_line` picks the line drawn in the accent color (default: the last; None for none).
    The art is cover-cropped to `size` (default YouTube's 1280×720), keeping the point at
    `focus` (fractions of the art's width and height) in view; `mirror` flips it first, for art
    whose subject sits where the headline goes. Output is PNG, or JPEG when PNG would exceed 2 MB.
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
        art = ImageOps.mirror(art.convert("RGB")) if mirror else art.convert("RGB")
        canvas = _apply_scrim(ImageOps.fit(art, size, Image.Resampling.LANCZOS, centering=focus), style)
    draw = ImageDraw.Draw(canvas)
    width, height = size
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


# Substack's full-fit post preview (14:10); its social cards crop that to 1200×630 (1.91:1) around the center.
NEWSLETTER_PREVIEW_SIZE = (1456, 1048)
SOCIAL_CARD_RATIO = 1200 / 630
# Share of the height a centered 1.91:1 crop cuts from the top (and the bottom), plus a little room.
_SOCIAL_CROP_MARGIN = (1 - NEWSLETTER_PREVIEW_SIZE[0] / SOCIAL_CARD_RATIO / NEWSLETTER_PREVIEW_SIZE[1]) / 2 + 0.03


def render_newsletter_preview(
    base_image_path: Path,
    headline_lines: list[str],
    output_path: Path,
    label: str | None = None,
    accent_line: int | None = -1,
    focus: tuple[float, float] = (0.5, 0.5),
    mirror: bool = False,
) -> dict[str, Any]:
    """A newsletter's post preview (thumbnail and social card) in the show's thumbnail style, at 1456×1048.

    The headline stays inside the band a 1200×630 social crop keeps, so the text survives
    on every surface that shows the preview.
    """
    style = load_style()
    left, top, right, bottom = style.text_box
    style = style.model_copy(update={
        "text_box": (left, max(top, _SOCIAL_CROP_MARGIN), right, min(bottom, 1 - _SOCIAL_CROP_MARGIN)),
    })
    result = render_thumbnail(
        base_image_path, headline_lines, output_path, label=label, accent_line=accent_line, style=style,
        size=NEWSLETTER_PREVIEW_SIZE, focus=focus, mirror=mirror,
    )
    return {**result, "next_step": "Show the image for approval; then set cover_image in the newsletter file."}


# Episode art: Apple and Spotify want a square, 1400-3000 px; 2048 is sharp on every screen.
EPISODE_ART_PX = 2048
# Layout, as fractions of the side: the fade covers the lower part and the text sits inside it.
_FADE_START, _FADE_FULL, _FADE_OPACITY = 0.48, 0.74, 0.94
_MARGIN, _TEXT_BLOCK_HEIGHT = 0.065, 0.30
_MAX_HEADLINE = 0.15  # caps the headline so a two-word one doesn't fill the icon
_LABEL_RATIO, _WORDMARK_RATIO = 0.024, 0.02
_LABEL_GAP, _WORDMARK_GAP = 0.02, 0.035  # below the label, and between the headline and the wordmark


def square_crop_box(width: int, height: int, focus_x: float) -> tuple[int, int, int, int]:
    """The largest square in the image, centered horizontally on `focus_x` (0-1) and kept inside it."""
    side = min(width, height)
    left = min(max(round(focus_x * width - side / 2), 0), width - side)
    top = (height - side) // 2
    return left, top, left + side, top + side


def _bottom_fade(canvas: Image.Image, style: ThumbnailStyle) -> Image.Image:
    """Darken the lower part of a square so the text reads over any art."""
    side = canvas.height
    start, full, peak = _FADE_START * side, _FADE_FULL * side, int(255 * _FADE_OPACITY)
    column = [0 if y <= start else peak if y >= full else int(peak * (y - start) / (full - start)) for y in range(side)]
    # One column of the mask, stretched sideways: far cheaper than per-pixel work.
    mask = Image.new("L", (1, side))
    mask.putdata(column)
    return Image.composite(Image.new("RGB", canvas.size, style.scrim_color), canvas, mask.resize(canvas.size))


def _block_size(draw: ImageDraw.ImageDraw, lines: list[str], style: ThumbnailStyle, size: int) -> tuple[int, int]:
    """Width and height of the headline lines stacked at `size` px, outlines and line gaps included."""
    font, stroke = _font(style.headline_font, size), round(size * style.stroke_ratio)
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke) for line in lines]
    gaps = round(size * style.line_spacing) * (len(lines) - 1)
    return max(b[2] - b[0] for b in boxes), sum(b[3] - b[1] for b in boxes) + gaps


def _episode_headline_size(draw: ImageDraw.ImageDraw, lines: list[str], style: ThumbnailStyle, box: tuple[int, int]) -> int:
    """Largest headline size (px) that fits `box`, by binary search, capped so short headlines stay proportionate."""
    low, high = MIN_FONT_PX, round(_MAX_HEADLINE * EPISODE_ART_PX)
    while low < high:
        size = (low + high + 1) // 2
        width, height = _block_size(draw, lines, style, size)
        if width <= box[0] and height <= box[1]:
            low = size
        else:
            high = size - 1
    return low


def _draw_line(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont,
               fill: str, stroke: int, style: ThumbnailStyle) -> int:
    """Draw one line with its top edge at xy (ignoring the font's internal leading); return its height."""
    left, top, _, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    draw.text((xy[0] - left, xy[1] - top), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=style.stroke_color)
    return bottom - top


def render_episode_art(
    base_image_path: Path,
    headline_lines: list[str],
    output_path: Path,
    label: str | None = None,
    focus_x: float = 0.33,
    style: ThumbnailStyle | None = None,
) -> dict[str, Any]:
    """Square episode art from the thumbnail's clean art: crop, bottom fade, label, headline, wordmark.

    `focus_x` (0-1) centers the square crop horizontally; the thumbnail prompt puts the subject
    in the left two-thirds, so the default sits there. The last headline line takes the accent
    color, like the thumbnail. Writes a 2048 px RGB JPEG that podcast_publish accepts as
    `episode_image_path`.
    """
    base_image_path, output_path = Path(base_image_path).resolve(), Path(output_path).resolve()
    if base_image_path == output_path:
        raise ValueError("output_path must differ from base_image_path, or the clean art would be overwritten.")
    if not 0 <= focus_x <= 1:
        raise ValueError("focus_x is a fraction of the art's width, from 0 to 1.")
    lines = [line.strip() for line in headline_lines if line.strip()]
    if not 1 <= len(lines) <= MAX_HEADLINE_LINES:
        raise ValueError(f"Give 1-{MAX_HEADLINE_LINES} non-empty headline lines (2-3 short words read best).")
    style = style or load_style()
    wordmark = settings.podcast_title if style.wordmark is None else style.wordmark
    if style.uppercase:
        lines, wordmark = [line.upper() for line in lines], wordmark.upper()

    # 1. Square crop around the subject, scaled to the icon size, faded at the bottom.
    with Image.open(base_image_path) as art:
        square = art.convert("RGB").crop(square_crop_box(art.width, art.height, focus_x))
    canvas = _bottom_fade(square.resize((EPISODE_ART_PX, EPISODE_ART_PX), Image.Resampling.LANCZOS), style)
    draw = ImageDraw.Draw(canvas)

    # 2. Bottom-up layout: wordmark on the bottom margin, the headline block above it, the label above that.
    side, margin = EPISODE_ART_PX, round(_MARGIN * EPISODE_ART_PX)
    wordmark_font = _font(style.label_font, round(_WORDMARK_RATIO * side))
    wordmark_top = side - margin
    if wordmark:
        wordmark_box = draw.textbbox((0, 0), wordmark, font=wordmark_font)
        wordmark_top -= wordmark_box[3] - wordmark_box[1]
    size = _episode_headline_size(draw, lines, style, (side - 2 * margin, round(_TEXT_BLOCK_HEIGHT * side)))
    headline_font, stroke, gap = _font(style.headline_font, size), round(size * style.stroke_ratio), round(size * style.line_spacing)
    y = wordmark_top - round(_WORDMARK_GAP * side) - _block_size(draw, lines, style, size)[1]

    # 3. Draw the label, the headline (last line in the accent color), and the wordmark.
    if label:
        label_font = _font(style.label_font, round(_LABEL_RATIO * side))
        label_box = draw.textbbox((0, 0), label, font=label_font)
        label_top = y - round(_LABEL_GAP * side) - (label_box[3] - label_box[1])
        _draw_line(draw, (margin, label_top), label, label_font, style.text_color, 0, style)
    for index, line in enumerate(lines):
        color = style.accent_color if index == len(lines) - 1 else style.text_color
        y += _draw_line(draw, (margin, y), line, headline_font, color, stroke, style) + gap
    if wordmark:
        _draw_line(draw, (margin, wordmark_top), wordmark, wordmark_font, style.accent_color, 0, style)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=90, optimize=True)
    return {
        "status": "rendered",
        "path": str(output_path),
        "size_px": EPISODE_ART_PX,
        "file_size_bytes": output_path.stat().st_size,
        "headline_font_px": size,
        "lines": lines,
        "next_step": "Show it for approval (check it small, too); then podcast_publish(..., episode_image_path=path).",
    }
