"""Verified local description for a venue.

The `seattle_local_specificity` judge wants terrain, history and neighborhood
texture. The fact-checker deletes any claim it cannot trace to a source. Until
this module existed, nothing supplied verified *description*, so those two
gates deadlocked: an article could satisfy one only by failing the other. The
first real run scored 3/8 on local specificity while the fact-checker stripped
"flat course" twice, for exactly this reason.

Sources are chosen so a claim built on them is auditable:

* **Wikipedia** -- an official REST API, no key, stable per-article URLs, and
  descriptive prose about parks and neighborhoods. Its summary of Magnuson Park
  supplies the former-naval-station history, the acreage and the Lake
  Washington setting that the judge asked for by name.
* **Seattle Parks** -- the city's own page for the venue. Permitted by
  seattle.gov's robots.txt, which disallows `/events/` and `/calendar/` but not
  `/parks/`.

Everything returned carries the URL it came from, so the writer can cite it,
the fact-checker can re-fetch it, and a judge can audit it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

from pipeline.enrich import USER_AGENT

logger = logging.getLogger(__name__)

WIKIPEDIA_API: str = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_SUMMARY: str = "https://en.wikipedia.org/api/rest_v1/page/summary/"
SEATTLE_PARKS_BASE: str = "https://www.seattle.gov/parks/allparks/"

HTTP_TIMEOUT: int = 25

#: Per-source cap. Magnuson Park's full Wikipedia article is 9,400 characters;
#: all of it would ride in the writer payload, and the writer is already the
#: slowest call in the pipeline. This keeps the lead section plus early history,
#: which is where the terrain and setting live.
EXTRACT_MAX_CHARS: int = 2500

#: Wikipedia disambiguates aggressively, and a Seattle park can share a name
#: with somewhere else entirely. Every lookup is scoped to Seattle and the
#: result is rejected unless the extract mentions the city.
_SEATTLE_MARKERS = ("seattle", "king county", "lake washington", "puget sound")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

_cache: dict[str, dict[str, Any] | None] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wikipedia_title(venue_name: str) -> str | None:
    """Resolve *venue_name* to a Wikipedia article title, or ``None``."""
    try:
        response = _session.get(
            WIKIPEDIA_API,
            params={
                "action": "query", "list": "search", "format": "json",
                "srsearch": f"{venue_name} Seattle", "srlimit": 3,
            },
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("Wikipedia search failed for %r", venue_name, exc_info=True)
        return None
    if response.status_code != 200:
        logger.warning("Wikipedia search returned HTTP %s", response.status_code)
        return None

    hits = (response.json().get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def _wikipedia_summary(title: str) -> dict[str, Any] | None:
    """Fetch a plain-text extract for a Wikipedia article title.

    Uses the action API rather than the REST summary: the summary returns only
    the lead sentence or two (~380 characters for Magnuson Park), while the
    extract carries the history and setting the local-specificity judge asks
    for. Capped, because this rides in the writer payload and the writer is
    already the slowest call in the pipeline.
    """
    try:
        response = _session.get(
            WIKIPEDIA_API,
            params={
                "action": "query", "prop": "extracts", "explaintext": 1,
                "format": "json", "titles": title, "exsectionformat": "plain",
            },
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("Wikipedia extract failed for %r", title, exc_info=True)
        return None
    if response.status_code != 200:
        return None

    pages = (response.json().get("query") or {}).get("pages") or {}
    if not pages:
        return None
    page = next(iter(pages.values()))
    extract = re.sub(r"\n{2,}", "\n", (page.get("extract") or "")).strip()
    if not extract:
        return None
    extract = extract[:EXTRACT_MAX_CHARS]

    payload = {"title": page.get("title") or title}

    # Reject an article that is not about somewhere in Seattle. "Green Lake"
    # alone matches a Wisconsin town; a wrong-city description would be worse
    # than none, because it reads as verified.
    if not any(marker in extract.lower() for marker in _SEATTLE_MARKERS):
        logger.info("Discarding Wikipedia article %r: no Seattle marker in extract", title)
        return None

    resolved = payload["title"]
    return {
        "source": "wikipedia",
        "title": resolved,
        "url": "https://en.wikipedia.org/wiki/" + resolved.replace(" ", "_"),
        "extract": extract,
        "fetched_at": _now(),
    }


def _parks_slug(venue_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", venue_name.lower()).strip("-")
    # The city drops the honorific: "Warren G. Magnuson Park" lives at
    # /parks/allparks/magnuson-park.
    return re.sub(r"^(warren-g-|judge-|dr-)", "", slug)


def _seattle_parks(venue_name: str) -> dict[str, Any] | None:
    """Fetch the City of Seattle's own page for a park, if one exists."""
    url = SEATTLE_PARKS_BASE + _parks_slug(venue_name)
    try:
        response = _session.get(url, timeout=HTTP_TIMEOUT,
                                headers={"Accept": "text/html"})
    except requests.RequestException:
        logger.warning("Seattle Parks fetch failed for %r", venue_name, exc_info=True)
        return None
    if response.status_code != 200:
        logger.info("No Seattle Parks page at %s (HTTP %s)", url, response.status_code)
        return None

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    # <main> only, not the whole document. The page opens with a site-wide
    # alert banner -- a burn ban, when this was written -- and taking the full
    # body made that the first thing the writer read about the park.
    container = soup.select_one("main") or soup
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    if len(text) < 200:
        return None

    return {
        "source": "seattle_parks",
        "title": venue_name,
        "url": url,
        "extract": text[:EXTRACT_MAX_CHARS],
        "fetched_at": _now(),
    }


def fetch(venue_name: str, neighborhood: str | None = None) -> list[dict[str, Any]]:
    """Return citable descriptions of *venue_name*, most authoritative first.

    Each entry carries ``source``, ``url``, ``extract`` and ``fetched_at``, so
    a claim drawn from it survives fact-checking and can be audited by a judge.
    Returns an empty list when nothing usable is found -- a venue with no
    description is a thinner article, never a wrong one.
    """
    name = (venue_name or "").strip()
    if not name:
        return []

    key = f"{name.lower()}|{(neighborhood or '').lower()}"
    if key in _cache:
        cached = _cache[key]
        return list(cached) if cached else []

    found: list[dict[str, Any]] = []

    title = _wikipedia_title(name)
    if title:
        summary = _wikipedia_summary(title)
        if summary:
            found.append(summary)

    parks = _seattle_parks(name)
    if parks:
        found.append(parks)

    if found:
        logger.info("Venue context for %r: %s", name,
                    ", ".join(f"{f['source']} ({len(f['extract'])} chars)" for f in found))
    else:
        logger.info("No venue context found for %r", name)

    _cache[key] = found or None
    return found
