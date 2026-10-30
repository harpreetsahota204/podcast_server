"""Converts a Substack draft body (its editor's JSON document) back into Markdown.

python-substack's own export keeps any node carrying editor-only attributes (Substack's
editor adds `textAlign` to every paragraph and heading on save) as an opaque comment, so
an edited draft comes back unreadable. This converter covers the nodes Markdown can
express and refuses the rest by name, so nothing is silently dropped when a draft
edited in Substack's editor is copied back into its source file.
"""

from typing import Any

# Inline marks → Markdown wrappers, applied innermost first; links are handled separately.
MARK_WRAPPERS = {"code": "`", "em": "*", "strong": "**", "strikethrough": "~~"}
MARK_ORDER = ("code", "em", "strong", "strikethrough")


class UnsupportedNodeError(ValueError):
    """The draft holds content Markdown can't represent (e.g. a button, embed, or footnote)."""


def _inline(nodes: list[dict[str, Any]]) -> str:
    """Text nodes with their marks (bold, italic, code, strikethrough, links) as Markdown."""
    out = []
    for node in nodes:
        if node.get("type") == "hard_break":
            out.append("  \n")
            continue
        if node.get("type") != "text":
            raise UnsupportedNodeError(f"inline node {node.get('type')!r}")
        text = node.get("text", "")
        marks = {mark.get("type"): mark for mark in node.get("marks") or []}
        if unknown := set(marks) - set(MARK_WRAPPERS) - {"link"}:
            raise UnsupportedNodeError(f"text mark(s) {sorted(unknown)}")
        for mark in MARK_ORDER:
            if mark in marks:
                text = f"{MARK_WRAPPERS[mark]}{text}{MARK_WRAPPERS[mark]}"
        if "link" in marks:
            text = f"[{text}]({(marks['link'].get('attrs') or {}).get('href', '')})"
        out.append(text)
    return "".join(out)


def _image(node: dict[str, Any]) -> str:
    """A captioned image as `![alt](src "caption")`, wrapped in a link when the image has one."""
    parts = {child.get("type"): child for child in node.get("content") or []}
    attrs = (parts.get("image2") or {}).get("attrs") or {}
    caption = _inline((parts.get("caption") or {}).get("content") or []).replace('"', "'")
    title = f' "{caption}"' if caption else ""
    image = f"![{attrs.get('alt') or ''}]({attrs.get('src', '')}{title})"
    return f"[{image}]({attrs['href']})" if attrs.get("href") else image


def _list(node: dict[str, Any], indent: str) -> str:
    """A bullet or numbered list, with nested lists indented under their item."""
    ordered = node.get("type") == "ordered_list"
    start = int((node.get("attrs") or {}).get("start") or 1)
    lines = []
    for number, item in enumerate(node.get("content") or [], start=start):
        marker = f"{number}. " if ordered else "- "
        blocks = [_block(child, indent + " " * len(marker)) for child in item.get("content") or []]
        lines.append(f"{indent}{marker}{blocks[0].lstrip() if blocks else ''}")
        lines.extend(blocks[1:])
    return "\n".join(lines)


def _block(node: dict[str, Any], indent: str = "") -> str:
    """One block-level node as Markdown; raise UnsupportedNodeError for anything Markdown can't hold."""
    kind = node.get("type")
    content = node.get("content") or []
    if kind == "paragraph":
        return indent + _inline(content)
    if kind == "heading":
        return "#" * int((node.get("attrs") or {}).get("level") or 2) + " " + _inline(content)
    if kind == "captionedImage":
        return _image(node)
    if kind in ("bullet_list", "ordered_list"):
        return _list(node, indent)
    if kind == "blockquote":
        return "\n>\n".join("> " + _block(child) for child in content)
    if kind == "horizontal_rule":
        return "---"
    if kind == "codeBlock":
        language = (node.get("attrs") or {}).get("language") or ""
        return f"```{language}\n{''.join(child.get('text', '') for child in content)}\n```"
    raise UnsupportedNodeError(f"block node {kind!r}")


def doc_to_markdown(body: dict[str, Any]) -> str:
    """The whole draft body as Markdown, blocks separated by blank lines."""
    blocks = [_block(node) for node in body.get("content") or []]
    # The editor leaves empty paragraphs where Enter was pressed twice; they carry nothing.
    return "\n\n".join(block for block in blocks if block.strip()) + "\n"
