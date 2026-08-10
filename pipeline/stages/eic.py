"""Stages 7 and 8 — the Editor in Chief.

Runs the inherited ``magazine-eic`` harness. The EIC sees the article plus the
whole trail behind it: every judge score, how many revisions it took, and what
the source verification actually found. A piece that scraped a pass on its
third attempt should not look identical to one that passed cleanly first time.

One deliberate deviation from that harness. It states "Never publish without
the owner's go, however green the gates are." This pipeline auto-publishes
when every gate passes. That override is a project decision, recorded here so
nobody later reads it as a bug.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, skills, state
from pipeline.errors import Held, InfraFailure

log = logging.getLogger(__name__)

_EIC_INSTRUCTIONS = """You are the Editor in Chief, running the harness above.

You receive a draft that has already cleared every hard gate and every judge.
Your job is the final read: does this run, and does it need adjustment first?

PIPELINE OVERRIDE, read this carefully:
The harness you are running says never to publish without the owner's go.
That rule is suspended for this pipeline. A PASS from you publishes the
article automatically. Judge accordingly — there is no human behind you.

You may make light editorial adjustments. You may not rewrite the piece. If it
needs a rewrite, return HOLD and say why.

OUTPUT

{
  "verdict": "PASS" | "HOLD",
  "reason": "<one or two sentences>",
  "adjustments": {
    "hook_line": "<revised, or omit if unchanged>",
    "sections": [{"heading": "...", "body": "..."}],
    "meta_title": "<revised, or omit>",
    "meta_description": "<revised, or omit>"
  },
  "notes": ["<anything the owner should know even on a PASS>"]
}

Omit `adjustments` entirely when nothing needs changing.
"""


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")
    if not draft:
        return {"ok": False, "reason": "no draft to review", "retry_from": "s4_write"}

    try:
        harness = skills.eic_harness(cfg)
    except skills.SkillsUnavailable as exc:
        raise InfraFailure(f"EIC harness unavailable: {exc}") from exc

    payload = {
        "draft": draft,
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "history": {
            "judge_verdicts": json.loads(article.get("judges_json") or "{}"),
            "fact_check": json.loads(article.get("factcheck_json") or "{}"),
            "revision_attempts": json.loads(article.get("attempts_json") or "{}"),
            "reholds": article.get("reholds", 0),
            "source_verified": bool(event.get("verified")),
            "sources_checked": json.loads(event.get("sources_json") or "[]"),
            "relevance_score": article.get("relevance_avg"),
        },
    }

    try:
        result = roles.run_role(
            "eic", payload,
            system_override=f"{harness}\n\n---\n\n{_EIC_INSTRUCTIONS}",
            timeout_s=900,
        )
    except roles.RoleError as exc:
        raise InfraFailure(f"EIC unavailable: {exc}") from exc

    verdict = result.get("verdict")
    reason = result.get("reason", "no reason given")

    if verdict != "PASS":
        raise Held("s7_eic", f"EIC held: {reason}")

    adjustments = result.get("adjustments") or {}
    if adjustments:
        draft = {**draft, **{k: v for k, v in adjustments.items() if v}}
        log.info("%s EIC adjusted: %s", article_id, ", ".join(adjustments))

    state.update_article(conn, article_id, draft_json=draft, eic_json=result)
    log.info("%s EIC PASS: %s", article_id, reason)
    return {"ok": True}
