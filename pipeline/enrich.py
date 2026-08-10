"""Venue enrichment for the Seattle events article pipeline.

Fills in the things an event listing usually leaves out -- coordinates, the
neighborhood, a photo and nearby transit -- using only free, key-less sources:
Nominatim (OpenStreetMap) for geocoding, the venue's own website for photos, a
curated stock library for the fallback photo, and GTFS for transit.

Nothing here invents data. Every function returns ``None`` for anything it
cannot actually source, and :func:`fallback_image` alt text always states that
the stock photo is illustrative rather than a picture of the real venue.

Event shape
-----------
The canonical flat record, matching the ``events`` columns in
:mod:`pipeline.state` so ``state.upsert_event`` persists every field::

    {"venue_name": ..., "venue_address": ..., "lat": ..., "lon": ...,
     "neighborhood": ..., "image_url": ..., "image_alt": ...,
     "transit_json": [{"stop_name": ..., "routes": [...],
                       "distance_m": ..., "mode": ...}, ...]}

``transit_json`` holds a plain list, not a string: ``state.upsert_event``
serialises the ``*_json`` columns itself, and ``stages/publish.py``,
``stages/write.py`` and ``stages/factcheck.py`` all read it back from there.

A nested shape is also accepted and written back in kind, for callers that hold
events in memory rather than in the database::

    {"venue": {"name": ..., "address": ..., "url": ..., "lat": ..., "lon": ...},
     "image": {"url": ..., "alt": ..., "source": ...}}

``venue_url`` is honoured when present but is not a column on ``events``; with
no venue website the photo step goes straight to the labelled stock fallback
rather than scraping an aggregator listing and calling it the venue's own photo.

Public API:
    geocode(venue_name, address) -> tuple[float, float] | None
    neighborhood_for(lat, lon) -> str | None
    find_venue_image(venue_name, venue_url) -> str | None
    fallback_image(neighborhood) -> tuple[str, str]
    enrich_event(event) -> dict
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit

import requests

try:  # Works both as ``pipeline.enrich`` and as a directly-run module.
    from . import transit as transit_module
except ImportError:  # pragma: no cover - depends on how the caller imports us
    import transit as transit_module  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_CONTACT = os.environ.get("ARTICLE_ENGINE_CONTACT", "timbr.tools@gmail.com")

#: Nominatim's usage policy requires a descriptive User-Agent with a contact.
USER_AGENT: str = f"Article_Engine/1.0 (Seattle events article pipeline; contact: {_CONTACT})"

NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"

#: Nominatim's usage policy caps clients at one request per second.
NOMINATIM_MIN_INTERVAL_SECONDS: float = 1.0
NOMINATIM_TIMEOUT: int = 30

#: Soft search bias (lon,lat,lon,lat) covering Seattle proper.
_SEATTLE_VIEWBOX = "-122.46,47.75,-122.22,47.48"

#: Hard sanity bounds. A geocode outside this box is discarded rather than
#: returned -- a confident wrong city is worse than no answer.
_LAT_MIN, _LAT_MAX = 47.40, 47.80
_LON_MIN, _LON_MAX = -122.55, -122.10

IMAGE_LIBRARY_PATH: str = os.environ.get(
    "ARTICLE_ENGINE_IMAGE_LIBRARY",
    str(Path(__file__).resolve().parent.parent / "config" / "neighborhood_images.json"),
)

VENUE_PAGE_TIMEOUT: int = 20
#: Stop reading a venue page after this much HTML; the metadata is in the head.
MAX_HTML_BYTES: int = 1_500_000
#: HEAD/ranged-GET each candidate image so dead URLs are not passed downstream.
VERIFY_IMAGE_URLS: bool = True

_DISCLAIMER = "Illustrative image, not the event venue."

# --------------------------------------------------------------------------- #
# Nominatim client (rate limited, single flight)
# --------------------------------------------------------------------------- #

_nominatim_lock = threading.Lock()
_nominatim_last_request = 0.0
_nominatim_session = requests.Session()
_nominatim_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

_geocode_cache: dict[str, tuple[float, float] | None] = {}
_neighborhood_cache: dict[tuple[float, float], str | None] = {}


def _nominatim_request(endpoint: str, params: dict[str, Any]) -> Any | None:
    """GET a Nominatim endpoint, never faster than one request per second.

    The lock is held across the whole request so concurrent callers queue up
    instead of issuing the parallel requests Nominatim's policy forbids.
    """
    global _nominatim_last_request
    url = f"{NOMINATIM_BASE_URL}/{endpoint}"
    with _nominatim_lock:
        elapsed = time.monotonic() - _nominatim_last_request
        wait = NOMINATIM_MIN_INTERVAL_SECONDS - elapsed
        if wait > 0:
            logger.debug("Throttling Nominatim request by %.2fs", wait)
            time.sleep(wait)
        try:
            response = _nominatim_session.get(url, params=params, timeout=NOMINATIM_TIMEOUT)
        except requests.RequestException:
            logger.warning("Nominatim request to %s failed", endpoint, exc_info=True)
            return None
        finally:
            _nominatim_last_request = time.monotonic()

    if response.status_code != 200:
        logger.warning("Nominatim %s returned HTTP %s", endpoint, response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("Nominatim %s returned a non-JSON body", endpoint, exc_info=True)
        return None


def _in_seattle_bounds(lat: float, lon: float) -> bool:
    return _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX


def geocode(venue_name: str, address: str | None) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` for a venue, or ``None``.

    Uses Nominatim (OpenStreetMap) -- free, no API key. Results outside the
    greater-Seattle sanity box are rejected rather than returned, because
    Nominatim will happily match a same-named venue in another city.
    """
    name = (venue_name or "").strip()
    if not name:
        logger.debug("geocode called without a venue name")
        return None

    address = (address or "").strip()
    if address:
        query = f"{name}, {address}"
    else:
        query = f"{name}, Seattle, WA"
    if "seattle" not in query.lower() and "wa" not in query.lower().split():
        query = f"{query}, Seattle, WA"

    cache_key = query.lower()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    payload = _nominatim_request(
        "search",
        {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",
            "viewbox": _SEATTLE_VIEWBOX,  # bias only; results are bounds-checked below
            "addressdetails": 0,
        },
    )

    result: tuple[float, float] | None = None
    if isinstance(payload, list) and payload:
        try:
            lat = float(payload[0]["lat"])
            lon = float(payload[0]["lon"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Nominatim returned an unusable result for %r", query)
        else:
            if _in_seattle_bounds(lat, lon):
                result = (round(lat, 6), round(lon, 6))
            else:
                logger.warning(
                    "Discarding out-of-area geocode for %r: %.5f,%.5f is outside Seattle",
                    query,
                    lat,
                    lon,
                )
    else:
        logger.info("No geocode result for %r", query)

    _geocode_cache[cache_key] = result
    return result


def neighborhood_for(lat: float, lon: float) -> str | None:
    """Return the Seattle neighborhood containing ``(lat, lon)``, or ``None``.

    Reverse-geocodes with Nominatim and canonicalises the answer against the
    names used by the stock image library. Points outside Seattle return
    ``None`` rather than a neighboring city's district name.
    """
    try:
        key = (round(float(lat), 4), round(float(lon), 4))
    except (TypeError, ValueError):
        logger.warning("neighborhood_for got non-numeric coordinates: %r, %r", lat, lon)
        return None
    if key in _neighborhood_cache:
        return _neighborhood_cache[key]

    payload = _nominatim_request(
        "reverse",
        {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 16, "addressdetails": 1},
    )

    result: str | None = None
    address = payload.get("address") if isinstance(payload, dict) else None
    if isinstance(address, dict):
        city = ""
        for city_key in ("city", "town", "village", "municipality"):
            value = address.get(city_key)
            if value:
                city = str(value)
                break
        if city and "seattle" not in city.lower():
            logger.info("Point %.5f,%.5f is in %s, not Seattle", lat, lon, city)
        else:
            # ``suburb`` is the field that actually carries Seattle's
            # well-known neighborhood names (Ballard, Fremont, Georgetown).
            # ``neighbourhood`` holds much noisier sub-areas -- it reports
            # "Van Asselt" for a point in Georgetown -- so it ranks lower.
            for field in (
                "suburb",
                "quarter",
                "neighbourhood",
                "city_district",
                "residential",
                "borough",
            ):
                raw = address.get(field)
                if raw:
                    raw_name = str(raw).strip()
                    result = _canonical_neighborhood(raw_name) or raw_name
                    break
            if result is None:
                logger.info("Nominatim had no neighborhood for %.5f,%.5f", lat, lon)

    _neighborhood_cache[key] = result
    return result


# --------------------------------------------------------------------------- #
# Neighborhood name canonicalisation
# --------------------------------------------------------------------------- #

#: Common OSM / colloquial spellings mapped onto the library's canonical names.
#: Sub-neighborhoods are folded into the parent the image library covers.
_NEIGHBORHOOD_ALIASES: dict[str, str] = {
    "u district": "University District",
    "udistrict": "University District",
    "the u district": "University District",
    "university village": "University District",
    "sodo": "SoDo",
    "so do": "SoDo",
    "industrial district": "SoDo",
    "stadium district": "SoDo",
    "downtown seattle": "Downtown",
    "central business district": "Downtown",
    "cbd": "Downtown",
    "slu": "South Lake Union",
    "greenlake": "Green Lake",
    "uptown": "Queen Anne",
    "lower queen anne": "Queen Anne",
    "upper queen anne": "Queen Anne",
    "north queen anne": "Queen Anne",
    "east queen anne": "Queen Anne",
    "west queen anne": "Queen Anne",
    "north beacon hill": "Beacon Hill",
    "mid beacon hill": "Beacon Hill",
    "south beacon hill": "Beacon Hill",
    "north capitol hill": "Capitol Hill",
    "pike pine": "Capitol Hill",
    "alki": "West Seattle",
    "alki point": "West Seattle",
    "admiral": "West Seattle",
    "the junction": "West Seattle",
    "west seattle junction": "West Seattle",
    "phinney": "Phinney Ridge",
    "the ave": "University District",
}


def _normalise_name(name: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for matching."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return " ".join(cleaned.split())


def _canonical_neighborhood(name: str | None) -> str | None:
    """Map a free-form neighborhood name onto a library key, or ``None``."""
    if not name:
        return None
    key = _normalise_name(name)
    if not key:
        return None
    library_keys = _library_key_index()
    if key in library_keys:
        return library_keys[key]
    return _NEIGHBORHOOD_ALIASES.get(key)


# --------------------------------------------------------------------------- #
# Stock image library
# --------------------------------------------------------------------------- #

_library_cache: dict[str, Any] | None = None
_library_index_cache: dict[str, str] | None = None


def _load_image_library() -> dict[str, Any]:
    """Load and memoise ``config/neighborhood_images.json``."""
    global _library_cache
    if _library_cache is not None:
        return _library_cache
    try:
        with open(IMAGE_LIBRARY_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("neighborhood image library must be a JSON object")
    except (OSError, ValueError):
        logger.error("Could not load image library at %s", IMAGE_LIBRARY_PATH, exc_info=True)
        data = {}
    _library_cache = data
    return data


def _library_key_index() -> dict[str, str]:
    """Normalised library key -> canonical library key (metadata keys skipped)."""
    global _library_index_cache
    if _library_index_cache is not None:
        return _library_index_cache
    index: dict[str, str] = {}
    for key in _load_image_library():
        if key.startswith("_") or key == "default":
            continue
        index[_normalise_name(key)] = key
    _library_index_cache = index
    return index


def _library_entry(key: str) -> dict[str, str] | None:
    """Return ``{"url": ..., "description": ...}`` for a key, or ``None``.

    Accepts both value shapes the library allows: a bare URL string, or an
    object with a ``url``. ``null`` (uncurated) yields ``None``.
    """
    library = _load_image_library()
    value = library.get(key)
    if value is None:
        # Fall back to a case-insensitive match on the raw key.
        wanted = _normalise_name(key)
        for candidate, candidate_value in library.items():
            if candidate.startswith("_"):
                continue
            if _normalise_name(candidate) == wanted:
                value = candidate_value
                break
    if isinstance(value, str) and value.strip():
        return {"url": value.strip(), "description": ""}
    if isinstance(value, dict):
        url = str(value.get("url") or "").strip()
        if url:
            return {"url": url, "description": str(value.get("description") or "").strip()}
    return None


def _build_alt(description: str, neighborhood: str | None) -> str:
    """Compose descriptive alt text that always flags the image as illustrative."""
    if description:
        base = description.strip()
    elif neighborhood:
        base = f"Street view in {neighborhood}, Seattle."
    else:
        base = "View of Seattle."
    if not base.endswith((".", "!", "?")):
        base += "."
    return f"{base} {_DISCLAIMER}"


def fallback_image(neighborhood: str | None) -> tuple[str, str]:
    """Return ``(image_url, alt_text)`` from the curated stock library.

    Falls back to the library's ``default`` entry when the neighborhood is
    unknown or still uncurated. The alt text always describes the photo that is
    actually being shown and always states that it is illustrative -- when the
    generic default is used it does not name the neighborhood, because the
    photo is not of that neighborhood.

    Returns ``("", alt_text)`` if even ``default`` is uncurated; callers should
    treat an empty URL as "no image available".
    """
    canonical = _canonical_neighborhood(neighborhood)
    label = canonical or (neighborhood.strip() if neighborhood else None)

    entry: dict[str, str] | None = None
    for key in (canonical, neighborhood):
        if key:
            entry = _library_entry(key)
            if entry:
                break

    if entry is not None:
        return entry["url"], _build_alt(entry["description"], label)

    if label:
        logger.info("No curated stock image for %r; using the default image", label)
    default_entry = _library_entry("default")
    if default_entry is not None:
        # Deliberately no neighborhood in the alt text: this is a generic photo.
        return default_entry["url"], _build_alt(default_entry["description"], None)

    logger.error(
        "Image library at %s has no usable 'default' entry; returning an empty image URL",
        IMAGE_LIBRARY_PATH,
    )
    return "", _build_alt("", None)


# --------------------------------------------------------------------------- #
# Venue photo discovery
# --------------------------------------------------------------------------- #

_META_IMAGE_KEYS = (
    "og:image:secure_url",
    "og:image",
    "og:image:url",
    "twitter:image",
    "twitter:image:src",
)

#: URL fragments that mark an asset as chrome rather than a photo.
_IMAGE_URL_BLOCKLIST = (
    "logo",
    "icon",
    "favicon",
    "sprite",
    "avatar",
    "placeholder",
    "spinner",
    "loader",
    "pixel",
    "spacer",
    "badge",
    "banner-ad",
    "1x1",
)

#: Hostname hints for asset hosts a venue site legitimately serves photos from.
_CDN_HOST_HINTS = (
    "cdn",
    "static",
    "assets",
    "images",
    "media",
    "wixstatic",
    "squarespace",
    "shopify",
    "cloudfront",
    "imgix",
    "wp.com",
    "akamaized",
    "fastly",
)

_venue_session = requests.Session()
_venue_session.headers.update({"User-Agent": USER_AGENT})


class _VenuePageParser(HTMLParser):
    """Collect social-card metadata and ``<img>`` tags from a venue page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.link_image: str | None = None
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rels = values.get("rel", "").lower().split()
            href = values.get("href", "").strip()
            if href and self.link_image is None and "image_src" in rels:
                self.link_image = href
        elif tag == "img" and len(self.images) < 200:
            self.images.append(values)


def _registrable_domain(host: str) -> str:
    parts = [part for part in (host or "").lower().split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


def _is_plausible_photo_url(url: str) -> bool:
    if not url or url.startswith("data:"):
        return False
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        return False
    lowered = url.lower()
    if lowered.split("?")[0].endswith((".svg", ".gif", ".ico")):
        return False
    return not any(token in lowered for token in _IMAGE_URL_BLOCKLIST)


#: Extensions a real photograph is served with. Used only for the ``<img>``
#: fallback: a PNG on a venue homepage is nearly always a logo or graphic, and
#: returning a logo as "a photo of the venue" is worse than returning nothing.
_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".webp", ".avif")
_NON_PHOTO_EXTENSIONS = (".png", ".bmp", ".tif", ".tiff")


def _looks_photographic(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(_PHOTO_EXTENSIONS):
        return True
    # CDN URLs often carry no extension at all; those stay eligible.
    return not path.endswith(_NON_PHOTO_EXTENSIONS)


def _attrs_look_like_chrome(values: dict[str, str]) -> bool:
    """Reject images whose class/id/alt marks them as a logo, icon, badge, ..."""
    haystack = " ".join(
        values.get(key, "") for key in ("class", "id", "alt", "title")
    ).lower()
    return any(token in haystack for token in _IMAGE_URL_BLOCKLIST)


def _same_site_or_cdn(image_url: str, page_url: str) -> bool:
    image_host = (urlsplit(image_url).hostname or "").lower()
    page_host = (urlsplit(page_url).hostname or "").lower()
    if not image_host:
        return False
    if _registrable_domain(image_host) == _registrable_domain(page_host):
        return True
    return any(hint in image_host for hint in _CDN_HOST_HINTS)


def _image_is_reachable(url: str) -> bool:
    """Confirm a URL actually serves an image, so dead links are not emitted."""
    if not VERIFY_IMAGE_URLS:
        return True
    try:
        response = _venue_session.head(url, timeout=VENUE_PAGE_TIMEOUT, allow_redirects=True)
        if response.status_code >= 400 or not response.headers.get("Content-Type"):
            # Plenty of CDNs reject HEAD; retry with a one-byte ranged GET.
            response = _venue_session.get(
                url,
                timeout=VENUE_PAGE_TIMEOUT,
                allow_redirects=True,
                stream=True,
                headers={"Range": "bytes=0-0"},
            )
            response.close()
        if response.status_code >= 400:
            logger.debug("Candidate image %s returned HTTP %s", url, response.status_code)
            return False
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and not content_type.startswith("image/"):
            logger.debug("Candidate image %s has content-type %s", url, content_type)
            return False
        return True
    except requests.RequestException:
        logger.debug("Could not verify candidate image %s", url, exc_info=True)
        return False


def _int_attr(values: dict[str, str], key: str) -> int | None:
    raw = (values.get(key) or "").strip()
    match = re.match(r"^(\d+)", raw)
    return int(match.group(1)) if match else None


def _fetch_html(url: str) -> str | None:
    """Fetch a page, capped at :data:`MAX_HTML_BYTES`, HTML content types only."""
    try:
        with _venue_session.get(
            url, timeout=VENUE_PAGE_TIMEOUT, allow_redirects=True, stream=True
        ) as response:
            if response.status_code >= 400:
                logger.info("Venue page %s returned HTTP %s", url, response.status_code)
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                logger.info("Venue page %s is %s, not HTML", url, content_type)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1 << 14):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_HTML_BYTES:
                    break
            encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")
    except (requests.RequestException, LookupError):
        logger.warning("Could not fetch venue page %s", url, exc_info=True)
        return None


def find_venue_image(venue_name: str, venue_url: str | None) -> str | None:
    """Return a photo URL published by the venue's own site, or ``None``.

    Prefers the page's own social-card image (``og:image`` / ``twitter:image``),
    then falls back to the largest in-page ``<img>`` that is served from the
    venue's domain or an asset CDN. Returns ``None`` when there is no venue URL
    -- it never guesses a website from the venue name.
    """
    url = (venue_url or "").strip()
    if not url:
        logger.debug("No venue URL for %r; cannot look for a venue photo", venue_name)
        return None
    if not urlsplit(url).scheme:
        url = f"https://{url}"

    html = _fetch_html(url)
    if html is None:
        return None

    parser = _VenuePageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.warning("Could not parse venue page %s", url, exc_info=True)
        return None

    # 1. Social-card metadata, declared by the venue's own page.
    candidates: list[str] = []
    for key in _META_IMAGE_KEYS:
        value = parser.meta.get(key)
        if value:
            candidates.append(urljoin(url, value))
    if parser.link_image:
        candidates.append(urljoin(url, parser.link_image))

    for candidate in candidates:
        if _is_plausible_photo_url(candidate) and _image_is_reachable(candidate):
            logger.info("Found venue photo for %r via page metadata", venue_name)
            return candidate

    # 2. Largest same-site/CDN <img> on the page.
    scored: list[tuple[int, str]] = []
    for values in parser.images:
        src = (values.get("src") or values.get("data-src") or "").strip()
        if not src:
            continue
        absolute = urljoin(url, src)
        if not _is_plausible_photo_url(absolute) or not _same_site_or_cdn(absolute, url):
            continue
        if not _looks_photographic(absolute) or _attrs_look_like_chrome(values):
            continue
        width = _int_attr(values, "width")
        height = _int_attr(values, "height")
        if (width is not None and width < 200) or (height is not None and height < 200):
            continue
        scored.append(((width or 0) * (height or 0), absolute))

    for _, candidate in sorted(scored, key=lambda item: item[0], reverse=True):
        if _image_is_reachable(candidate):
            logger.info("Found venue photo for %r via an in-page <img>", venue_name)
            return candidate

    logger.info("No usable venue photo found on %s", url)
    return None


# --------------------------------------------------------------------------- #
# Event enrichment
# --------------------------------------------------------------------------- #

# field -> (nested key aliases, flat key aliases). The first alias is used when
# the key is absent and has to be created.
_VENUE_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "name": (("name",), ("venue_name",)),
    "address": (("address", "street_address"), ("venue_address", "address")),
    "url": (("url", "website"), ("venue_url", "venue_website")),
    "lat": (("lat", "latitude"), ("lat", "latitude", "venue_lat")),
    "lon": (("lon", "lng", "longitude"), ("lon", "lng", "longitude", "venue_lon")),
}

_IMAGE_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "url": (("url",), ("image_url",)),
    "alt": (("alt", "alt_text"), ("image_alt", "image_alt_text")),
    "source": (("source",), ("image_source",)),
}

_NEIGHBORHOOD_KEYS = ("neighborhood", "neighbourhood")

# ``transit_json`` first, deliberately. ``state._EVENT_FIELDS`` only persists
# known columns and the events table has ``transit_json``, not ``transit`` --
# writing to ``transit`` would let upsert_event drop the stops on the floor.
# state.upsert_event serialises the list itself, so it is stored unserialised.
_TRANSIT_KEYS = ("transit_json", "transit", "transit_stops")


def _container(event: dict, group: str) -> tuple[dict, bool]:
    """Return the dict venue/image fields live on, and whether it is nested."""
    nested = event.get(group)
    if isinstance(nested, dict):
        return nested, True
    return event, False


def _aliases(
    specs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]], field: str, nested: bool
) -> tuple[str, ...]:
    return specs[field][0] if nested else specs[field][1]


def _read(container: dict, keys: Sequence[str]) -> Any:
    for key in keys:
        value = container.get(key)
        if value is not None and value != "":
            return value
    return None


def _write(container: dict, keys: Sequence[str], value: Any) -> None:
    """Write to whichever alias already exists, otherwise create the first."""
    for key in keys:
        if key in container:
            container[key] = value
            return
    container[keys[0]] = value


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def enrich_event(event: dict) -> dict:
    """Fill in missing venue coords, neighborhood, image and transit.

    Returns a new dict; the input is never mutated. Existing values are never
    overwritten, and any field that cannot be sourced is left as ``None`` --
    nothing is guessed or fabricated. Each stage is isolated, so one failing
    lookup does not abort the rest of the enrichment.
    """
    if not isinstance(event, dict):
        raise TypeError(f"enrich_event expects a dict, got {type(event).__name__}")

    enriched = copy.deepcopy(event)
    venue, venue_nested = _container(enriched, "venue")

    name = _read(venue, _aliases(_VENUE_SPECS, "name", venue_nested))
    if name is None and not venue_nested and isinstance(enriched.get("venue"), str):
        name = enriched["venue"].strip() or None  # ``{"venue": "Neumos"}`` shape
    address = _read(venue, _aliases(_VENUE_SPECS, "address", venue_nested))
    venue_url = _read(venue, _aliases(_VENUE_SPECS, "url", venue_nested))
    lat_keys = _aliases(_VENUE_SPECS, "lat", venue_nested)
    lon_keys = _aliases(_VENUE_SPECS, "lon", venue_nested)
    lat = _as_float(_read(venue, lat_keys))
    lon = _as_float(_read(venue, lon_keys))

    # 1. Coordinates -------------------------------------------------------- #
    if lat is None or lon is None:
        try:
            coords = geocode(str(name), str(address) if address else None) if name else None
            if coords is None and not name:
                logger.info("Event has no venue name; cannot geocode")
        except Exception:
            logger.exception("Geocoding failed for venue %r", name)
            coords = None
        if coords is not None:
            lat, lon = coords
            _write(venue, lat_keys, lat)
            _write(venue, lon_keys, lon)
        else:
            # Normalise the shape without destroying a partial input value.
            _write(venue, lat_keys, lat)
            _write(venue, lon_keys, lon)

    has_coords = lat is not None and lon is not None

    # 2. Neighborhood ------------------------------------------------------- #
    neighborhood = _read(enriched, _NEIGHBORHOOD_KEYS)
    if neighborhood is None:
        if has_coords:
            try:
                neighborhood = neighborhood_for(lat, lon)  # type: ignore[arg-type]
            except Exception:
                logger.exception("Neighborhood lookup failed for %s,%s", lat, lon)
                neighborhood = None
        else:
            logger.info("No coordinates for %r; neighborhood stays None", name)
        _write(enriched, _NEIGHBORHOOD_KEYS, neighborhood)

    # 3. Image -------------------------------------------------------------- #
    image, image_nested = _container(enriched, "image")
    image_url_keys = _aliases(_IMAGE_SPECS, "url", image_nested)
    image_alt_keys = _aliases(_IMAGE_SPECS, "alt", image_nested)
    image_source_keys = _aliases(_IMAGE_SPECS, "source", image_nested)

    if _read(image, image_url_keys) is None:
        new_url: str | None = None
        new_alt: str | None = None
        new_source: str | None = None
        try:
            if name and venue_url:
                new_url = find_venue_image(str(name), str(venue_url))
                if new_url:
                    # Neutral wording: the venue published it, but we cannot
                    # assert the photo depicts the venue itself.
                    new_alt = f"Image from the website of {name}."
                    new_source = "venue_site"
        except Exception:
            logger.exception("Venue image lookup failed for %r", name)
            new_url = None

        if not new_url:
            try:
                stock_url, stock_alt = fallback_image(
                    str(neighborhood) if neighborhood else None
                )
                if stock_url:
                    new_url, new_alt, new_source = stock_url, stock_alt, "stock_neighborhood"
                else:
                    logger.info("No stock image available; image stays None")
            except Exception:
                logger.exception("Stock image lookup failed for %r", neighborhood)

        _write(image, image_url_keys, new_url)
        if _read(image, image_alt_keys) is None:
            _write(image, image_alt_keys, new_alt)
        if _read(image, image_source_keys) is None:
            _write(image, image_source_keys, new_source)

    # 4. Transit ------------------------------------------------------------ #
    if _read(enriched, _TRANSIT_KEYS) is None:
        stops: list[dict] | None = None
        if has_coords:
            try:
                found = transit_module.nearest_stops(lat, lon)  # type: ignore[arg-type]
                # An empty list is ambiguous -- "no stops nearby" and "no GTFS
                # data" look identical here -- so it is reported as None.
                stops = found or None
                if not found:
                    logger.info("No transit stops resolved for %s,%s", lat, lon)
            except Exception:
                logger.exception("Transit lookup failed for %s,%s", lat, lon)
        else:
            logger.info("No coordinates for %r; transit stays None", name)
        _write(enriched, _TRANSIT_KEYS, stops)

    return enriched
