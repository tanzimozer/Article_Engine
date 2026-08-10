"""Generic RSS/Atom adapter for event feeds.

Driven entirely by a list of feed URLs in config, so adding a feed never
requires code changes::

    cfg = {
        "rss": {
            "feeds": [
                "https://everout.com/seattle/events/feed/",
                {"url": "https://example.org/events.xml", "source": "example"},
            ],
        }
    }

Bytes are fetched through :func:`pipeline.sources.base.http_get` (so feeds
get the same User-Agent, retries, robots and rate limiting as everything
else) and handed to ``feedparser`` for parsing.

Event feeds vary wildly in how they express dates. This adapter looks for
the event-specific extensions first (``ev:startdate``, xCal ``dtstart``,
Google Calendar fields) and only then considers the entry's publication
date -- and even that only when ``use_published_as_start`` is enabled,
because "when the listing was posted" is not "when the event happens".
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

try:
    import feedparser
except ImportError as exc:  # pragma: no cover - dependency is declared
    raise ImportError(
        "The rss adapter requires feedparser (pip install feedparser)"
    ) from exc

from .base import (
    RawEvent,
    SourceError,
    absolute_url,
    build_event,
    clean_text,
    dedupe_events,
    first_present,
    http_get,
    normalize_dt,
    strip_html,
    within_window,
)

LOG = logging.getLogger(__name__)

#: Entry keys checked for an event start, most authoritative first.
_START_KEYS: tuple[str, ...] = (
    "ev_startdate",
    "xcal_dtstart",
    "dtstart",
    "start_date",
    "start_time",
    "startdate",
    "gd_when",
    "event_start",
)
#: Entry keys checked for an event end.
_END_KEYS: tuple[str, ...] = (
    "ev_enddate",
    "xcal_dtend",
    "dtend",
    "end_date",
    "end_time",
    "enddate",
    "event_end",
)
_VENUE_KEYS: tuple[str, ...] = (
    "ev_location",
    "xcal_location",
    "location",
    "venue",
    "event_venue",
)
_ADDRESS_KEYS: tuple[str, ...] = (
    "xcal_x-calconnect-street",
    "address",
    "event_address",
    "street",
)
_PRICE_KEYS: tuple[str, ...] = ("ev_cost", "cost", "price", "admission", "ticket_price")
_REGISTER_KEYS: tuple[str, ...] = (
    "registration_url",
    "ticket_url",
    "tickets",
    "ev_url",
    "xcal_url",
)

_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Return *value* converted to JSON-serialisable primitives.

    feedparser hands back ``time.struct_time`` objects and its own dict
    subclass; both are flattened so ``raw`` can be stored or serialised.
    """
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, time.struct_time):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", value)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in value]
    return str(value)


def _first_key(entry: Mapping[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    """Return the first ``(key, value)`` of *keys* present and non-empty in *entry*."""
    for key in keys:
        if key not in entry:
            continue
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return key, value
    return None, None


def _entry_link(entry: Mapping[str, Any], feed_url: str) -> str | None:
    """Return the entry's canonical absolute URL, or ``None``."""
    link = clean_text(entry.get("link"))
    if not link:
        for candidate in entry.get("links") or []:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("rel") in (None, "alternate") and candidate.get("href"):
                link = clean_text(candidate.get("href"))
                break
    return absolute_url(feed_url, link) if link else None


def _entry_description(entry: Mapping[str, Any]) -> str | None:
    """Return the entry body as plain text, preferring full content over summary."""
    contents = entry.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, Mapping) and item.get("value"):
                text = strip_html(item.get("value"))
                if text:
                    return text
    return strip_html(
        first_present(entry.get("summary"), entry.get("subtitle"), entry.get("description"))
    )


def _entry_image(entry: Mapping[str, Any], feed_url: str) -> str | None:
    """Return an absolute image URL for the entry, or ``None``.

    Checks Media RSS, enclosures, then the first ``<img>`` in the body.
    """
    for key in ("media_thumbnail", "media_content"):
        media = entry.get(key)
        if isinstance(media, list):
            for item in media:
                if isinstance(item, Mapping) and item.get("url"):
                    return absolute_url(feed_url, str(item["url"]))

    for enclosure in entry.get("enclosures") or []:
        if not isinstance(enclosure, Mapping):
            continue
        mime = str(enclosure.get("type") or "")
        if mime.startswith("image/") and enclosure.get("href"):
            return absolute_url(feed_url, str(enclosure["href"]))

    for key in ("summary", "description"):
        body = entry.get(key)
        if isinstance(body, str):
            match = _IMG_SRC_RE.search(body)
            if match:
                return absolute_url(feed_url, match.group(1))

    contents = entry.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, Mapping) and isinstance(item.get("value"), str):
                match = _IMG_SRC_RE.search(item["value"])
                if match:
                    return absolute_url(feed_url, match.group(1))
    return None


def _source_name(feed_cfg: Mapping[str, Any], feed_url: str, feed_meta: Mapping[str, Any]) -> str:
    """Return the source label for a feed.

    Uses the configured ``source``, else the feed's host ("everout.com"
    becomes "everout"), else the feed title.
    """
    configured = clean_text(feed_cfg.get("source") or feed_cfg.get("name"))
    if configured:
        return configured
    host = (urlparse(feed_url).netloc or "").lower().removeprefix("www.")
    if host:
        return host.split(".")[0] or host
    return clean_text(feed_meta.get("title")) or "rss"


def _entry_to_event(
    entry: Mapping[str, Any],
    *,
    source: str,
    feed_url: str,
    feed_meta: Mapping[str, Any],
    use_published_as_start: bool,
) -> RawEvent | None:
    """Map one feed entry onto the RawEvent contract, or ``None`` if unusable.

    Everything the mapping did not consume is preserved under
    ``raw["_unmapped"]``, with the whole entry kept under ``raw["entry"]``.
    """
    link = _entry_link(entry, feed_url)
    title = clean_text(entry.get("title"))
    if not link or not title:
        LOG.debug("Skipping feed entry without a link or title in %s", feed_url)
        return None

    consumed: set[str] = {"title", "link", "links", "id", "guid", "summary", "subtitle",
                          "description", "content", "enclosures", "media_thumbnail",
                          "media_content"}

    start_key, start_value = _first_key(entry, _START_KEYS)
    end_key, end_value = _first_key(entry, _END_KEYS)
    venue_key, venue_value = _first_key(entry, _VENUE_KEYS)
    address_key, address_value = _first_key(entry, _ADDRESS_KEYS)
    price_key, price_value = _first_key(entry, _PRICE_KEYS)
    register_key, register_value = _first_key(entry, _REGISTER_KEYS)
    for key in (start_key, end_key, venue_key, address_key, price_key, register_key):
        if key:
            consumed.add(key)

    start_dt = normalize_dt(start_value) if start_value is not None else None
    if start_dt is None and use_published_as_start:
        for key in ("published_parsed", "updated_parsed", "published", "updated"):
            if entry.get(key):
                start_dt = normalize_dt(entry.get(key))
                if start_dt:
                    consumed.add(key)
                    LOG.debug("Using %s as the start date for %s", key, link)
                    break

    entry_payload = _jsonable(entry)
    unmapped = {
        key: value
        for key, value in entry_payload.items()
        if key not in consumed and not key.endswith("_parsed")
    }

    return build_event(
        source=source,
        source_id=clean_text(first_present(entry.get("id"), entry.get("guid"))) or link,
        url=link,
        title=title,
        description=_entry_description(entry),
        start_dt=start_dt,
        end_dt=normalize_dt(end_value) if end_value is not None else None,
        venue_name=clean_text(venue_value) if venue_value is not None else None,
        venue_address=clean_text(address_value) if address_value is not None else None,
        price=clean_text(price_value) if price_value is not None else None,
        register_url=absolute_url(feed_url, clean_text(register_value))
        if register_value is not None
        else None,
        image_url=_entry_image(entry, feed_url),
        raw={
            "_parser": "rss",
            "_feed_url": feed_url,
            "feed": {
                "title": clean_text(feed_meta.get("title")),
                "link": clean_text(feed_meta.get("link")),
            },
            "entry": entry_payload,
            "_unmapped": unmapped,
        },
    )


def _iter_feed_configs(cfg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the normalised list of feed configs from the pipeline config.

    Accepts a bare list of URL strings, a list of dicts with a ``url``
    key, and looks under ``cfg["rss"]["feeds"]``, ``cfg["feeds"]`` or
    ``cfg["rss_feeds"]``.
    """
    rss_cfg = cfg.get("rss") if isinstance(cfg, Mapping) else None
    container = rss_cfg if isinstance(rss_cfg, Mapping) else cfg
    feeds = None
    for key in ("feeds", "rss_feeds", "urls"):
        if isinstance(container, Mapping) and container.get(key):
            feeds = container[key]
            break
    if not feeds:
        return []
    if isinstance(feeds, str):
        feeds = [feeds]

    normalised: list[Mapping[str, Any]] = []
    for entry in feeds:
        if isinstance(entry, str):
            normalised.append({"url": entry})
        elif isinstance(entry, Mapping) and (entry.get("url") or entry.get("feed_url")):
            feed_cfg = dict(entry)
            feed_cfg.setdefault("url", feed_cfg.pop("feed_url", None))
            normalised.append(feed_cfg)
        else:
            LOG.warning("Skipping unrecognised RSS feed entry: %r", entry)
    return normalised


def _fetch_one_feed(
    feed_cfg: Mapping[str, Any], window_days: int, default_use_published: bool
) -> list[RawEvent]:
    """Fetch and parse one feed, returning its in-window events.

    Raises :class:`SourceError` if the feed cannot be fetched or contains
    no entries at all.
    """
    feed_url = str(feed_cfg["url"])
    response = http_get(feed_url)
    parsed = feedparser.parse(response.content)

    if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
        LOG.warning("Feed %s is not well-formed: %s", feed_url, parsed.bozo_exception)
    entries = getattr(parsed, "entries", []) or []
    if not entries:
        raise SourceError(f"Feed {feed_url} returned no entries")

    feed_meta = getattr(parsed, "feed", {}) or {}
    source = _source_name(feed_cfg, feed_url, feed_meta)
    include_undated = bool(feed_cfg.get("include_undated", True))
    use_published = bool(feed_cfg.get("use_published_as_start", default_use_published))

    events: list[RawEvent] = []
    for entry in entries:
        event = _entry_to_event(
            entry,
            source=source,
            feed_url=feed_url,
            feed_meta=feed_meta,
            use_published_as_start=use_published,
        )
        if event is None:
            continue
        if within_window(event["start_dt"], window_days, include_undated=include_undated):
            events.append(event)

    LOG.info(
        "%s: %d entry/entries in %s, kept %d within %d day(s)",
        source,
        len(entries),
        feed_url,
        len(events),
        window_days,
    )
    return events


def fetch(cfg: dict, window_days: int = 10) -> list[dict]:
    """Return raw event dicts for events starting within window_days from now.

    Reads every feed listed in config. A single failing feed is logged and
    skipped (feeds are independent and individually replaceable), but if
    every configured feed fails a :class:`SourceError` is raised so the
    caller does not mistake a total outage for a quiet week.

    Entries with no discoverable event date are returned with
    ``start_dt=None`` so a later enrichment step can resolve them; set
    ``include_undated: false`` on a feed to drop them instead.
    """
    feed_configs = _iter_feed_configs(cfg)
    if not feed_configs:
        LOG.warning("rss adapter: no feeds configured, nothing to do")
        return []

    rss_cfg = cfg.get("rss") if isinstance(cfg, Mapping) else None
    default_use_published = bool(
        rss_cfg.get("use_published_as_start", False) if isinstance(rss_cfg, Mapping) else False
    )

    events: list[RawEvent] = []
    failures: list[str] = []
    for feed_cfg in feed_configs:
        if feed_cfg.get("enabled") is False:
            LOG.info("Skipping disabled feed %s", feed_cfg.get("url"))
            continue
        try:
            events.extend(_fetch_one_feed(feed_cfg, window_days, default_use_published))
        except SourceError as exc:
            failures.append(f"{feed_cfg.get('url')}: {exc}")
            LOG.error("Feed failed, continuing: %s", exc)
        except Exception as exc:  # malformed feed payloads should not kill the run
            failures.append(f"{feed_cfg.get('url')}: {type(exc).__name__}: {exc}")
            LOG.exception("Unexpected error reading feed %s", feed_cfg.get("url"))

    if failures and not events:
        raise SourceError(
            "Every configured RSS feed failed: " + "; ".join(failures)
        )

    LOG.info("rss adapter returning %d event(s)", len(events))
    return list(dedupe_events(events))
