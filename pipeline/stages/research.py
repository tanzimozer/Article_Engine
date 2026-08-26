"""Stage 3b -- fact research.

Stage 3 verifies. This stage *learns*.

The distinction is the reason articles read thin without it. Verification
cross-checks three fields against a listing the pipeline already had; it can
confirm a price is still $32 but it can never discover that the venue is a
former naval air station, that the operator sold during the pandemic, or that
packet pickup is the night before. Everything an article knows beyond its
original listing has to come from somewhere, and until this stage existed
there was nowhere for it to come from.

The reference implementation in `Current-Article-Engine` puts research second
of eight for the same reason. Its own README names the failure mode: models
trust a search-snippet summary instead of the primary source, or silently pick
one number when three conflicting ones exist. The prompt carries the fixes.

Output is a fact trail, never prose. The writer drafts from it and does not
re-verify, so a low-confidence fact that slips through here is a low-confidence
sentence in a published article.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, state
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)

#: Scoped deliberately. A generic "research everything" brief produces a worse
#: trail than a short explicit list of what the writer will actually need.
QUESTIONS: dict[str, list[str]] = {
    "event": [
        "Exact start time, and whether waves, heats or distances start separately.",
        "Every distance, category or format offered, with the exact price of each.",
        "Registration status now: open, closed, sold out, waitlist, cancelled, postponed.",
        "Registration deadlines and whether day-of signup exists.",
        "Packet or bib pickup: where and when.",
        "The course: surface, terrain, elevation, loop or point-to-point.",
        "What the organiser provides on the day, and what participants must bring.",
        "Who the organiser is, and whether this event has run before.",
    ],
    "venue_feature": [
        "Founding: when, by whom, and the actual story if one exists.",
        "Every current location with full street address.",
        "Opening hours, per location where they differ.",
        "Full pricing: memberships, day rates, joining fees, freeze and cancellation "
        "terms, youth and concession rates.",
        "What the venue offers beyond its headline activity.",
        "Ownership changes, expansions, closures.",
        "Anything currently running there: leagues, series, classes, events.",
        "How this location relates to the operator's other sites, if any.",
    ],
}


def _subject_line(event: dict[str, Any], content_type: str) -> str:
    """What the researcher is being pointed at."""
    if content_type == "venue_feature":
        parts = [event.get("venue_name"), event.get("venue_address")]
    else:
        parts = [event.get("title"), event.get("venue_name"), event.get("start_dt")]
    return ", ".join(str(p) for p in parts if p)


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])

    content_type = event.get("content_type") or "event"
    questions = QUESTIONS.get(content_type, QUESTIONS["event"])

    payload = {
        "subject": _subject_line(event, content_type),
        "content_type": content_type,
        # The verified record so far: a floor to cross-check against, never a
        # ceiling on what the researcher may find.
        "known": {k: v for k, v in event.items() if not k.endswith("_json")},
        "venue_context": json.loads(event.get("venue_context_json") or "[]"),
        "questions": questions,
    }

    try:
        trail = roles.run_role(
            "fact_researcher", payload,
            # The only role besides the validator that touches the network, and
            # the only one that searches. It has to find pages before it can
            # read them.
            timeout_s=1800,
        )
    except roles.RoleError as exc:
        raise InfraFailure(f"fact researcher unavailable: {exc}") from exc

    if trail.get("kill"):
        # A killed assignment costs one research call. A published piece built
        # on nothing costs more than that.
        reason = trail.get("kill_reason") or "no verifiable angle found"
        state.update_article(conn, article_id, research_json=trail, status="dropped")
        log.info("%s killed at research: %s", article_id, reason)
        return {"ok": False, "reason": f"killed at research: {reason}", "drop": True}

    facts = trail.get("facts") or []
    solid = [f for f in facts if f.get("confidence") in ("high", "medium")]
    fetched = trail.get("sources_fetched") or []

    state.update_article(conn, article_id, research_json=trail)
    log.info(
        "%s researched: %d fact(s) (%d high/medium), %d conflict(s), "
        "%d unverified, %d source(s) fetched",
        article_id, len(facts), len(solid), len(trail.get("conflicts") or []),
        len(trail.get("unverified") or []), len(fetched),
    )

    # A trail this thin cannot support an article, and the writer would fill the
    # gap by inventing -- which the fact-checker then deletes, leaving a piece
    # too short to publish. Cheaper to say so here.
    if len(solid) < 5:
        return {
            "ok": False,
            "reason": f"only {len(solid)} fact(s) at high or medium confidence across "
                      f"{len(fetched)} fetched source(s); not enough verified material "
                      f"to write from",
            "retry_from": "s3b_research",
        }

    return {"ok": True}
