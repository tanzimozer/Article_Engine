"""Stage 6 — the judge panel.

Seven dimensions, all of which must pass. Six come from the inherited rubric;
``seo`` is added here because that rubric has no SEO dimension and this
pipeline is search-driven.

Judges score independently and never see each other's verdicts. That is not
ceremony — showing one judge another's score anchors it, and three anchored
scores are one score wearing a hat.

Every score below the maximum needs a quoted excerpt as evidence. A judge that
says "voice: 62" with no receipt cannot be audited or improved against.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, skills, state
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)

# Dimensions carried over from Skill-Cabinet's RUBRIC.md. They share one
# generic prompt built from that rubric; only `seo` has its own file, since
# the rubric has nothing to say about it.
INHERITED_DIMENSIONS = {
    "voice_brand_compliance",
    "structural_format_compliance",
    "editorial_value",
    "factual_venue_integrity",
    "seattle_local_specificity",
    "ai_pattern_detection",
}

_GENERIC_JUDGE = """You are a single-dimension judge on an editorial panel.

You are scoring ONE dimension: `{dimension}`

Score it 1-10 against the rubric below. The passing mark is {threshold}.

Be the skeptic, not the cheerleader. This rubric exists because a lenient
grader previously let posts ship at half their intended word count and missed
a voice trap entirely.

Every score below 10 REQUIRES a quoted excerpt from the draft as evidence. A
score with no receipt is unauditable and will be discarded.

You are judging only `{dimension}`. Ignore every other dimension, including
ones you think matter more. Another judge has them.

GUARDRAILS
- Score and give feedback. Never edit the draft.
- You cannot see other judges' verdicts and must not speculate about them.
- If the draft is unscorable on this dimension because required context is
  missing, say so rather than guessing a number.

OUTPUT

{{
  "dimension": "{dimension}",
  "score": <int 1-10>,
  "evidence": [{{"claim": "<what you observed>", "excerpt": "<quoted from draft>"}}],
  "revision_notes": ["<specific, actionable>"],
  "pass": <bool>
}}
"""


def _judge_payload(article: dict[str, Any], event: dict[str, Any],
                   draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft": draft,
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "research_trail": json.loads(event.get("sources_json") or "[]"),
    }


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")
    if not draft:
        return {"ok": False, "reason": "no draft to judge", "retry_from": "s4_write"}

    threshold = cfg["settings"]["thresholds"]["judge_pass_score"]
    dimensions = cfg["settings"]["judges"]

    try:
        rubric_text = skills.rubric(cfg)
    except skills.SkillsUnavailable as exc:
        raise InfraFailure(f"rubric unavailable: {exc}") from exc

    payload = _judge_payload(article, event, draft)
    verdicts: dict[str, Any] = {}

    for dimension in dimensions:
        try:
            if dimension in INHERITED_DIMENSIONS:
                verdict = roles.run_role(
                    dimension, payload,
                    system_override=(
                        _GENERIC_JUDGE.format(dimension=dimension, threshold=threshold)
                        + "\n\n# RUBRIC\n\n" + rubric_text
                    ),
                    timeout_s=420,
                )
            else:
                verdict = roles.run_role(dimension_prompt(dimension), payload,
                                         timeout_s=420)
        except roles.RoleError as exc:
            raise InfraFailure(f"judge {dimension} unavailable: {exc}") from exc

        score = int(verdict.get("score", 0))
        verdict["pass"] = score >= threshold
        verdicts[dimension] = verdict
        log.info("%s judge %s: %d/%d %s", article_id, dimension, score, threshold,
                 "PASS" if verdict["pass"] else "FAIL")

    state.update_article(conn, article_id, judges_json=verdicts)

    failed = [d for d, v in verdicts.items() if not v["pass"]]
    if failed:
        return {
            "ok": False,
            "reason": f"failed: {', '.join(failed)}",
            "retry_from": "s4_write",
        }

    return {"ok": True}


def dimension_prompt(dimension: str) -> str:
    """Prompt filename stem for a dimension that has its own file."""
    return f"judge_{dimension}"
