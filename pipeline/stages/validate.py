"""Stage 3 — validate event data.

Runs before a single word is written, because the cheapest place to catch a
wrong date is before you have paid to draft, fact-check and judge an article
built on it.

Bar: the source re-fetches unchanged AND two or more sources agree on date,
venue and price. The one exception is a single source that is either
Eventbrite or the venue's own site, since both are authoritative for their own
events and plenty of small local events appear nowhere else.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, state
from pipeline.errors import Held, InfraFailure

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    if article is None:
        raise Held("s3_validate", "article row vanished")

    event = state.get_event(conn, article["event_id"])
    if event is None:
        raise Held("s3_validate", "event row vanished")

    if event.get("verified"):
        return {"ok": True}

    payload = {
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "provenance": json.loads(event.get("provenance_json") or "{}"),
        "sources": json.loads(event.get("sources_json") or "[]"),
        "priority_order": cfg["sources"].get("priority_order", []),
        "single_source_exceptions": ["eventbrite", "venue_site"],
    }

    try:
        result = roles.run_role("cross_check_validator", payload)
    except roles.RoleError as exc:
        raise InfraFailure(f"validator unavailable: {exc}") from exc

    verdict = result.get("verdict")
    checked = result.get("sources_checked", [])

    if verdict != "VERIFIED":
        raise Held(
            "s3_validate",
            f"unverified: {result.get('reason', 'no reason given')} "
            f"(checked {len(checked)} source(s))",
        )

    state.upsert_event(conn, {**event, "verified": 1})
    log.info("%s verified against %d source(s)", article_id, len(checked))
    return {"ok": True}
