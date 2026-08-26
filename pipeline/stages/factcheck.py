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
from pipeline.stages.write import prose_words
from pipeline.errors import Held, InfraFailure

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")
    if not draft:
        return {"ok": False, "reason": "no draft to check", "retry_from": "s4_write"}

    # Falls back to max_revise_attempts so an older settings file still runs.
    thresholds = cfg["settings"]["thresholds"]
    max_loops = thresholds.get(
        "max_factcheck_loops", thresholds["max_revise_attempts"]
    )

    try:
        house = skills.voice_handbook(cfg)
    except skills.SkillsUnavailable as exc:
        raise InfraFailure(f"voice handbook unavailable: {exc}") from exc

    verified = {k: v for k, v in event.items() if not k.endswith("_json")}
    verified["transit"] = json.loads(event.get("transit_json") or "[]")
    # Without this the checker deletes every sentence about the venue itself,
    # because none of it appears in the event record -- which is exactly what
    # happened to "flat course" twice while the local-specificity judge was
    # simultaneously failing the article for having no sense of place.
    verified["venue_context"] = json.loads(event.get("venue_context_json") or "[]")
    # The stage 3b fact trail. Omitting this is catastrophic and silent: the
    # writer drafts from research the checker cannot see, so every sourced fact
    # reads as invention and gets deleted. One run lost 71% of its prose that
    # way -- packet pickup, lap counts, course surface, terrain, start waves,
    # all of them verified with URLs, all struck as untraceable.
    verified["research"] = json.loads(article.get("research_json") or "{}")

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
                # Loop 1 measured 529s against a 600s ceiling -- 88% of budget
                # on a thin article. Every claim added to the draft is another
                # claim to verify, so this scales with payload the same way the
                # writer does.
                timeout_s=1200,
            )
        except roles.RoleError as exc:
            raise InfraFailure(f"fact checker unavailable: {exc}") from exc

        changelog.extend(result.get("changelog", []))
        revised = result.get("revised") or {}
        if revised:
            draft = {**draft, **revised}

        # The fact-checker can only rewrite hook_line, sections and disclaimer.
        # A bad claim in meta_description is real but unreachable from here, so
        # it reports it and leaves the field alone. Looping on that would burn
        # all three attempts on something this stage cannot fix — send it back
        # to the writer instead.
        out_of_scope = [
            entry for entry in result.get("changelog", [])
            if str(entry.get("reason", "")).startswith("OUT OF SCOPE")
        ]
        if out_of_scope and not result.get("clean"):
            state.update_article(
                conn, article_id,
                draft_json=draft,
                factcheck_json={"changelog": changelog, "loops": loop, "clean": False},
            )
            fields = ", ".join(
                str(e.get("reason", "")).split(":", 1)[0] for e in out_of_scope
            )
            return {
                "ok": False,
                "reason": f"unfixable here, needs a rewrite: {fields}",
                "retry_from": "s4_write",
            }

        if result.get("clean"):
            state.update_article(
                conn, article_id,
                draft_json=draft,
                factcheck_json={"changelog": changelog, "loops": loop, "clean": True},
            )

            # Re-measure. write.py checks the word range at draft time only,
            # so an article that passed there can arrive at the panel at half
            # the length: the first real run drafted 948 words, fact-checking
            # removed 28 unsupported claims, and 457 words reached seven judges
            # who correctly called it thin. Removing unsupported claims is this
            # stage doing its job; shipping the remains is not.
            floor = cfg["settings"]["article"]["word_count_min"]
            words = prose_words(draft)
            if words < floor:
                log.info("%s fact-checked clean but fell to %d words (floor %d)",
                         article_id, words, floor)
                return {
                    "ok": False,
                    "reason": f"fact-checking removed {len(changelog)} claim(s), leaving "
                              f"{words} words against a {floor}-word floor. The rewrite "
                              f"needs more verifiable substance, not more assertion.",
                    "retry_from": "s4_write",
                }

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
