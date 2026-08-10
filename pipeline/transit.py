"""GTFS-backed transit lookups for the Seattle events article pipeline.

Reads the King County Metro and Sound Transit GTFS feeds with nothing but the
standard library (``zipfile`` + ``csv``) and caches the parsed result so the
feeds are downloaded at most once per :data:`CACHE_TTL_SECONDS`.

Public API:
    nearest_stops(lat, lon, limit=3) -> list[dict]
    refresh_gtfs(cache_dir) -> None
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: Free, key-less GTFS feeds. Feed name -> download URL.
GTFS_FEEDS: dict[str, str] = {
    "king_county_metro": "https://metro.kingcounty.gov/GTFS/google_transit.zip",
    "sound_transit": "https://www.soundtransit.org/GTFS-rail/40_gtfs.zip",
}

#: Re-download the feeds when the cache is older than this (7 days).
CACHE_TTL_SECONDS: int = 7 * 24 * 60 * 60

#: Schema version of the on-disk cache. Bump to invalidate old caches.
CACHE_VERSION: int = 1

_CACHE_FILENAME = "gtfs_stops.json"

#: Where the parsed feed is cached. ``state/gtfs_cache/`` is the location
#: .gitignore reserves for it, so the cache never lands in a commit.
#: Override with ARTICLE_ENGINE_GTFS_CACHE.
DEFAULT_CACHE_DIR: str = os.environ.get(
    "ARTICLE_ENGINE_GTFS_CACHE",
    str(Path(__file__).resolve().parent.parent / "state" / "gtfs_cache"),
)

USER_AGENT: str = (
    "Article_Engine/1.0 (Seattle events article pipeline; "
    "contact: " + os.environ.get("ARTICLE_ENGINE_CONTACT", "timbr.tools@gmail.com") + ")"
)

DOWNLOAD_TIMEOUT: int = 180

#: Primary search radius, then the widened fallback radius, in metres.
SEARCH_RADIUS_M: float = 1500.0
FALLBACK_SEARCH_RADIUS_M: float = 5000.0

#: Same-named stops closer together than this are merged into one result.
_MERGE_RADIUS_M: float = 400.0

_EARTH_RADIUS_M: float = 6_371_008.8

# GTFS ``route_type`` -> mode bucket. The contract allows only three modes, so
# heavy rail (Sounder, route_type 2) is bucketed with light rail; there is no
# separate "commuter_rail" value to report it under.
_RAIL_ROUTE_TYPES = {0, 1, 2, 12}
_BUS_ROUTE_TYPES = {3, 11}

#: Higher wins when a single stop is served by several modes.
_MODE_PRIORITY: dict[str, int] = {"light_rail": 3, "streetcar": 2, "bus": 1}


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# GTFS parsing helpers
# --------------------------------------------------------------------------- #


def _open_member(archive: zipfile.ZipFile, filename: str) -> io.TextIOWrapper:
    """Open a GTFS member by basename, tolerating feeds nested in a folder."""
    for name in archive.namelist():
        if name.rsplit("/", 1)[-1].lower() == filename.lower():
            # utf-8-sig strips the BOM some agencies ship on their CSV headers.
            return io.TextIOWrapper(archive.open(name), encoding="utf-8-sig", newline="")
    raise KeyError(f"{filename} not found in GTFS archive")


def _route_mode(route_type: str, label: str) -> str | None:
    """Map a GTFS ``route_type`` to one of the three supported modes."""
    try:
        rtype = int((route_type or "").strip())
    except ValueError:
        return None
    if rtype in _RAIL_ROUTE_TYPES:
        # route_type 0 covers both light rail and streetcars; only the route
        # name distinguishes Link from the SLU/First Hill streetcars.
        return "streetcar" if "streetcar" in label.lower() else "light_rail"
    if rtype in _BUS_ROUTE_TYPES:
        return "bus"
    return None  # ferry, funicular, aerial lift, ... -- not reported.


def _sort_routes(routes: Iterable[str]) -> list[str]:
    """Numeric routes first in numeric order, then everything else A-Z."""

    def sort_key(route: str) -> tuple[int, int, str]:
        match = re.match(r"^\s*(\d+)", route)
        if match:
            return (0, int(match.group(1)), route)
        return (1, 0, route)

    return sorted({r for r in routes if r}, key=sort_key)


def _parse_feed(zip_path: Path, feed_name: str) -> list[dict[str, Any]]:
    """Parse one GTFS zip into a list of stop records that have service."""
    with zipfile.ZipFile(zip_path) as archive:
        # routes.txt -> route_id: (label, mode)
        routes: dict[str, tuple[str, str]] = {}
        with _open_member(archive, "routes.txt") as handle:
            for row in csv.DictReader(handle):
                route_id = (row.get("route_id") or "").strip()
                if not route_id:
                    continue
                short = (row.get("route_short_name") or "").strip()
                long_name = (row.get("route_long_name") or "").strip()
                label = short or long_name
                if not label:
                    continue
                mode = _route_mode(row.get("route_type") or "", f"{short} {long_name}")
                if mode is None:
                    continue
                routes[route_id] = (label, mode)

        # trips.txt -> trip_id: route_id
        trip_routes: dict[str, str] = {}
        with _open_member(archive, "trips.txt") as handle:
            for row in csv.DictReader(handle):
                trip_id = (row.get("trip_id") or "").strip()
                route_id = (row.get("route_id") or "").strip()
                if trip_id and route_id in routes:
                    trip_routes[trip_id] = route_id

        # stop_times.txt -> stop_id: {route_id, ...}
        # This is the big one (millions of rows), so it is streamed with the
        # plain reader and integer column offsets rather than DictReader.
        stop_routes: dict[str, set[str]] = defaultdict(set)
        with _open_member(archive, "stop_times.txt") as handle:
            reader = csv.reader(handle)
            try:
                header = [c.strip().lower() for c in next(reader)]
            except StopIteration:
                header = []
            try:
                trip_idx = header.index("trip_id")
                stop_idx = header.index("stop_id")
            except ValueError:
                raise KeyError("stop_times.txt is missing trip_id/stop_id columns")
            width = max(trip_idx, stop_idx) + 1
            last_trip = None
            last_route: str | None = None
            for row in reader:
                if len(row) < width:
                    continue
                trip_id = row[trip_idx]
                # stop_times is grouped by trip, so this hits almost always.
                if trip_id != last_trip:
                    last_trip = trip_id
                    last_route = trip_routes.get(trip_id)
                if last_route is not None:
                    stop_routes[row[stop_idx]].add(last_route)

        # stops.txt -> final records, dropping stations/nodes with no service.
        stops: list[dict[str, Any]] = []
        with _open_member(archive, "stops.txt") as handle:
            for row in csv.DictReader(handle):
                stop_id = (row.get("stop_id") or "").strip()
                route_ids = stop_routes.get(stop_id)
                if not route_ids:
                    continue
                try:
                    lat = float(row.get("stop_lat") or "")
                    lon = float(row.get("stop_lon") or "")
                except ValueError:
                    continue
                name = (row.get("stop_name") or "").strip()
                if not name:
                    continue
                labels = {routes[r][0] for r in route_ids}
                mode = max(
                    (routes[r][1] for r in route_ids),
                    key=lambda m: _MODE_PRIORITY.get(m, 0),
                )
                stops.append(
                    {
                        "stop_name": name,
                        "lat": lat,
                        "lon": lon,
                        "routes": _sort_routes(labels),
                        "mode": mode,
                        "feed": feed_name,
                    }
                )

    logger.info("Parsed %d served stops from GTFS feed %r", len(stops), feed_name)
    return stops


def _download(url: str, dest: Path) -> None:
    """Stream a GTFS zip to ``dest``."""
    logger.info("Downloading GTFS feed %s", url)
    with requests.get(
        url,
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as response:
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    handle.write(chunk)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _cache_path(cache_dir: str) -> Path:
    return Path(cache_dir) / _CACHE_FILENAME


def _read_cache(cache_dir: str) -> dict[str, Any] | None:
    path = _cache_path(cache_dir)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("GTFS cache at %s is unreadable; ignoring it", path, exc_info=True)
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        logger.info("GTFS cache at %s has an old schema; ignoring it", path)
        return None
    if not isinstance(data.get("stops"), list):
        return None
    return data


def _cache_is_fresh(cache: dict[str, Any]) -> bool:
    try:
        fetched_at = float(cache.get("fetched_at", 0.0))
    except (TypeError, ValueError):
        return False
    return (time.time() - fetched_at) < CACHE_TTL_SECONDS


def refresh_gtfs(cache_dir: str) -> None:
    """Download and cache the King County Metro + Sound Transit GTFS feeds.

    Downloads each feed, parses ``stops.txt`` / ``routes.txt`` / ``trips.txt`` /
    ``stop_times.txt``, and writes a single consolidated JSON cache into
    ``cache_dir``. A feed that fails to download or parse is logged and skipped;
    :class:`RuntimeError` is raised only when no feed could be loaded at all, so
    an existing cache is never replaced with nothing.
    """
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)

    stops: list[dict[str, Any]] = []
    loaded: list[str] = []

    for feed_name, url in GTFS_FEEDS.items():
        temp_zip: str | None = None
        try:
            handle, temp_zip = tempfile.mkstemp(
                suffix=".zip", prefix=f"gtfs_{feed_name}_", dir=str(directory)
            )
            os.close(handle)
            _download(url, Path(temp_zip))
            stops.extend(_parse_feed(Path(temp_zip), feed_name))
            loaded.append(feed_name)
        except Exception:
            logger.warning("Could not load GTFS feed %r from %s", feed_name, url, exc_info=True)
        finally:
            if temp_zip:
                try:
                    os.unlink(temp_zip)
                except OSError:
                    logger.debug("Could not remove temporary GTFS zip %s", temp_zip)

    if not loaded:
        raise RuntimeError("No GTFS feed could be downloaded or parsed")

    payload = {
        "version": CACHE_VERSION,
        "fetched_at": time.time(),
        "feeds": loaded,
        "stops": stops,
    }

    path = _cache_path(cache_dir)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as out:
        json.dump(payload, out, separators=(",", ":"))
    os.replace(temp_path, path)
    logger.info("Cached %d GTFS stops from %s to %s", len(stops), ", ".join(loaded), path)


def _load_stops(cache_dir: str) -> list[dict[str, Any]]:
    """Return cached stops, refreshing when the cache is missing or stale."""
    cache = _read_cache(cache_dir)
    if cache is not None and _cache_is_fresh(cache):
        return cache["stops"]

    try:
        refresh_gtfs(cache_dir)
    except Exception:
        if cache is not None:
            logger.warning(
                "GTFS refresh failed; falling back to the stale cache in %s",
                cache_dir,
                exc_info=True,
            )
            return cache["stops"]
        logger.error("GTFS refresh failed and no cache is available", exc_info=True)
        return []

    refreshed = _read_cache(cache_dir)
    return refreshed["stops"] if refreshed else []


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _candidates_within(
    stops: list[dict[str, Any]], lat: float, lon: float, radius_m: float
) -> list[tuple[float, dict[str, Any]]]:
    """Bounding-box prefilter, then exact haversine distances."""
    lat_pad = radius_m / 111_320.0
    cos_lat = math.cos(math.radians(lat))
    lon_pad = radius_m / (111_320.0 * cos_lat) if abs(cos_lat) > 1e-9 else 180.0

    found: list[tuple[float, dict[str, Any]]] = []
    for stop in stops:
        stop_lat = stop["lat"]
        stop_lon = stop["lon"]
        if abs(stop_lat - lat) > lat_pad or abs(stop_lon - lon) > lon_pad:
            continue
        distance = _haversine_m(lat, lon, stop_lat, stop_lon)
        if distance <= radius_m:
            found.append((distance, stop))
    return found


def _merge_by_name(
    candidates: list[tuple[float, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Collapse the several stop_ids that share a name (e.g. both directions).

    Keeps the nearest instance's distance and unions the routes served by every
    nearby instance, so a corner reads as one stop with all of its routes.
    """
    merged: dict[str, dict[str, Any]] = {}
    for distance, stop in sorted(candidates, key=lambda item: item[0]):
        key = " ".join(stop["stop_name"].lower().split())
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                "stop_name": stop["stop_name"],
                "routes": list(stop["routes"]),
                "distance_m": distance,
                "mode": stop["mode"],
            }
            continue
        if distance - existing["distance_m"] > _MERGE_RADIUS_M:
            continue  # Same name, but a genuinely different place -- leave it.
        existing["routes"].extend(stop["routes"])
        if _MODE_PRIORITY.get(stop["mode"], 0) > _MODE_PRIORITY.get(existing["mode"], 0):
            existing["mode"] = stop["mode"]

    results = list(merged.values())
    for result in results:
        result["routes"] = _sort_routes(result["routes"])
        result["distance_m"] = int(round(result["distance_m"]))
    results.sort(key=lambda item: (item["distance_m"], item["stop_name"]))
    return results


def nearest_stops(lat: float, lon: float, limit: int = 3) -> list[dict]:
    """Return the nearest transit stops to ``(lat, lon)`` from the GTFS feeds.

    Each entry is ``{"stop_name": str, "routes": list[str], "distance_m": int,
    "mode": str}`` where ``mode`` is ``"light_rail"``, ``"bus"`` or
    ``"streetcar"``. Returns ``[]`` when GTFS data is unavailable or no stop is
    within :data:`FALLBACK_SEARCH_RADIUS_M` -- it never guesses a stop.
    """
    if limit <= 0:
        return []
    try:
        stops = _load_stops(DEFAULT_CACHE_DIR)
    except Exception:
        logger.exception("Could not load GTFS stop data")
        return []
    if not stops:
        logger.warning("No GTFS stop data available; returning no transit for %s,%s", lat, lon)
        return []

    candidates = _candidates_within(stops, lat, lon, SEARCH_RADIUS_M)
    if not candidates:
        candidates = _candidates_within(stops, lat, lon, FALLBACK_SEARCH_RADIUS_M)
    if not candidates:
        logger.info("No transit stop within %.0f m of %s,%s", FALLBACK_SEARCH_RADIUS_M, lat, lon)
        return []

    return _merge_by_name(candidates)[:limit]


if __name__ == "__main__":  # pragma: no cover - manual cache warm-up
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CACHE_DIR
    refresh_gtfs(target)
