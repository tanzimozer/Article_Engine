"""Eventbrite API adapter.

Authentication uses a personal OAuth token read from the
``EVENTBRITE_TOKEN`` environment variable -- never from config or source.
When the variable is unset the adapter logs and returns an empty list, so
a pipeline run on a machine without Eventbrite credentials degrades to the
other sources instead of failing.

The token is sent in the ``Authorization`` header rather than as a
``?token=`` query parameter so it never lands in a URL, a log line or an
error message.

A note on endpoints: Eventbrite's public ``/events/search/`` endpoint has
been restricted for some time and many tokens receive 404/403 from it. The
adapter therefore treats an unavailable search endpoint as an expected
condition -- it logs a clear explanation and falls back to
``/organizations/{id}/events/`` when organization ids are configured::

    cfg = {"eventbrite": {"organization_ids": ["123456789"]}}
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .base import (
    RawEvent,
    SourceError,
    build_event,
    clean_text,
    dedupe_events,
    first_present,
    http_get,
    normalize_dt,
    now_pacific,
    strip_html,
    within_window,
)

LOG = logging.getLogger(__name__)

API_ROOT = "https://www.eventbriteapi.com/v3"
TOKEN_ENV_VAR = "EVENTBRITE_TOKEN"

DEFAULT_LOCATION = "Seattle, WA"
DEFAULT_RADIUS = "25mi"
DEFAULT_MAX_PAGES = 5

#: Sub-objects worth requesting inline; saves a request per event.
_EXPANSIONS = "venue,ticket_availability,organizer,category,format"

#: Statuses inspected by the adapter rather than raised by http_get.
_INSPECTED_STATUSES = (200, 400, 401, 403, 404, 409, 410)


# --------------------------------------------------------------------------
# API plumbing
# --------------------------------------------------------------------------


def _utc_stamp(moment: datetime) -> str:
    """Return *moment* formatted the way the Eventbrite API expects (UTC, Z)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_bounds(window_days: int) -> tuple[str, str]:
    """Return ``(range_start, range_end)`` UTC stamps for the collection window.

    Runs from the start of today in Seattle through the end of the
    ``window_days``-th day, matching :func:`base.within_window`.
    """
    start_of_today = now_pacific().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start_of_today + timedelta(days=max(0, window_days) + 1)
    return _utc_stamp(start_of_today), _utc_stamp(end)


def _describe_error(payload: Mapping[str, Any] | None, status: int) -> str:
    """Return a log-safe description of an Eventbrite error response."""
    if not isinstance(payload, Mapping):
        return f"HTTP {status}"
    error = clean_text(payload.get("error")) or ""
    description = clean_text(payload.get("error_description")) or ""
    detail = " ".join(part for part in (error, description) if part)
    return f"HTTP {status} {detail}".strip()


def _api_get(
    path: str, params: Mapping[str, Any], token: str
) -> tuple[int, dict[str, Any] | None]:
    """GET an Eventbrite API path and return ``(status_code, parsed_json)``.

    The token travels in the Authorization header and is never included in
    any returned value or log message. Raises :class:`SourceError` only for
    transport-level failures; HTTP error statuses are returned to the
    caller so it can distinguish "no permission for this endpoint" from
    "the request was wrong".
    """
    url = f"{API_ROOT}{path}"
    response = http_get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        # An authenticated API, not a crawl target; robots.txt does not apply.
        respect_robots=False,
        allow_statuses=_INSPECTED_STATUSES,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = None
    return response.status_code, payload


def _paged_events(
    path: str, params: Mapping[str, Any], token: str, max_pages: int
) -> list[dict[str, Any]]:
    """Return raw Eventbrite event objects from *path*, following pagination.

    Stops at *max_pages*. Raises :class:`SourceError` on auth failures and
    on unexpected error statuses; a 404/410 (endpoint not available to this
    token) is returned to the caller as an empty list after a warning.
    """
    collected: list[dict[str, Any]] = []
    request_params = dict(params)
    page = 1

    while page <= max_pages:
        status, payload = _api_get(path, request_params, token)

        if status in (401, 403):
            raise SourceError(
                f"Eventbrite rejected the {TOKEN_ENV_VAR} credential for {path} "
                f"({_describe_error(payload, status)}). Check the token's scopes."
            )
        if status in (404, 410):
            LOG.warning(
                "Eventbrite endpoint %s is not available to this token (%s). "
                "Eventbrite restricts public event search; configure "
                "eventbrite.organization_ids to use the organization endpoint.",
                path,
                _describe_error(payload, status),
            )
            return collected
        if status != 200 or payload is None:
            raise SourceError(
                f"Eventbrite request to {path} failed: {_describe_error(payload, status)}"
            )

        events = payload.get("events")
        if not isinstance(events, list):
            LOG.warning("Eventbrite response for %s contained no events array", path)
            break
        collected.extend(item for item in events if isinstance(item, dict))

        pagination = payload.get("pagination")
        if not isinstance(pagination, Mapping) or not pagination.get("has_more_items"):
            break
        continuation = pagination.get("continuation")
        if continuation:
            request_params = dict(params)
            request_params["continuation"] = continuation
        else:
            request_params = dict(params)
            request_params["page"] = page + 1
        page += 1

    if page > max_pages:
        LOG.info("Stopped paging %s at the %d page cap", path, max_pages)
    return collected


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


def _event_start(block: Any) -> str | None:
    """Return an ISO Pacific timestamp from an Eventbrite start/end block."""
    if not isinstance(block, Mapping):
        return None
    # "utc" is unambiguous; "local" is wall time in the event's own timezone.
    return normalize_dt(first_present(block.get("utc"), block.get("local")))


def _event_venue(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(venue_name, venue_address)`` for an Eventbrite event."""
    venue = event.get("venue")
    if not isinstance(venue, Mapping):
        if event.get("online_event"):
            return "Online", None
        return None, None
    name = clean_text(venue.get("name"))
    address = venue.get("address")
    if isinstance(address, Mapping):
        display = clean_text(
            first_present(
                address.get("localized_address_display"),
                address.get("localized_multi_line_address_display"),
            )
        )
        if not display:
            parts = [
                clean_text(address.get("address_1")),
                clean_text(address.get("address_2")),
                clean_text(address.get("city")),
                clean_text(address.get("region")),
                clean_text(address.get("postal_code")),
            ]
            display = ", ".join(part for part in parts if part) or None
        return name, display
    return name, None


def _event_price(event: Mapping[str, Any]) -> str | None:
    """Return the published price text for an event, e.g. ``"Free"``, ``"$35"``.

    Uses Eventbrite's own display strings so the value stays verbatim as
    published rather than being re-formatted from raw numbers.
    """
    if event.get("is_free") is True:
        return "Free"

    availability = event.get("ticket_availability")
    if not isinstance(availability, Mapping):
        return None
    if availability.get("is_free") is True:
        return "Free"

    def _display(key: str) -> str | None:
        block = availability.get(key)
        if isinstance(block, Mapping):
            return clean_text(block.get("display"))
        return None

    minimum = _display("minimum_ticket_price")
    maximum = _display("maximum_ticket_price")
    if minimum and maximum and minimum != maximum:
        return f"{minimum} - {maximum}"
    return minimum or maximum


def _event_image(event: Mapping[str, Any]) -> str | None:
    """Return the event's promotional image URL, or ``None``."""
    logo = event.get("logo")
    if not isinstance(logo, Mapping):
        return None
    original = logo.get("original")
    if isinstance(original, Mapping) and original.get("url"):
        return clean_text(original.get("url"))
    return clean_text(logo.get("url"))


def _text_block(value: Any) -> str | None:
    """Return the plain text of an Eventbrite ``{"text":..., "html":...}`` block."""
    if isinstance(value, Mapping):
        return clean_text(value.get("text")) or strip_html(value.get("html"))
    if isinstance(value, str):
        return clean_text(value)
    return None


def _to_event(event: Mapping[str, Any]) -> RawEvent | None:
    """Map an Eventbrite API event onto the RawEvent contract, or ``None``.

    Returns ``None`` for records lacking both a name and a URL, which the
    API occasionally emits for unpublished drafts.
    """
    url = clean_text(event.get("url"))
    title = _text_block(event.get("name"))
    if not url or not title:
        return None

    venue_name, venue_address = _event_venue(event)
    description = first_present(
        _text_block(event.get("summary")), _text_block(event.get("description"))
    )

    return build_event(
        source="eventbrite",
        source_id=clean_text(event.get("id")) or url,
        url=url,
        title=title,
        description=description,
        start_dt=_event_start(event.get("start")),
        end_dt=_event_start(event.get("end")),
        venue_name=venue_name,
        venue_address=venue_address,
        price=_event_price(event),
        register_url=url,  # Eventbrite's event page is the registration page
        image_url=_event_image(event),
        raw={"_parser": "eventbrite-api", "event": dict(event)},
    )


# --------------------------------------------------------------------------
# Adapter entry point
# --------------------------------------------------------------------------


def _search_params(eb_cfg: Mapping[str, Any], window_days: int) -> dict[str, Any]:
    """Build the query parameters for the Eventbrite event search endpoint."""
    range_start, range_end = _window_bounds(window_days)
    params: dict[str, Any] = {
        "location.address": eb_cfg.get("location", DEFAULT_LOCATION),
        "location.within": eb_cfg.get("within", DEFAULT_RADIUS),
        "start_date.range_start": range_start,
        "start_date.range_end": range_end,
        "expand": _EXPANSIONS,
        "sort_by": "date",
    }
    for cfg_key, param_key in (
        ("q", "q"),
        ("categories", "categories"),
        ("subcategories", "subcategories"),
        ("price", "price"),
    ):
        value = eb_cfg.get(cfg_key)
        if value:
            params[param_key] = ",".join(value) if isinstance(value, (list, tuple)) else value
    return params


def fetch(cfg: dict, window_days: int = 10) -> list[dict]:
    """Return raw event dicts for events starting within window_days from now.

    Searches Eventbrite for Seattle-area events in the window. Returns an
    empty list (after logging) when ``EVENTBRITE_TOKEN`` is unset or when
    the account has no access to the search endpoint, so a missing
    credential never breaks a pipeline run. Raises
    :class:`~pipeline.sources.base.SourceError` only when a configured
    credential is actively rejected or the API misbehaves.
    """
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token or not token.strip():
        LOG.warning(
            "%s is not set; skipping the Eventbrite source. Export a personal "
            "OAuth token to enable it.",
            TOKEN_ENV_VAR,
        )
        return []
    token = token.strip()

    eb_cfg = cfg.get("eventbrite", {}) if isinstance(cfg, Mapping) else {}
    if not isinstance(eb_cfg, Mapping):
        eb_cfg = {}
    max_pages = int(eb_cfg.get("max_pages", DEFAULT_MAX_PAGES))
    include_undated = bool(eb_cfg.get("include_undated", False))

    raw_events = _paged_events(
        "/events/search/", _search_params(eb_cfg, window_days), token, max_pages
    )

    org_ids = eb_cfg.get("organization_ids") or eb_cfg.get("organization_id") or []
    if isinstance(org_ids, (str, int)):
        org_ids = [org_ids]
    if org_ids:
        range_start, range_end = _window_bounds(window_days)
        org_params = {
            "status": "live",
            "order_by": "start_asc",
            "start_date.range_start": range_start,
            "start_date.range_end": range_end,
            "expand": _EXPANSIONS,
        }
        for org_id in org_ids:
            LOG.info("Fetching Eventbrite events for organization %s", org_id)
            raw_events.extend(
                _paged_events(
                    f"/organizations/{org_id}/events/", org_params, token, max_pages
                )
            )

    events: list[RawEvent] = []
    for raw_event in raw_events:
        event = _to_event(raw_event)
        if event is None:
            continue
        if within_window(event["start_dt"], window_days, include_undated=include_undated):
            events.append(event)

    events = list(dedupe_events(events))
    LOG.info(
        "eventbrite adapter returning %d of %d event(s) within %d day(s)",
        len(events),
        len(raw_events),
        window_days,
    )
    return events
