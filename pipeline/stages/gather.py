"""Stage 1 — gather.

Fetch, keyword-filter, skip anything already processed, then score what
survives.

Order matters for cost. The fingerprint check runs *before* the relevance
scorer, so an event sitting in the 7-10 day window across five consecutive
runs is scored once, not five times.

A source going down does not stop the run. Coverage is a union of several
free sources precisely so no single one is load-bearing.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from dateutil import parser as dateparser

from pipeline import notify, roles, state
from pipeline.sources import eventbrite, rss, scrape

log = logging.getLogger(__name__)


def _within_window(start_dt: str | None, min_days: int, max_days: int) -> bool:
    """True when the event starts inside the useful lead window."""
    if not start_dt:
        return False
    try:
        start = dateparser.parse(start_dt)
    except (ValueError, TypeError):
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
    delta = start - datetime.now(start.tzinfo)
    return timedelta(days=0) <= delta <= timedelta(days=max_days)


@lru_cache(maxsize=None)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Whole-word matcher for one keyword.

    Substring matching made "run" fire on "brunch" and "runs through
    November", "ride" on "bride" and "race" on "bracelet" -- a third of a
    general city calendar reached the scorer on words like those. Generous
    is the point, but generous about *words*, not about letters.

    Uses lookarounds rather than ``\\b`` so multi-word terms ("half
    marathon") and digit-leading terms ("5k") behave the same way.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def _keyword_match(raw: dict[str, Any], keywords: dict[str, Any]) -> bool:
    """Generous pre-filter.

    A false positive costs one scoring call. A false negative loses the article
    entirely, so this errs toward letting things through.
    """
    haystack = f"{raw.get('title', '')} {raw.get('description', '')}".lower()

    for term in keywords.get("exclude", []):
        if _term_pattern(str(term).lower()).search(haystack):
            return False

    for group in ("fitness", "wellness", "lifestyle"):
        for term in keywords.get(group, []):
            if _term_pattern(str(term).lower()).search(haystack):
                return True
    return False


def _fetch_all(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch from every enabled source. Returns (events, failed_source_names)."""
    sources = cfg["sources"]
    window = cfg["settings"]["pipeline"]["event_window_days"]
    collected: list[dict[str, Any]] = []
    failed: list[str] = []

    adapters = [
        ("eventbrite", eventbrite, sources.get("eventbrite", {})),
        ("rss", rss, sources.get("rss", {})),
        ("scrape", scrape, sources.get("scrape", {})),
    ]

    for name, module, sub_cfg in adapters:
        if not sub_cfg.get("enabled"):
            continue
        try:
            found = module.fetch(sub_cfg, window)
            log.info("source %s returned %d event(s)", name, len(found))
            collected.extend(found)
        except Exception as exc:
            log.exception("source %s failed", name)
            failed.append(f"{name}: {exc}")

    return collected, failed


def _priority_for(source: str, cfg: dict[str, Any]) -> int:
    """Position in the conflict-resolution ranking. Lower wins."""
    order = cfg["sources"].get("priority_order", [])
    try:
        return order.index(source)
    except ValueError:
        return len(order)


def run(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Populate the candidates table. Returns how many events qualified."""
    settings = cfg["settings"]
    keywords = cfg["keywords"]
    min_days = settings["pipeline"]["event_window_min_days"]
    max_days = settings["pipeline"]["event_window_days"]
    pass_avg = settings["thresholds"]["relevance_pass_average"]

    raw_events, failed = _fetch_all(cfg)
    if failed:
        notify.source_failures(cfg, failed)

    qualified = 0
    skipped_seen = skipped_keyword = skipped_window = skipped_score = 0

    for raw in raw_events:
        if not _within_window(raw.get("start_dt"), min_days, max_days):
            skipped_window += 1
            continue

        if not _keyword_match(raw, keywords):
            skipped_keyword += 1
            continue

        new_hash = state.content_hash(
            raw.get("start_dt"), raw.get("venue_name"), raw.get("price")
        )
        if state.seen_before(conn, raw["source"], raw["source_id"], new_hash):
            skipped_seen += 1
            continue

        try:
            scored = roles.run_role("relevance_scorer", raw)
        except roles.RoleError:
            log.exception("relevance scoring failed for %s", raw.get("title"))
            continue

        average = float(scored.get("average", 0.0))
        state.record_fingerprint(conn, raw["source"], raw["source_id"], new_hash)

        if average < pass_avg or not scored.get("pass"):
            skipped_score += 1
            log.debug("below threshold (%.1f): %s", average, raw.get("title"))
            continue

        state.add_candidate(conn, raw, _priority_for(raw["source"], cfg),
                            scored, average)
        qualified += 1

    log.info(
        "gather: %d raw, %d qualified "
        "(skipped: %d out-of-window, %d no-keyword, %d already-seen, %d low-score)",
        len(raw_events), qualified,
        skipped_window, skipped_keyword, skipped_seen, skipped_score,
    )
    return qualified
