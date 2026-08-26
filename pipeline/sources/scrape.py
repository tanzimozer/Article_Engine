"""HTML scrapers for Seattle event calendars.

Strategy, in order of preference:

1. **JSON-LD** -- most event calendars emit ``schema.org/Event`` blocks in
   ``<script type="application/ld+json">``. That is structured, stable and
   authoritative, so it is always tried first.
2. **CSS selectors** -- a per-site fallback for pages without JSON-LD.
3. **Detail pages** -- when a listing card is missing a start time, the
   event's own page is fetched (within a budget) and re-checked for JSON-LD.

Adding a site is one function plus one registry entry::

    def _scrape_example(site_cfg, window_days):
        return _scrape_with_spec(_EXAMPLE, site_cfg, window_days)

    SCRAPERS["example"] = _scrape_example

The CSS selector lists are best-effort: site markup changes without notice,
so every field of a :class:`SiteSpec` can be overridden from config without
touching this file. A scraper that cannot find *any* candidate events raises
:class:`~pipeline.sources.base.SourceError` rather than silently returning an
empty list, because "the selectors broke" and "nothing is on this week" must
not look alike to the caller.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from bs4 import BeautifulSoup
from bs4.element import Tag

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
    parse_dt_range,
    set_polite_delay,
    strip_html,
    within_window,
)

LOG = logging.getLogger(__name__)

#: Parsed with the stdlib parser so lxml is not a hard dependency.
_HTML_PARSER = "html.parser"

#: schema.org types that are events. Matched by substring so subtypes such as
#: MusicEvent/TheaterEvent/ScreeningEvent are picked up automatically.
_NON_EVENT_TYPES = frozenset({"eventvenue", "eventreservation", "eventseries"})

#: How many event detail pages a single site may fetch per run.
DEFAULT_MAX_DETAIL_PAGES = 12


# --------------------------------------------------------------------------
# JSON-LD extraction
# --------------------------------------------------------------------------


def _coerce_str(value: Any) -> str | None:
    """Flatten a JSON-LD scalar/list/node into a plain string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            found = _coerce_str(item)
            if found:
                return found
        return None
    if isinstance(value, Mapping):
        for key in ("name", "url", "@id", "text", "contentUrl"):
            found = _coerce_str(value.get(key))
            if found:
                return found
    return None


def _type_names(node: Mapping[str, Any]) -> list[str]:
    """Return the lowercased ``@type`` values of a JSON-LD *node*."""
    raw_type = node.get("@type") or node.get("type")
    if isinstance(raw_type, str):
        return [raw_type.lower()]
    if isinstance(raw_type, list):
        return [str(item).lower() for item in raw_type if item]
    return []


def _is_event_node(node: Any) -> bool:
    """Return whether *node* is a schema.org Event (excluding EventVenue etc.)."""
    if not isinstance(node, Mapping):
        return False
    names = _type_names(node)
    return any("event" in name and name not in _NON_EVENT_TYPES for name in names)


def _walk_jsonld(payload: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Yield every mapping inside a JSON-LD payload, unwrapping the usual nesting.

    Handles top-level arrays, ``@graph``, ``itemListElement`` and ``ListItem``
    wrappers, which is how calendars normally wrap a page's worth of events.
    """
    if depth > 6:
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_jsonld(item, depth + 1)
        return
    if not isinstance(payload, Mapping):
        return

    yield payload
    for key in ("@graph", "itemListElement", "item", "subEvent", "events"):
        nested = payload.get(key)
        if nested is not None:
            yield from _walk_jsonld(nested, depth + 1)


def extract_jsonld_events(soup: BeautifulSoup) -> list[Mapping[str, Any]]:
    """Return every schema.org Event node found in *soup*'s JSON-LD blocks.

    Malformed blocks are logged and skipped; one bad script tag never
    invalidates the rest of the page.
    """
    events: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for index, script in enumerate(soup.find_all("script", type="application/ld+json")):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.debug("Skipping unparseable JSON-LD block #%d: %s", index, exc)
            continue
        for node in _walk_jsonld(payload):
            if _is_event_node(node) and id(node) not in seen:
                seen.add(id(node))
                events.append(node)
    return events


def _jsonld_location(node: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(venue_name, venue_address)`` from an Event's ``location``."""
    location = node.get("location")
    if isinstance(location, list):
        location = next((item for item in location if item), None)
    if location is None:
        return None, None
    if isinstance(location, str):
        return clean_text(location), None
    if not isinstance(location, Mapping):
        return None, None

    name = _coerce_str(location.get("name"))
    if "virtuallocation" in " ".join(_type_names(location)):
        return name or "Online", _coerce_str(location.get("url"))

    address = location.get("address")
    if isinstance(address, str):
        return name, clean_text(address)
    if isinstance(address, list):
        address = next((item for item in address if isinstance(item, Mapping)), None)
    if isinstance(address, Mapping):
        parts = [
            _coerce_str(address.get("streetAddress")),
            _coerce_str(address.get("addressLocality")),
            _coerce_str(address.get("addressRegion")),
            _coerce_str(address.get("postalCode")),
        ]
        joined = ", ".join(part for part in parts if part)
        # "Seattle, WA, 98101" reads better as "Seattle, WA 98101".
        postal = _coerce_str(address.get("postalCode"))
        region = _coerce_str(address.get("addressRegion"))
        if postal and region and f"{region}, {postal}" in joined:
            joined = joined.replace(f"{region}, {postal}", f"{region} {postal}")
        return name, clean_text(joined)
    return name, None


def _format_amount(value: Any, currency: str | None) -> str | None:
    """Render a JSON-LD price as published-looking text, e.g. ``"$35"``."""
    text = _coerce_str(value)
    if text is None:
        return None
    try:
        amount = float(text)
    except ValueError:
        return text  # already a display string such as "Donation"
    if amount == 0:
        return "Free"
    rendered = f"{amount:.2f}".rstrip("0").rstrip(".")
    symbol = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    return f"{symbol}{rendered}"


def _jsonld_price(node: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(price_text, register_url)`` from an Event's ``offers``."""
    if node.get("isAccessibleForFree") in (True, "true", "True"):
        return "Free", _coerce_str(node.get("url"))

    offers = node.get("offers")
    if isinstance(offers, Mapping):
        offers = [offers]
    if not isinstance(offers, list):
        return None, None

    register_url: str | None = None
    prices: list[str] = []
    for offer in offers:
        if not isinstance(offer, Mapping):
            continue
        register_url = register_url or _coerce_str(offer.get("url"))
        currency = _coerce_str(offer.get("priceCurrency"))
        low = _format_amount(offer.get("lowPrice"), currency)
        high = _format_amount(offer.get("highPrice"), currency)
        if low and high and low != high:
            prices.append(f"{low} - {high}")
            continue
        single = _format_amount(
            first_present(
                offer.get("price"),
                (offer.get("priceSpecification") or {}).get("price")
                if isinstance(offer.get("priceSpecification"), Mapping)
                else None,
                offer.get("lowPrice"),
            ),
            currency,
        )
        if single:
            prices.append(single)

    unique = list(dict.fromkeys(price for price in prices if price))
    if not unique:
        return None, register_url
    if len(unique) == 1:
        return unique[0], register_url
    if "Free" in unique:
        others = [price for price in unique if price != "Free"]
        return f"Free - {others[-1]}" if others else "Free", register_url
    return f"{unique[0]} - {unique[-1]}", register_url


def _jsonld_image(node: Mapping[str, Any], page_url: str) -> str | None:
    """Return the Event's absolute image URL, or ``None``."""
    candidate = _coerce_str(first_present(node.get("image"), node.get("photo")))
    return absolute_url(page_url, candidate) if candidate else None


def jsonld_to_event(
    node: Mapping[str, Any], *, source: str, page_url: str
) -> RawEvent | None:
    """Map a schema.org Event node onto the RawEvent contract.

    Returns ``None`` when the node has neither a title nor a usable URL,
    which is the sign of a stub or breadcrumb rather than a real event.
    """
    title = _coerce_str(node.get("name"))
    event_url = absolute_url(
        page_url, _coerce_str(first_present(node.get("url"), node.get("@id")))
    ) or page_url
    if not title:
        return None

    venue_name, venue_address = _jsonld_location(node)
    price, offer_url = _jsonld_price(node)
    description = strip_html(
        first_present(node.get("description"), node.get("disambiguatingDescription"))
    )

    return build_event(
        source=source,
        source_id=_coerce_str(first_present(node.get("@id"), node.get("identifier")))
        or event_url,
        url=event_url,
        title=title,
        description=description,
        start_dt=normalize_dt(node.get("startDate")),
        end_dt=normalize_dt(node.get("endDate")),
        venue_name=venue_name,
        venue_address=venue_address,
        price=price,
        register_url=absolute_url(page_url, offer_url) if offer_url else None,
        image_url=_jsonld_image(node, page_url),
        raw={"_parser": "json-ld", "_page_url": page_url, "jsonld": dict(node)},
    )


# --------------------------------------------------------------------------
# CSS selector fallback
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteSpec:
    """Everything needed to scrape one event calendar.

    Selector fields are ordered candidate lists: the first selector that
    matches wins, so a site can be given several generations of markup.
    """

    source: str
    listing_url: str
    card_selectors: tuple[str, ...] = ()
    title_selectors: tuple[str, ...] = ("h1", "h2", "h3", ".title", "[itemprop=name]")
    link_selectors: tuple[str, ...] = ("a[href]",)
    date_selectors: tuple[str, ...] = ("time[datetime]", "time", ".date", ".datetime")
    description_selectors: tuple[str, ...] = (".description", ".summary", "p")
    venue_selectors: tuple[str, ...] = (".venue", ".location", ".place")
    address_selectors: tuple[str, ...] = (".address", "[itemprop=address]")
    price_selectors: tuple[str, ...] = (".price", ".cost", ".ticket-price")
    image_selectors: tuple[str, ...] = ("img[src]", "img[data-src]")
    follow_detail: bool = True
    max_detail_pages: int = DEFAULT_MAX_DETAIL_PAGES


#: config key -> SiteSpec field, for the names config uses natively.
_FIELD_ALIASES = {
    "url": "listing_url",
    "name": "source",
    "source": "source",
}

_OVERRIDABLE_FIELDS = frozenset(
    {
        "source",
        "listing_url",
        "card_selectors",
        "title_selectors",
        "link_selectors",
        "date_selectors",
        "description_selectors",
        "venue_selectors",
        "address_selectors",
        "price_selectors",
        "image_selectors",
        "follow_detail",
        "max_detail_pages",
    }
)


def _apply_overrides(spec: SiteSpec, site_cfg: Mapping[str, Any]) -> SiteSpec:
    """Return *spec* with any recognised keys from *site_cfg* applied.

    Accepts both the SiteSpec field names and the shorter names used in
    ``config/sources.yaml`` (``url`` for the listing page, ``name`` for
    the source label).
    """
    overrides: dict[str, Any] = {}
    for key, value in site_cfg.items():
        key = _FIELD_ALIASES.get(key, key)
        if key not in _OVERRIDABLE_FIELDS or value is None:
            continue
        if key.endswith("_selectors"):
            if isinstance(value, str):
                value = (value,)
            else:
                value = tuple(str(item) for item in value)
        overrides[key] = value
    return replace(spec, **overrides) if overrides else spec


def _select_one(scope: Tag, selectors: Sequence[str]) -> Tag | None:
    """Return the first element in *scope* matching any of *selectors*."""
    for selector in selectors:
        try:
            found = scope.select_one(selector)
        except (ValueError, NotImplementedError):  # malformed selector from config
            LOG.debug("Ignoring invalid CSS selector %r", selector)
            continue
        if found is not None:
            return found
    return None


def _select_text(scope: Tag, selectors: Sequence[str]) -> str | None:
    """Return the text of the first element matching *selectors*, or ``None``."""
    found = _select_one(scope, selectors)
    return clean_text(found.get_text(" ")) if found is not None else None


def _select_date_text(scope: Tag, selectors: Sequence[str]) -> str | None:
    """Return a date string, preferring a ``<time datetime=...>`` attribute."""
    found = _select_one(scope, selectors)
    if found is None:
        return None
    for attr in ("datetime", "content", "data-date", "data-start"):
        value = clean_text(found.get(attr))
        if value:
            return value
    return clean_text(found.get_text(" "))


def _select_url(scope: Tag, selectors: Sequence[str], page_url: str) -> str | None:
    """Return the first absolute href matching *selectors*, or ``None``."""
    for selector in selectors:
        try:
            candidates = scope.select(selector)
        except (ValueError, NotImplementedError):
            continue
        for candidate in candidates:
            resolved = absolute_url(page_url, candidate.get("href"))
            if resolved:
                return resolved
    return None


def _select_image(scope: Tag, selectors: Sequence[str], page_url: str) -> str | None:
    """Return the first absolute image URL matching *selectors*, or ``None``."""
    for selector in selectors:
        try:
            candidates = scope.select(selector)
        except (ValueError, NotImplementedError):
            continue
        for candidate in candidates:
            for attr in ("src", "data-src", "data-lazy-src", "content"):
                resolved = absolute_url(page_url, candidate.get(attr))
                if resolved:
                    return resolved
    return None


def _card_to_event(card: Tag, spec: SiteSpec, page_url: str) -> RawEvent | None:
    """Map one listing card onto the RawEvent contract, or ``None`` if unusable."""
    event_url = _select_url(card, spec.link_selectors, page_url)
    title = _select_text(card, spec.title_selectors)
    if not title and event_url:
        anchor = _select_one(card, spec.link_selectors)
        title = clean_text(anchor.get("title")) if anchor is not None else None
    if not title or not event_url:
        return None

    date_text = _select_date_text(card, spec.date_selectors)
    start_dt, end_dt = parse_dt_range(date_text)
    venue = _select_text(card, spec.venue_selectors)
    address = _select_text(card, spec.address_selectors)
    price = _select_text(card, spec.price_selectors)
    description = _select_text(card, spec.description_selectors)

    return build_event(
        source=spec.source,
        source_id=clean_text(card.get("data-event-id") or card.get("id")) or event_url,
        url=event_url,
        title=title,
        description=description,
        start_dt=start_dt,
        end_dt=end_dt,
        venue_name=venue,
        venue_address=address,
        price=price,
        register_url=None,
        image_url=_select_image(card, spec.image_selectors, page_url),
        raw={
            "_parser": "css",
            "_page_url": page_url,
            "fields": {
                "date_text": date_text,
                "venue": venue,
                "address": address,
                "price": price,
            },
            "_html": str(card)[:4000],
        },
    )


# --------------------------------------------------------------------------
# Scrape driver
# --------------------------------------------------------------------------


def _get_soup(url: str) -> BeautifulSoup:
    """Fetch *url* through the shared HTTP layer and return parsed HTML.

    Raises :class:`SourceError` when the page cannot be fetched or parsed.
    """
    response = http_get(url)
    try:
        return BeautifulSoup(response.text, _HTML_PARSER)
    except Exception as exc:  # pragma: no cover - html.parser is very tolerant
        raise SourceError(f"Could not parse HTML from {url}: {exc}") from exc


def _merge_event(primary: RawEvent, fallback: RawEvent) -> RawEvent:
    """Fill empty fields of *primary* from *fallback*, keeping *primary*'s url."""
    merged: RawEvent = dict(primary)  # type: ignore[assignment]
    for key, value in fallback.items():
        if key in ("raw", "url", "source", "source_id"):
            continue
        if not merged.get(key):
            merged[key] = value  # type: ignore[literal-required]
    merged["raw"] = {**fallback.get("raw", {}), **primary.get("raw", {})}
    return merged


def _enrich_from_detail(event: RawEvent, spec: SiteSpec) -> RawEvent:
    """Return *event* topped up from its detail page's JSON-LD, if available.

    Network failures are logged and swallowed: a listing card that already
    parsed is still worth keeping.
    """
    try:
        soup = _get_soup(event["url"])
    except SourceError as exc:
        LOG.debug("Detail fetch failed for %s: %s", event["url"], exc)
        return event

    for node in extract_jsonld_events(soup):
        detailed = jsonld_to_event(node, source=spec.source, page_url=event["url"])
        if detailed is not None:
            return _merge_event(event, detailed)

    meta = soup.find("meta", property="og:description")
    if meta is not None and not event["description"]:
        event["description"] = clean_text(meta.get("content")) or ""
    return event


def _scrape_with_spec(
    spec: SiteSpec, site_cfg: Mapping[str, Any], window_days: int
) -> list[RawEvent]:
    """Scrape one site and return its in-window events.

    Tries JSON-LD, then CSS cards, then detail pages for cards missing a
    start time. Raises :class:`SourceError` when the page yields no event
    candidates at all, which means the markup changed rather than that the
    calendar is empty.
    """
    spec = _apply_overrides(spec, site_cfg)
    include_undated = bool(site_cfg.get("include_undated", False))
    soup = _get_soup(spec.listing_url)

    candidates: list[RawEvent] = []
    for node in extract_jsonld_events(soup):
        event = jsonld_to_event(node, source=spec.source, page_url=spec.listing_url)
        if event is not None:
            candidates.append(event)
    parser_used = "json-ld"

    if not candidates:
        parser_used = "css"
        cards: list[Tag] = []
        for selector in spec.card_selectors:
            try:
                cards = soup.select(selector)
            except (ValueError, NotImplementedError):
                LOG.debug("Ignoring invalid card selector %r for %s", selector, spec.source)
                continue
            if cards:
                LOG.debug(
                    "%s: %d card(s) matched selector %r", spec.source, len(cards), selector
                )
                break
        for card in cards:
            event = _card_to_event(card, spec, spec.listing_url)
            if event is not None:
                candidates.append(event)

    if not candidates:
        raise SourceError(
            f"{spec.source}: no events found at {spec.listing_url} "
            "(no JSON-LD Event blocks and no cards matched the configured selectors)"
        )

    candidates = dedupe_events(candidates)

    if spec.follow_detail:
        budget = max(0, int(spec.max_detail_pages))
        for index, event in enumerate(candidates):
            if budget <= 0:
                break
            if event["start_dt"] or not event["url"]:
                continue
            candidates[index] = _enrich_from_detail(event, spec)
            budget -= 1

    kept = [
        event
        for event in candidates
        if within_window(event["start_dt"], window_days, include_undated=include_undated)
    ]
    LOG.info(
        "%s: parsed %d candidate(s) via %s, kept %d within %d day(s)",
        spec.source,
        len(candidates),
        parser_used,
        len(kept),
        window_days,
    )
    return kept


# --------------------------------------------------------------------------
# Site definitions
# --------------------------------------------------------------------------

_VISIT_SEATTLE = SiteSpec(
    source="visitseattle",
    listing_url="https://visitseattle.org/events/",
    card_selectors=(
        ".search-result-preview",
        ".event-item",
        "article.event",
        ".events-list .item",
        "article",
    ),
    title_selectors=("h3 a", "h2 a", "h3", "h2", ".title"),
    date_selectors=("time[datetime]", ".event-date", ".date", "time"),
    venue_selectors=(".venue", ".event-venue", ".location"),
    price_selectors=(".price", ".cost"),
)

_SEATTLE_GOV = SiteSpec(
    source="seattlegov",
    # Seattle.gov runs on Drupal, hence the views-row markup in the fallback.
    listing_url="https://www.seattle.gov/event-calendar",
    card_selectors=(
        ".views-row",
        ".event-listing",
        ".calendar-item",
        "article.node--type-event",
        "article",
    ),
    title_selectors=(".field--name-title a", "h3 a", "h2 a", "h3", "h2"),
    date_selectors=(
        "time[datetime]",
        ".field--name-field-date",
        ".date-display-single",
        ".date",
    ),
    venue_selectors=(".field--name-field-location", ".venue", ".location"),
    address_selectors=(".field--name-field-address", ".address"),
)

_DO206 = SiteSpec(
    source="do206",
    # Do206 is a DoStuff Media property; its markup uses the ds-* prefix.
    listing_url="https://do206.com/events",
    card_selectors=(
        ".ds-listing",
        ".event-card",
        "article.ds-listing",
        "[data-event-id]",
        "article",
    ),
    title_selectors=(
        ".ds-listing-event-title-text",
        ".ds-listing-event-title",
        "h3 a",
        "h2 a",
        "h3",
    ),
    link_selectors=("a.ds-listing-event-title", "a[href]"),
    date_selectors=("time[datetime]", ".ds-event-time", ".ds-listing-details", "time"),
    venue_selectors=(".ds-venue-name", ".ds-listing-details", ".venue"),
    price_selectors=(".ds-listing-price", ".price"),
)

_THE_STRANGER = SiteSpec(
    source="thestranger",
    # The Stranger's calendar is published through EverOut; override
    # listing_url from config if that arrangement changes.
    listing_url="https://everout.com/seattle/events/",
    card_selectors=(
        ".event-list-item",
        ".event",
        "article.event",
        ".calendar-item",
        "article",
    ),
    title_selectors=(".event-title a", "h2 a", "h3 a", ".title", "h2"),
    date_selectors=("time[datetime]", ".event-date", ".date", "time"),
    venue_selectors=(".location-name", ".event-location", ".venue"),
    address_selectors=(".location-address", ".address"),
    price_selectors=(".event-price", ".price", ".cost"),
)


#: Visit Seattle runs WordPress and exposes a structured events route.
#: Preferred over CSS scraping: the listing markup was rebuilt and none of the
#: selectors in ``_VISIT_SEATTLE`` match it any more, while this returns clean
#: JSON. robots.txt disallows only /wp-admin/, /portals/, /thank-you/ and
#: /site/, so /wp-json/ is permitted.
_VISIT_SEATTLE_API = "https://visitseattle.org/wp-json/visitseattle/v1/events"


def _visitseattle_from_api(
    site_cfg: Mapping[str, Any], window_days: int
) -> list[RawEvent]:
    """Return in-window events from the Visit Seattle JSON route."""
    url = site_cfg.get("api_url") or _VISIT_SEATTLE_API
    response = http_get(url, headers={"Accept": "application/json"})
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError(f"visitseattle: {url} did not return JSON") from exc
    if not isinstance(payload, list):
        raise SourceError(f"visitseattle: expected a JSON array from {url}")

    events: list[RawEvent] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        start_dt = normalize_dt(item.get("start_date"))
        # Undated rows are dropped rather than deferred: this route always
        # carries start_date, so a missing one means a malformed row.
        if not within_window(start_dt, window_days, include_undated=False):
            continue
        link = clean_text(item.get("website_url")) or _VISIT_SEATTLE.listing_url
        events.append(
            build_event(
                source="visitseattle",
                url=link,
                source_id=str(item.get("event_id") or "") or None,
                title=item.get("title"),
                description=strip_html(item.get("description")),
                start_dt=start_dt,
                end_dt=normalize_dt(item.get("end_date")),
                venue_name=item.get("venue"),
                venue_address=item.get("address"),
                register_url=link,
                raw=dict(item),
            )
        )
    return events


def _scrape_visitseattle(
    site_cfg: Mapping[str, Any], window_days: int
) -> list[RawEvent]:
    """Return in-window events from visitseattle.org.

    Prefers the JSON route and falls back to the CSS spec, so a future
    markup fix keeps working if the route is ever withdrawn.
    """
    try:
        return _visitseattle_from_api(site_cfg, window_days)
    except SourceError as exc:
        LOG.warning("visitseattle JSON route unusable (%s); trying HTML", exc)
        return _scrape_with_spec(_VISIT_SEATTLE, site_cfg, window_days)


def _scrape_seattlegov(site_cfg: Mapping[str, Any], window_days: int) -> list[RawEvent]:
    """Return in-window events from the City of Seattle event calendar."""
    return _scrape_with_spec(_SEATTLE_GOV, site_cfg, window_days)


def _scrape_do206(site_cfg: Mapping[str, Any], window_days: int) -> list[RawEvent]:
    """Return in-window events from do206.com."""
    return _scrape_with_spec(_DO206, site_cfg, window_days)


def _scrape_thestranger(site_cfg: Mapping[str, Any], window_days: int) -> list[RawEvent]:
    """Return in-window events from The Stranger's EverOut calendar."""
    return _scrape_with_spec(_THE_STRANGER, site_cfg, window_days)


ScraperFn = Callable[[Mapping[str, Any], int], list[RawEvent]]

#: Registry of available scrapers. Add a site by adding one entry.
SCRAPERS: dict[str, ScraperFn] = {
    "visitseattle": _scrape_visitseattle,
    "seattlegov": _scrape_seattlegov,
    "do206": _scrape_do206,
    "thestranger": _scrape_thestranger,
}


# --------------------------------------------------------------------------
# Adapter entry point
# --------------------------------------------------------------------------


def _iter_site_configs(cfg: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield ``(scraper_name, site_cfg)`` pairs from the pipeline config.

    Accepts ``["do206", ...]``, ``[{"name": "do206", ...}, ...]`` and
    ``{"do206": {...}}``. The registry key comes from ``scraper`` when
    present, so a site's display ``name`` can differ from the scraper it
    reuses. With nothing configured, every registered scraper runs with
    default settings.
    """
    scrape_cfg = cfg.get("scrape", cfg) if isinstance(cfg, Mapping) else {}
    sites = scrape_cfg.get("sites") if isinstance(scrape_cfg, Mapping) else None

    if not sites:
        for name in SCRAPERS:
            yield name, {}
        return

    if isinstance(sites, Mapping):
        for name, site_cfg in sites.items():
            yield str(name), site_cfg if isinstance(site_cfg, Mapping) else {}
        return

    for entry in sites:
        if isinstance(entry, str):
            yield entry, {}
        elif isinstance(entry, Mapping):
            name = entry.get("scraper") or entry.get("name") or entry.get("source")
            if name:
                yield str(name), entry
            else:
                LOG.warning("Skipping scrape site entry without a name: %r", entry)
        else:
            LOG.warning("Skipping unrecognised scrape site entry: %r", entry)


def fetch(cfg: dict, window_days: int = 10) -> list[dict]:
    """Return raw event dicts for events starting within window_days from now.

    Accepts either the ``scrape`` sub-config (as ``stages/gather.py``
    passes it) or a full config containing a ``scrape`` key.

    Runs every configured site scraper and concatenates the results. By
    default a failing scraper propagates its :class:`SourceError` so the
    caller decides what to do; set ``continue_on_error: true`` to log and
    skip broken sites instead. Disable one site with
    ``{"name": "do206", "enabled": false}``.
    """
    scrape_cfg = cfg.get("scrape", cfg) if isinstance(cfg, Mapping) else {}
    if not isinstance(scrape_cfg, Mapping):
        scrape_cfg = {}
    continue_on_error = bool(scrape_cfg.get("continue_on_error", False))
    if scrape_cfg.get("host_delay_seconds") is not None:
        set_polite_delay(scrape_cfg["host_delay_seconds"])

    events: list[RawEvent] = []
    for name, site_cfg in _iter_site_configs(cfg):
        if site_cfg.get("enabled") is False:
            LOG.info("Skipping disabled scrape site %r", name)
            continue
        scraper = SCRAPERS.get(name)
        if scraper is None:
            LOG.warning(
                "No scraper registered for %r (known: %s)", name, ", ".join(sorted(SCRAPERS))
            )
            continue
        try:
            events.extend(scraper(site_cfg, window_days))
        except SourceError as exc:
            if not continue_on_error:
                raise
            LOG.error("Scraper %r failed, continuing: %s", name, exc)
        except Exception as exc:  # unexpected parse bug: surface it as SourceError
            wrapped = SourceError(f"Scraper {name!r} raised {type(exc).__name__}: {exc}")
            if not continue_on_error:
                raise wrapped from exc
            LOG.exception("Scraper %r raised unexpectedly, continuing", name)

    LOG.info("scrape adapter returning %d event(s)", len(events))
    return list(dedupe_events(events))
