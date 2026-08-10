"""Stage 9 — schedule and publish.

Two halves. The Slot Scheduler picks *when*, targeting 5-7 days before the
event; the Wix client does the publishing. The scheduler cannot publish and
the client cannot choose a time, which keeps a bad model call from putting a
piece live immediately.

Events too close to happening are dropped here rather than left in the queue.
Highest-relevance-first ordering would otherwise let a low scorer sit until
its date passed, and an article about a finished event is worse than none.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from dateutil import parser as dateparser

from pipeline import notify, ricos, roles, state, wix
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)


def _details_box(event: dict[str, Any], transit: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the box from verified fields.

    Built here rather than by the writer because a model retyping a date is a
    chance to get the date wrong. `register` is omitted entirely when the event
    has no registration page.
    """
    when = event.get("start_dt")
    if when:
        try:
            parsed = dateparser.parse(when)
            # Built without %-d / %-I: those are glibc-only and this has to
            # run identically on a Windows desktop and a Linux runner.
            hour = parsed.hour % 12 or 12
            when = (
                f"{parsed:%A, %B} {parsed.day}, "
                f"{hour}:{parsed:%M} {parsed:%p}"
            )
        except (ValueError, TypeError):
            pass  # keep the raw string rather than dropping the field

    where = event.get("venue_name") or ""
    if event.get("venue_address"):
        where = f"{where}, {event['venue_address']}".strip(", ")

    box: dict[str, Any] = {
        "when": when,
        "where": where or None,
        "cost": event.get("price"),
        "register": event.get("register_url"),
        "skill_level": event.get("skill_level"),
        "what_to_bring": event.get("what_to_bring"),
    }
    if transit:
        nearest = transit[0]
        routes = ", ".join(nearest.get("routes", [])[:3])
        box["getting_there"] = (
            f"{nearest['stop_name']} ({routes}), {nearest['distance_m']}m away"
        )
    return {k: v for k, v in box.items() if v}


def _too_late(event: dict[str, Any], cfg: dict[str, Any]) -> bool:
    start = event.get("start_dt")
    if not start:
        return False
    try:
        start_dt = dateparser.parse(start)
    except (ValueError, TypeError):
        return False
    floor = cfg["settings"]["scheduling"]["drop_if_lead_days_below"]
    lead = start_dt - datetime.now(start_dt.tzinfo)
    return lead < timedelta(days=floor)


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")

    if _too_late(event, cfg):
        state.update_article(conn, article_id, status="dropped")
        log.info("%s dropped: event too close to publish usefully", article_id)
        return {"ok": True}

    transit = json.loads(event.get("transit_json") or "[]")

    scheduling = cfg["settings"]["scheduling"]
    try:
        slot = roles.run_role("slot_scheduler", {
            "event_start_dt": event.get("start_dt"),
            "venue_neighborhood": event.get("neighborhood"),
            "lead_days_min": scheduling["lead_days_min"],
            "lead_days_max": scheduling["lead_days_max"],
            "timezone": scheduling["timezone"],
            "existing_queue": [
                {"publish_at": r["publish_at"]}
                for r in conn.execute(
                    "SELECT publish_at FROM articles "
                    "WHERE publish_at IS NOT NULL AND status = 'scheduled'"
                ).fetchall()
            ],
        })
    except roles.RoleError as exc:
        raise InfraFailure(f"slot scheduler unavailable: {exc}") from exc

    publish_at = slot.get("publish_at")
    state.update_article(conn, article_id, publish_at=publish_at, status="scheduled")

    if cfg.get("dry_run"):
        log.warning("DRY RUN: would publish %s at %s", article_id, publish_at)
        return {"ok": True}

    settings = cfg["settings"]
    rich_content = ricos.build_rich_content(
        hook_line=draft.get("hook_line", ""),
        details_box=_details_box(event, transit),
        sections=draft.get("sections", []),
        disclaimer=draft.get("disclaimer") or settings["article"]["disclaimer"],
        image_url=event.get("image_url"),
        image_alt=event.get("image_alt"),
    )

    seo_data = {
        "tags": [
            {"type": "title",
             "children": draft.get("meta_title", "") + settings["article"]["meta_title_suffix"]},
            {"type": "meta",
             "props": {"name": "description",
                       "content": draft.get("meta_description", "")}},
        ]
    }

    try:
        client = wix.WixClient.from_env(settings["wix"])
        draft_id = client.create_draft(
            title=draft.get("meta_title", "")[:120],
            rich_content=rich_content,
            seo_data=seo_data,
            category_ids=[settings["wix"]["categories"]["the_guide"]],
            tag_ids=[],
            slug=article.get("slug") or "",
            excerpt=draft.get("meta_description", ""),
        )
        published = client.publish_draft(draft_id)
    except Exception as exc:
        raise InfraFailure(f"Wix publish failed: {exc}") from exc

    post_id = published.get("post", {}).get("id", draft_id)
    url = published.get("post", {}).get("url", "")

    # A 200 from the publish call is not proof the body landed. Confirm the
    # live post actually contains the hook line, re-polling past CDN staleness.
    hook = (draft.get("hook_line") or "")[:40]
    if hook and not client.verify_published(post_id, hook):
        raise InfraFailure(
            f"published {post_id} but the live body never matched; not recording it"
        )

    state.record_published(
        conn, post_id, article_id, event["id"], url,
        article.get("slug") or "", event.get("start_dt"),
    )
    state.update_article(conn, article_id, status="published")
    notify.published(cfg, article_id, draft.get("meta_title", ""), url)

    log.info("%s published: %s", article_id, url or post_id)
    return {"ok": True}
