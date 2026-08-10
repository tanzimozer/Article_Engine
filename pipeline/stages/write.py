"""Stage 4 — write the article.

The writer produces the hook line, 5-6 sections, the disclaimer and the
metadata. It does not produce the details box: that is assembled from verified
fields by :mod:`pipeline.ricos`, because a model retyping a date is a chance
to get the date wrong.

Transit is passed in as verified GTFS data and must be used as given. It is a
factual claim, so anything embellished here gets stripped in Stage 5 anyway.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from dateutil import parser as dateparser

from pipeline import roles, skills, state
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)

# Headroom reserved for the assembled details box, which counts toward the
# article's word ceiling but is not written by the writer. Six fields plus the
# transit line land between 25 and 45 words; the upper bound is used so a long
# venue name cannot push a passing draft over the limit.
DETAILS_BOX_WORD_ALLOWANCE = 45


def _slugify(stem: str, start_dt: str | None) -> str:
    """Lowercase kebab-case, with the event date appended.

    The date suffix is load-bearing. One article per occurrence and no cooldown
    means a weekly run club produces near-identical titles all year, and Wix's
    auto-slug would collide on every one of them.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60]
    if start_dt:
        try:
            date_part = dateparser.parse(start_dt).strftime("%Y-%m-%d")
            return f"{stem}-{date_part}"
        except (ValueError, TypeError):
            pass
    return stem


def _revision_notes(article: dict[str, Any]) -> list[str]:
    """Judge and fact-check feedback from the previous attempt, if any.

    A rewrite with no notes just reproduces the same failure.
    """
    notes: list[str] = []
    for column in ("judges_json", "factcheck_json"):
        blob = article.get(column)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            for judge, verdict in data.items():
                if isinstance(verdict, dict) and not verdict.get("pass", True):
                    notes.extend(
                        f"[{judge}] {n}" for n in verdict.get("revision_notes", [])
                    )
    return notes


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])

    try:
        house = skills.voice_handbook(cfg)
    except skills.SkillsUnavailable as exc:
        # Fatal on purpose. Drafting without the voice handbook produces copy
        # the judges will reject, so failing here is cheaper than failing later.
        raise InfraFailure(f"voice handbook unavailable: {exc}") from exc

    article_cfg = cfg["settings"]["article"]
    payload = {
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "transit": json.loads(event.get("transit_json") or "[]"),
        "constraints": {
            "word_count_min": article_cfg["word_count_min"],
            "word_count_max": article_cfg["word_count_max"],
            "sections_min": article_cfg["sections_min"],
            "sections_max": article_cfg["sections_max"],
            "meta_title_max": article_cfg["meta_title_max"],
            "meta_description_max": article_cfg["meta_description_max"],
            "no_em_dashes": True,
            "no_h3": True,
            "no_bullets": True,
            "no_faq": True,
            "no_byline": True,
        },
        "revision_notes": _revision_notes(article),
    }

    try:
        draft = roles.run_role(
            "writer", payload,
            extra_context=f"# VOICE HANDBOOK (authoritative)\n\n{house}",
            timeout_s=600,
        )
    except roles.RoleError as exc:
        raise InfraFailure(f"writer unavailable: {exc}") from exc

    sections = draft.get("sections") or []
    if not (article_cfg["sections_min"] <= len(sections) <= article_cfg["sections_max"]):
        return {
            "ok": False,
            "reason": f"{len(sections)} sections, expected "
                      f"{article_cfg['sections_min']}-{article_cfg['sections_max']}",
            "retry_from": "s4_write",
        }

    body = " ".join(s.get("body", "") for s in sections)
    words = len(body.split())

    # The inherited gate counts the whole body, and the details box is part of
    # that body even though the writer never sees it. Six assembled fields run
    # 25-45 words, so the writer's ceiling has to sit below the article's or a
    # draft that passes here fails hardgate downstream.
    prose_max = article_cfg["word_count_max"] - DETAILS_BOX_WORD_ALLOWANCE
    if not (article_cfg["word_count_min"] <= words <= prose_max):
        return {
            "ok": False,
            "reason": f"{words} words of prose, expected "
                      f"{article_cfg['word_count_min']}-{prose_max} "
                      f"(leaves room for the {DETAILS_BOX_WORD_ALLOWANCE}-word details box)",
            "retry_from": "s4_write",
        }

    if "—" in body or "—" in draft.get("hook_line", ""):
        return {
            "ok": False,
            "reason": "em-dash present, hard fail on this surface",
            "retry_from": "s4_write",
        }

    slug = _slugify(draft.get("slug_stem", ""), event.get("start_dt"))
    state.update_article(conn, article_id, draft_json=draft, slug=slug)
    log.info("%s drafted: %d words, %d sections", article_id, words, len(sections))
    return {"ok": True}
