"""Stage 5 — fact-check the draft.

Separate from Stage 3 on purpose. Stage 3 verifies the event data; this
verifies what the writer did with it. Drafting introduces claims that were
never in the source, and those are exactly what this strips.

Zero medical or health claims. The article describes an event. It does not
tell anyone what will happen to their body.

The revise loop lives inside this function rather than in the orchestrator,
because the fact-checker returns the corrected text itself — there is nothing
to hand back to the writer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, skills, state
from pipeline.errors import Held, InfraFailure

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")
    if not draft:
        return {"ok": False, "reason": "no draft to check", "retry_from": "s4_write"}

    max_loops = cfg["settings"]["thresholds"]["max_revise_attempts"]

    try:
        house = skills.voice_handbook(cfg)
    except skills.SkillsUnavailable as exc:
        raise InfraFailure(f"voice handbook unavailable: {exc}") from exc

    verified = {k: v for k, v in event.items() if not k.endswith("_json")}
    verified["transit"] = json.loads(event.get("transit_json") or "[]")

    changelog: list[dict[str, Any]] = []

    for loop in range(1, max_loops + 1):
        payload = {
            "draft": draft,
            "verified_source_data": verified,
            "rules": {
                "strip_unsourced_claims": True,
                "medical_health_claims_allowed": False,
                "edit_scope": "accuracy only, never tone or structure",
            },
        }

        try:
            result = roles.run_role(
                "fact_checker", payload,
                extra_context=f"# VOICE HANDBOOK (do not edit toward it, only avoid violating it)\n\n{house}",
                timeout_s=600,
            )
        except roles.RoleError as exc:
            raise InfraFailure(f"fact checker unavailable: {exc}") from exc

        changelog.extend(result.get("changelog", []))
        revised = result.get("revised") or {}
        if revised:
            draft = {**draft, **revised}

        if result.get("clean"):
            state.update_article(
                conn, article_id,
                draft_json=draft,
                factcheck_json={"changelog": changelog, "loops": loop, "clean": True},
            )
            log.info("%s fact-checked clean after %d loop(s), %d change(s)",
                     article_id, loop, len(changelog))
            return {"ok": True}

        log.info("%s fact-check loop %d/%d, %d change(s) so far",
                 article_id, loop, max_loops, len(changelog))

    state.update_article(
        conn, article_id,
        draft_json=draft,
        factcheck_json={"changelog": changelog, "loops": max_loops, "clean": False},
    )
    raise Held(
        "s5_factcheck",
        f"still not clean after {max_loops} revise loops "
        f"({len(changelog)} change(s) attempted)",
    )
