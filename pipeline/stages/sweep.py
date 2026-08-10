"""The two sweeps that run before the pipeline on every invocation.

**Approval sweep** — reads the Drive hold queue and returns anything the human
cleared. An approved article resumes at the stage that held it, not from the
start, and its attempt counter for that stage resets to zero. Without the
reset it would walk straight back into the same exhausted counter and re-hold
immediately.

Two re-holds is the cap. An article that has bounced back twice is archived
rather than cycled indefinitely.

**Post-publish sweep** — walks live articles and banners the ones whose events
have ended, been cancelled, or sold out. This re-checks status at the source;
a date comparison alone would miss a cancellation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from dateutil import parser as dateparser

from pipeline import drive, notify, roles, state, wix

log = logging.getLogger(__name__)

ENDED_BANNER = "This event has ended."
CANCELLED_BANNER = "This event was cancelled."
SOLD_OUT_BANNER = "This event is sold out."


def run_approval_sweep(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Return held articles the human approved. Returns how many were cleared."""
    approved_value = cfg["settings"]["drive"]["approved_value"]
    max_reholds = cfg["settings"]["thresholds"]["max_reholds"]
    cleared = 0

    for article in state.held_articles(conn):
        doc_id = article.get("hold_doc_id")
        if not doc_id:
            continue

        status = drive.read_status(cfg, doc_id)
        if status != approved_value:
            continue

        article_id = article["id"]
        reholds = (article.get("reholds") or 0) + 1

        if reholds > max_reholds:
            state.update_article(conn, article_id, status="archived")
            notify.archived(
                cfg, article_id,
                f"exceeded {max_reholds} re-holds; last held at "
                f"{article.get('hold_stage')}",
            )
            log.warning("%s archived after %d re-holds", article_id, reholds)
            continue

        hold_stage = article.get("hold_stage") or "s4_write"
        state.reset_attempts(conn, article_id, hold_stage)
        state.update_article(
            conn, article_id,
            status="queued", stage=hold_stage,
            reholds=reholds, hold_reason=None, hold_stage=None,
        )
        cleared += 1
        log.info("%s approved, resuming at %s (re-hold %d/%d)",
                 article_id, hold_stage, reholds, max_reholds)

    return cleared


def _event_state(event: dict[str, Any], published_row: dict[str, Any]) -> str | None:
    """Decide whether a live article needs a banner.

    Date first, because it is free. Cancellation needs a source check, which
    is the expensive half.
    """
    start = published_row.get("event_start_dt")
    if start:
        try:
            start_dt = dateparser.parse(start)
            if start_dt < datetime.now(start_dt.tzinfo):
                return "ended"
        except (ValueError, TypeError):
            log.warning("unparsable event date on %s", published_row["post_id"])
    return None


def run_postpublish_sweep(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Banner ended, cancelled and sold-out events. Returns banners applied."""
    applied = 0
    dry_run = cfg.get("dry_run")

    for row in state.live_published(conn):
        new_state = _event_state({}, row)

        if new_state is None:
            # Still upcoming. Re-check the source for a cancellation or sellout.
            event = state.get_event(conn, row["event_id"])
            if event is None:
                continue
            try:
                verdict = roles.run_role("cross_check_validator", {
                    "event": {k: v for k, v in event.items() if not k.endswith("_json")},
                    "sources": json.loads(event.get("sources_json") or "[]"),
                    "mode": "status_recheck",
                    "looking_for": ["cancelled", "sold_out"],
                })
            except roles.RoleError:
                log.warning("status re-check failed for %s; leaving as-is",
                            row["post_id"])
                continue

            flag = (verdict.get("status") or "").lower()
            if flag in ("cancelled", "sold_out"):
                new_state = flag
            else:
                continue

        banner = {
            "ended": ENDED_BANNER,
            "cancelled": CANCELLED_BANNER,
            "sold_out": SOLD_OUT_BANNER,
        }[new_state]

        if dry_run:
            log.warning("DRY RUN: would banner %s as %s", row["post_id"], new_state)
            state.mark_published_state(conn, row["post_id"], new_state, banner)
            applied += 1
            continue

        try:
            client = wix.WixClient.from_env(cfg["settings"]["wix"])
            client.prepend_banner(row["post_id"], banner)
        except Exception:
            log.exception("could not banner %s; will retry next run", row["post_id"])
            continue

        state.mark_published_state(conn, row["post_id"], new_state, banner)
        applied += 1
        log.info("%s bannered: %s", row["post_id"], new_state)

    return applied
