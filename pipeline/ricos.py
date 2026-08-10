"""Ricos node builders for the Seattle events article pipeline.

Turns our article structure (hook line, details box, sections, disclaimer,
optional hero image) into a Wix Ricos ``richContent`` document.

Spacing is the owner standard from the magazine-eic skill, Step 7. All three
rules or sections space inconsistently:

1. Trailing ``\\n`` TEXT node on every paragraph -> the gap *above* each H2.
2. Leading ``\\n`` TEXT node on every paragraph too -> the gap *below* each H2.
   Skip it and one-line headings look fine while two-line headings render
   visibly cramped.
3. A spacer paragraph (a lone ``\\n`` TEXT node) after every IMAGE. Otherwise
   the next H2 sits flush against the bottom of the photo.

Rules 1 and 2 are applied to *every* paragraph node this module emits,
including the details-box lines, because that is what the owner standard says.
The details box is the one place where that reads as generous line spacing; it
is deliberate, not a bug.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

Node = dict[str, Any]

#: Ricos document schema version carried in ``richContent.metadata``.
RICOS_VERSION = 1

#: The lone character that does all the spacing work. See module docstring.
NEWLINE = "\n"

#: Details-box fields, in the owner's fixed order. (normalized key, printed label)
#: Mirrors ``config/settings.yaml`` -> ``article.details_box_fields`` and
#: SPEC.md §2. The order is locked; change SPEC.md first.
DETAILS_FIELDS: tuple[tuple[str, str], ...] = (
    ("when", "When"),
    ("where", "Where"),
    ("cost", "Cost"),
    ("register", "Register"),
    ("skill_level", "Skill level"),
    ("what_to_bring", "What to bring"),
)

#: Fields allowed to be absent/None without a warning. Register is dropped
#: entirely when it has no value -- no empty line, no "TBA".
OPTIONAL_DETAILS_FIELDS = frozenset({"register"})

_BOLD_DECORATION: Node = {"type": "BOLD", "fontWeightValue": 700}


# --------------------------------------------------------------------------- #
# Leaf nodes
# --------------------------------------------------------------------------- #
def _text_node(text: str, *, bold: bool = False) -> Node:
    """A Ricos TEXT node, optionally bold."""
    return {
        "type": "TEXT",
        "id": "",
        "nodes": [],
        "textData": {
            "text": text,
            "decorations": [dict(_BOLD_DECORATION)] if bold else [],
        },
    }


def _newline_node() -> Node:
    """The bare ``\\n`` TEXT node used by all three spacing rules."""
    return _text_node(NEWLINE)


def _paragraph(runs: Sequence[Node]) -> Node:
    """A PARAGRAPH node wrapped in the leading + trailing newline TEXT nodes.

    Spacing rules 1 and 2 live here, so no caller has to remember them.
    """
    return {
        "type": "PARAGRAPH",
        "id": "",
        "nodes": [_newline_node(), *runs, _newline_node()],
        "paragraphData": {
            "textStyle": {"textAlignment": "AUTO"},
            "indentation": 0,
        },
    }


def _text_paragraph(text: str) -> Node:
    """A plain single-run paragraph."""
    return _paragraph([_text_node(text)])


def _spacer_paragraph() -> Node:
    """Spacing rule 3: a paragraph holding a *lone* ``\\n`` TEXT node.

    Deliberately not built through :func:`_paragraph` -- the spacer is one
    newline node, not a wrapped empty run.
    """
    return {
        "type": "PARAGRAPH",
        "id": "",
        "nodes": [_newline_node()],
        "paragraphData": {
            "textStyle": {"textAlignment": "AUTO"},
            "indentation": 0,
        },
    }


def banner_node(text: str) -> Node:
    """A bold status line for the top of an already-published post.

    The post-publish sweep prepends one of these when an event ends, is
    cancelled, or sells out. The article keeps its URL and whatever search
    authority it has accumulated; only this line is added.

    Bold rather than a heading, so it reads as a notice rather than becoming
    the article's first section.
    """
    return _paragraph([_text_node(text, bold=True)])


def _heading(text: str, level: int = 2) -> Node:
    """A real HEADING node -- never bold text pretending to be a heading.

    Always H2. SPEC.md §2 bans H3 anywhere on this surface.
    """
    return {
        "type": "HEADING",
        "id": "",
        "nodes": [_text_node(text)],
        "headingData": {
            "level": level,
            "textStyle": {"textAlignment": "AUTO"},
            "indentation": 0,
        },
    }


def _image_node(image_url: str, image_alt: str | None) -> Node:
    """An IMAGE node from an external URL.

    Wix-hosted media would use ``image.src.id``; a plain URL goes in
    ``image.src.url``.
    """
    return {
        "type": "IMAGE",
        "id": "",
        "nodes": [],
        "imageData": {
            "containerData": {
                "width": {"size": "CONTENT"},
                "alignment": "CENTER",
                "textWrap": True,
            },
            "image": {"src": {"url": image_url}},
            "altText": image_alt or "",
        },
    }


# --------------------------------------------------------------------------- #
# Structure helpers
# --------------------------------------------------------------------------- #
def _split_paragraphs(text: str) -> list[str]:
    """Split prose on blank lines. House style is one paragraph per section,
    but a writer handing over two blocks should not get them welded together.
    """
    if not text:
        return []
    blocks = [block.strip() for block in text.replace("\r\n", "\n").split("\n\n")]
    return [block for block in blocks if block]


def _normalize_key(key: Any) -> str:
    """``"Skill level"``, ``"skill-level"`` and ``"skillLevel"`` are one field."""
    raw = str(key).strip()
    out: list[str] = []
    for index, char in enumerate(raw):
        if char in " -/":
            out.append("_")
        elif char.isupper() and index and raw[index - 1].islower():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char.lower())
    return "".join(out).strip("_")


def _details_nodes(details_box: dict[str, Any]) -> list[Node]:
    """Bold label + value, one per line, as separate paragraph nodes.

    Not bullets, not a table. Register is omitted entirely when its value is
    None; any other missing field is skipped with a warning, because the
    template expects it.
    """
    normalized = {_normalize_key(key): value for key, value in (details_box or {}).items()}

    def line(label: str, value: Any) -> Node:
        return _paragraph(
            [_text_node(f"{label}: ", bold=True), _text_node(str(value).strip())]
        )

    def is_blank(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    nodes: list[Node] = []
    for key, label in DETAILS_FIELDS:
        value = normalized.get(key)
        if is_blank(value):
            if key not in OPTIONAL_DETAILS_FIELDS:
                logger.warning("details box missing %r -- line dropped from the box", label)
            continue
        nodes.append(line(label, value))

    # Anything the caller added beyond the locked six renders after them rather
    # than being dropped -- stages/publish.py appends a `getting_there` transit
    # line, and SPEC.md §2 requires transit on every article.
    for key in normalized:
        if key in {known for known, _ in DETAILS_FIELDS} or is_blank(normalized[key]):
            continue
        label = key.replace("_", " ").strip().capitalize()
        logger.info("details box: rendering extra field %r after the fixed six", label)
        nodes.append(line(label, normalized[key]))

    return nodes


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_rich_content(
    hook_line: str,
    details_box: dict[str, Any],
    sections: list[dict[str, str]],
    disclaimer: str,
    image_url: str | None = None,
    image_alt: str | None = None,
) -> dict[str, Any]:
    """Build the Wix Ricos ``richContent`` document for one events article.

    Document order:

    1. hook line
    2. details box (When / Where / Cost / [Register] / Skill level / What to bring)
    3. hero image, plus its mandatory spacer paragraph
    4. one H2 + body per section
    5. disclaimer

    Args:
        hook_line: The opening line, above the details box.
        details_box: Field values for the box. Keys are matched loosely
            (``"skill_level"``, ``"Skill level"``, ``"skillLevel"``). A
            ``register`` of None drops the Register line entirely.
        sections: ``[{"heading": str, "body": str}, ...]``, in running order.
        disclaimer: Closing disclaimer paragraph.
        image_url: Optional hero image URL. Placed after the details box, so
            the first H2 follows the photo -- which is why rule 3 exists.
        image_alt: Alt text for that image.

    Returns:
        A ``richContent`` dict ready to hand to ``WixClient.create_draft`` or
        ``WixClient.update_published``.
    """
    nodes: list[Node] = []

    for block in _split_paragraphs(hook_line):
        nodes.append(_text_paragraph(block))

    nodes.extend(_details_nodes(details_box))

    if image_url:
        nodes.append(_image_node(image_url, image_alt))
        nodes.append(_spacer_paragraph())  # rule 3 -- keeps the next H2 off the photo
    elif image_alt:
        logger.warning("image_alt supplied without image_url -- no IMAGE node emitted")

    for index, section in enumerate(sections or []):
        heading = (section.get("heading") or "").strip()
        body = section.get("body") or ""
        if not heading:
            logger.warning("section %d has no heading -- body emitted without an H2", index)
        else:
            nodes.append(_heading(heading, level=2))
        for block in _split_paragraphs(body):
            nodes.append(_text_paragraph(block))
        if not body.strip():
            logger.warning("section %r has an empty body", heading or index)

    for block in _split_paragraphs(disclaimer):
        nodes.append(_text_paragraph(block))

    logger.debug(
        "built richContent: %d nodes (%d sections, image=%s)",
        len(nodes),
        len(sections or []),
        bool(image_url),
    )
    return {
        "nodes": nodes,
        "metadata": {"version": RICOS_VERSION},
        "documentStyle": {},
    }


def extract_text(rich_content: dict[str, Any] | None) -> str:
    """Flatten every TEXT node in a Ricos document into one string.

    Used by ``WixClient.verify_published`` to check a live post's body without
    trusting a CDN render or a summarizer.
    """
    collected: list[str] = []

    def walk(items: Iterable[Any]) -> None:
        for node in items or []:
            if not isinstance(node, dict):
                continue
            text = (node.get("textData") or {}).get("text")
            if isinstance(text, str):
                collected.append(text)
            walk(node.get("nodes") or [])

    walk((rich_content or {}).get("nodes") or [])
    return "".join(collected)
