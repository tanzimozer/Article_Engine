"""Stage 4b -- the voice pass.

Sits between the writer and the fact-checker, deliberately in that order.

A voice pass makes prose vivid, and vivid is exactly where invention creeps in:
a flat course, a thinning crowd, the light off the lake. Running it *before*
the fact-checker keeps that stage the last thing to touch prose, so anything
this pass invents is still caught. Running it after would have reopened the
hallucination gap the whole pipeline exists to close.

The stage exists because of a measured regression. When Stage 3b began handing
the writer thirty-plus sourced facts, six judge dimensions rose and voice fell
from 8 to 5 -- "a race-registration FAQ transcribed into paragraph form". The
writer was juggling a fact trail, a word band, a section count, an SEO phrase
and forty-one banned words; voice is the one thing in that list nothing counts,
so it is the one that loses. Splitting it out is the same move that made
research work: one job per stage.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, skills, state
from pipeline.errors import InfraFailure
from pipeline.stages.write import prose_words as _words  # one counter, everywhere

log = logging.getLogger(__name__)

#: Fields the pass may rewrite. Anything outside this is copied through
#: untouched, so a voice edit can never quietly drop a key the writer set.
REWRITABLE = ("hook_line", "sections", "disclaimer", "meta_title",
              "meta_description", "slug_stem")


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    draft = json.loads(article.get("draft_json") or "{}")
    if not draft.get("sections"):
        return {"ok": False, "reason": "no draft to edit", "retry_from": "s4_write"}

    article_cfg = cfg["settings"]["article"]
    before_words = _words(draft)
    before_sections = len(draft["sections"])

    try:
        house = skills.voice_handbook(cfg)
    except skills.SkillsUnavailable as exc:
        # Same reasoning as the writer: editing toward a voice standard without
        # the standard produces confident, wrong copy.
        raise InfraFailure(f"voice handbook unavailable: {exc}") from exc

    payload = {
        "draft": draft,
        # Supplied so the editor can see which facts are load-bearing and how
        # firmly each is sourced. It may NOT pull anything new from here -- the
        # writer decided coverage; this stage decides how it reads.
        "research": json.loads(article.get("research_json") or "{}"),
        "constraints": {
            "sections_min": article_cfg["sections_min"],
            "sections_max": article_cfg["sections_max"],
            "keep_section_count": before_sections,
            "no_em_dashes": True,
            "no_new_facts": True,
        },
    }

    try:
        result = roles.run_role(
            "voice_editor", payload,
            extra_context=f"# VOICE HANDBOOK (authoritative)\n\n{house}",
            timeout_s=1200,
        )
    except roles.RoleError as exc:
        raise InfraFailure(f"voice editor unavailable: {exc}") from exc

    revised = result.get("revised") or {}
    sections = revised.get("sections") or []
    if not sections:
        return {"ok": False, "reason": "voice pass returned no sections",
                "retry_from": "s4_write"}

    if len(sections) != before_sections:
        # Restructuring is the writer's job. A pass that changes the shape has
        # exceeded its brief, and the section count is load-bearing downstream.
        return {
            "ok": False,
            "reason": f"voice pass changed the section count from "
                      f"{before_sections} to {len(sections)}",
            "retry_from": "s4_write",
        }

    merged = dict(draft)
    for key in REWRITABLE:
        if revised.get(key):
            merged[key] = revised[key]

    after_words = _words(merged)
    drift = after_words - before_words
    if abs(drift) > before_words * 0.25:
        # A quarter of the piece is a rewrite, not an edit, and it is the shape
        # a pass takes when it starts adding rather than reworking.
        return {
            "ok": False,
            "reason": f"voice pass moved the word count {drift:+d} "
                      f"({before_words} to {after_words}), beyond an edit",
            "retry_from": "s4_write",
        }

    state.update_article(conn, article_id, draft_json=merged,
                         voice_json={"changes": result.get("changes") or [],
                                     "unchanged": result.get("unchanged") or []})

    changes = result.get("changes") or []
    log.info("%s voice pass: %d change(s), %d section(s) left alone, "
             "%d -> %d words (%+d)",
             article_id, len(changes), len(result.get("unchanged") or []),
             before_words, after_words, drift)
    for change in changes[:6]:
        log.debug("  %s: %s", change.get("section"), change.get("problem"))

    return {"ok": True}
