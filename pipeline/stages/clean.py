"""Stage 2 — clean and organise.

Three steps: collapse the same real event across sources, keep a record of
which source each surviving field came from, then fill in what the sources
did not provide.

The provenance tags are not bookkeeping. Stage 3 cross-checks fields against
their sources, and a merge that discards which source said what would leave
it nothing to check.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from typing import Any

from pipeline import enrich, roles, state

log = logging.getLogger(__name__)


def _canonical_id(canonical: dict[str, Any]) -> str:
    """Deterministic id from the fields that identify a real event.

    Deterministic rather than random so a re-run that produces the same merge
    lands on the same row instead of creating a duplicate.
    """
    seed = "|".join([
        (canonical.get("title") or "").lower().strip(),
        (canonical.get("start_dt") or "")[:16],
        (canonical.get("venue_name") or "").lower().strip(),
    ])
    return "evt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def run(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Drain candidates into canonical events and queue articles for them."""
    candidates = state.take_candidates(conn)
    if not candidates:
        return 0

    payload = {
        "priority_order": cfg["sources"].get("priority_order", []),
        "events": [
            {
                "source": c["source"],
                "source_id": c["source_id"],
                "priority": c["priority"],
                **c["raw"],
            }
            for c in candidates
        ],
    }

    try:
        merged = roles.run_role("dedupe_agent", payload)
    except roles.RoleError:
        log.exception("dedupe failed; leaving candidates for the next run")
        return 0

    groups = merged.get("groups", [])
    log.info("dedupe: %d candidate(s) collapsed into %d event(s)",
             len(candidates), len(groups))

    # Best relevance score across a merged group carries forward, so an event
    # is not penalised for one source having a thin description.
    score_by_source_id = {c["id"]: c["relevance_avg"] or 0.0 for c in candidates}

    created = 0
    consumed: list[str] = []

    for group in groups:
        canonical = group.get("canonical") or {}
        provenance = group.get("provenance") or {}
        members = group.get("member_source_ids") or []

        event_id = _canonical_id(canonical)
        best_score = max(
            (score_by_source_id.get(m, 0.0) for m in members), default=0.0
        )

        try:
            canonical = enrich.enrich_event(canonical)
        except Exception:
            log.exception("enrichment failed for %s; storing unenriched",
                          canonical.get("title"))

        canonical["id"] = event_id
        canonical["provenance_json"] = provenance
        canonical["sources_json"] = members
        state.upsert_event(conn, canonical)

        for member in members:
            source, _, source_id = member.partition(":")
            if source and source_id:
                state.record_fingerprint(
                    conn, source, source_id,
                    state.content_hash(
                        canonical.get("start_dt"),
                        canonical.get("venue_name"),
                        canonical.get("price"),
                    ),
                    canonical_id=event_id,
                )

        article_id = f"art_{uuid.uuid4().hex[:12]}"
        state.create_article(conn, article_id, event_id, best_score)
        created += 1
        consumed.extend(members)

    state.clear_candidates(conn, [c["id"] for c in candidates])
    log.info("clean: %d article(s) queued", created)
    return created
