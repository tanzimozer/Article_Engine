"""Shared plumbing for every Article_Engine source adapter.

This module owns the pieces that all adapters must agree on:

* :class:`RawEvent` -- the strict dict contract every adapter returns.
* :class:`SourceError` -- the failure signal an adapter raises instead of
  handing back partial garbage.
* :func:`http_get` -- the single chokepoint for outbound HTTP (descriptive
  User-Agent, 3 attempts with exponential backoff, 20 second timeout,
  per-host politeness delay, best-effort robots.txt compliance).
* :func:`normalize_dt` -- parses the date formats event calendars publish
  and returns ISO 8601 anchored to America/Los_Angeles.
* :func:`build_event` -- constructs a ``RawEvent`` with exactly the
  contract keys so no adapter can drift.

Nothing in here performs adapter-specific parsing; see ``rss.py``,
``scrape.py`` and ``eventbrite.py`` for that.
"""

from __future__ import annotations

import html as _html
import logging
import os
import random
import re
import threading
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any, Final, Mapping, TypedDict
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

try:  # python-dateutil is a declared dependency but base.py stays usable without it.
    from dateutil import parser as _dateutil_parser
except ImportError:  # pragma: no cover - exercised only on incomplete installs
    _dateutil_parser = None  # type: ignore[assignment]

LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

def _nth_sunday(year: int, month: int, nth: int) -> datetime:
    """Return 02:00 local on the *nth* Sunday of *month* in *year*."""
    first = datetime(year, month, 1, 2, 0)
    offset = (6 - first.weekday()) % 7  # Monday=0 ... Sunday=6
    return first + timedelta(days=offset + 7 * (nth - 1))


class _FallbackPacific(tzinfo):
    """US Pacific time implemented from the post-2007 DST rule.

    Only used when neither the system tz database nor python-dateutil can
    supply America/Los_Angeles (a bare Windows install, typically).
    Accurate outside the one-hour transition windows; install ``tzdata``
    for exact behaviour.
    """

    _STANDARD = timedelta(hours=-8)
    _SAVING = timedelta(hours=1)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        """Return the UTC offset in effect at *dt*."""
        return self._STANDARD + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        """Return one hour during daylight saving time, else zero."""
        if dt is None:
            return timedelta(0)
        naive = dt.replace(tzinfo=None)
        start = _nth_sunday(naive.year, 3, 2)  # second Sunday in March
        end = _nth_sunday(naive.year, 11, 1)  # first Sunday in November
        return self._SAVING if start <= naive < end else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        """Return ``"PDT"`` or ``"PST"`` for *dt*."""
        return "PDT" if self.dst(dt) else "PST"

    def __repr__(self) -> str:
        return "<_FallbackPacific America/Los_Angeles>"


def _load_pacific() -> tzinfo:
    """Return an America/Los_Angeles tzinfo from the best source available.

    Prefers the stdlib tz database, then python-dateutil, then a built-in
    DST-rule fallback so a missing ``tzdata`` never breaks import.
    """
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:  # ZoneInfoNotFoundError on systems without a tz database
        pass
    try:
        from dateutil import tz as _dateutil_tz

        zone = _dateutil_tz.gettz("America/Los_Angeles")
        if zone is not None:
            return zone
    except ImportError:
        pass
    LOG.warning(
        "No America/Los_Angeles tz database found; using the built-in DST-rule "
        "fallback. Run 'pip install tzdata' for exact Pacific timestamps."
    )
    return _FallbackPacific()


#: Every timestamp this pipeline emits is expressed in Seattle local time.
PACIFIC: Final[tzinfo] = _load_pacific()

_DEFAULT_USER_AGENT: Final[str] = (
    "Article_Engine/1.0 (+Seattle events article pipeline; "
    "polite crawler, contact repository maintainer)"
)

#: Descriptive UA sent on every request. Override with ARTICLE_ENGINE_USER_AGENT
#: to add a real contact address without editing source.
USER_AGENT: Final[str] = os.environ.get(
    "ARTICLE_ENGINE_USER_AGENT", _DEFAULT_USER_AGENT
).strip() or _DEFAULT_USER_AGENT

HTTP_TIMEOUT: Final[float] = 20.0
MAX_ATTEMPTS: Final[int] = 3
BACKOFF_BASE_SECONDS: Final[float] = 1.0
MAX_BACKOFF_SECONDS: Final[float] = 30.0

#: Minimum gap between two requests to the same host. robots.txt Crawl-delay
#: wins when it asks for more.
POLITE_DELAY_SECONDS: Final[float] = 1.0

#: Transient statuses worth another attempt. Everything else 4xx fails fast.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: The exact key set of a RawEvent, in contract order.
EVENT_KEYS: Final[tuple[str, ...]] = (
    "source",
    "source_id",
    "url",
    "title",
    "description",
    "start_dt",
    "end_dt",
    "venue_name",
    "venue_address",
    "price",
    "register_url",
    "image_url",
    "raw",
)


# --------------------------------------------------------------------------
# Contract types and errors
# --------------------------------------------------------------------------


class RawEvent(TypedDict):
    """One event as published by a single source, before enrichment.

    Every key is always present; missing values are ``None`` rather than
    absent. ``raw`` carries the original parsed payload for provenance.
    """

    source: str
    source_id: str
    url: str
    title: str
    description: str
    start_dt: str | None
    end_dt: str | None
    venue_name: str | None
    venue_address: str | None
    price: str | None
    register_url: str | None
    image_url: str | None
    raw: dict[str, Any]


class SourceError(RuntimeError):
    """A source could not be fetched or parsed well enough to trust.

    Adapters raise this instead of returning half-parsed results so the
    caller can decide whether to skip the source or abort the run.
    """


class RobotsDisallowedError(SourceError):
    """The target site's robots.txt forbids fetching the requested URL."""


# --------------------------------------------------------------------------
# HTTP: politeness, robots, retries
# --------------------------------------------------------------------------

_HOST_LOCK = threading.Lock()
_LAST_REQUEST_AT: dict[str, float] = {}
_HOST_DELAY: dict[str, float] = {}
_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_SESSION_LOCK = threading.Lock()
_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    """Return the process-wide :class:`requests.Session` used for all fetches.

    Reusing one session keeps connections pooled across adapters.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            _SESSION = session
        return _SESSION


def _host_of(url: str) -> str:
    """Return the lowercased ``host:port`` of *url*, or ``""`` if unparseable."""
    return (urlparse(url).netloc or "").lower()


def _polite_wait(host: str) -> None:
    """Block until the configured minimum gap since the last hit on *host*."""
    if not host:
        return
    with _HOST_LOCK:
        delay = _HOST_DELAY.get(host, POLITE_DELAY_SECONDS)
        last = _LAST_REQUEST_AT.get(host)
        now = time.monotonic()
        wait_for = 0.0 if last is None else (last + delay) - now
        # Reserve this host's slot before releasing the lock so concurrent
        # callers queue up instead of all sleeping the same interval.
        _LAST_REQUEST_AT[host] = now + max(wait_for, 0.0)
    if wait_for > 0:
        LOG.debug("Polite delay: sleeping %.2fs before next request to %s", wait_for, host)
        time.sleep(wait_for)


def _load_robots(url: str) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and cache the robots.txt governing *url*.

    Returns a parser, or ``None`` when robots.txt is missing or unreadable
    (which this pipeline treats as "no restrictions stated").
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[origin]

    parser: urllib.robotparser.RobotFileParser | None = None
    robots_url = f"{origin}/robots.txt"
    try:
        response = http_get(
            robots_url,
            attempts=1,
            timeout=10.0,
            respect_robots=False,
            allow_statuses=(200, 401, 403, 404, 410),
        )
        if response.status_code == 200 and response.text.strip():
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
        else:
            LOG.debug("No usable robots.txt at %s (HTTP %s)", robots_url, response.status_code)
    except SourceError as exc:
        LOG.debug("Could not read %s (%s); proceeding without robots rules", robots_url, exc)

    _ROBOTS_CACHE[origin] = parser
    if parser is not None:
        try:
            crawl_delay = parser.crawl_delay(USER_AGENT)
        except Exception:  # pragma: no cover - robotparser is lenient but not total
            crawl_delay = None
        if crawl_delay:
            with _HOST_LOCK:
                _HOST_DELAY[parsed.netloc.lower()] = max(
                    POLITE_DELAY_SECONDS, float(crawl_delay)
                )
            LOG.info("Honoring robots.txt Crawl-delay of %ss for %s", crawl_delay, parsed.netloc)
    return parser


def robots_allows(url: str) -> bool:
    """Return whether robots.txt permits this pipeline's UA to fetch *url*.

    Missing or unparseable robots.txt returns ``True``: this is a
    trivially-checkable courtesy, not an access-control mechanism.
    """
    parser = _load_robots(url)
    if parser is None:
        return True
    try:
        return bool(parser.can_fetch(USER_AGENT, url))
    except Exception:  # pragma: no cover
        return True


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Return the Retry-After delay of *response* in seconds, if it states one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=PACIFIC)
    return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())


def http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    respect_robots: bool = True,
    allow_statuses: tuple[int, ...] = (200,),
) -> requests.Response:
    """GET *url* politely and return the :class:`requests.Response`.

    Every network call in this package goes through here. Behaviour:
    a descriptive User-Agent, a 20 second timeout, a per-host courtesy
    delay, best-effort robots.txt compliance, and up to *attempts* tries
    with exponential backoff plus jitter on transient failures.

    Returns a response whose status is in *allow_statuses*. Raises
    :class:`RobotsDisallowedError` when robots.txt forbids the URL and
    :class:`SourceError` when every attempt fails.
    """
    if respect_robots and not robots_allows(url):
        raise RobotsDisallowedError(f"robots.txt disallows fetching {url}")

    session = get_session()
    request_headers = dict(headers or {})
    host = _host_of(url)
    last_error: str = "no attempts made"

    for attempt in range(1, max(1, attempts) + 1):
        _polite_wait(host)
        try:
            response = session.get(
                url,
                params=params,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning(
                "GET %s failed on attempt %d/%d: %s", url, attempt, attempts, last_error
            )
        else:
            if response.status_code in allow_statuses:
                return response
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUSES:
                raise SourceError(f"GET {url} returned {last_error}")
            LOG.warning(
                "GET %s returned %s on attempt %d/%d", url, last_error, attempt, attempts
            )
            retry_after = _retry_after_seconds(response)
            if retry_after is not None and attempt < attempts:
                sleep_for = min(retry_after, MAX_BACKOFF_SECONDS)
                LOG.info("Server asked us to wait %.1fs before retrying %s", sleep_for, url)
                time.sleep(sleep_for)
                continue

        if attempt < attempts:
            backoff = min(
                BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS
            )
            backoff += random.uniform(0, backoff * 0.25)  # jitter, avoids lockstep retries
            LOG.debug("Backing off %.2fs before retrying %s", backoff, url)
            time.sleep(backoff)

    raise SourceError(f"GET {url} failed after {attempts} attempts ({last_error})")


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>|<\s*br\s*/?\s*>"
)


def clean_text(value: Any) -> str | None:
    """Return *value* as a whitespace-collapsed string, or ``None`` if empty."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def strip_html(value: Any) -> str | None:
    """Return the plain-text content of an HTML fragment, or ``None`` if empty.

    Block-level closers become spaces so words do not run together.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = _BLOCK_BREAK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return clean_text(_html.unescape(text))


def absolute_url(base_url: str, href: str | None) -> str | None:
    """Resolve *href* against *base_url*; returns ``None`` for empty/junk hrefs."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    try:
        resolved = urljoin(base_url, href)
    except ValueError:
        return None
    return resolved or None


def first_present(*values: Any) -> Any:
    """Return the first argument that is neither ``None`` nor an empty string."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


# --------------------------------------------------------------------------
# Date handling
# --------------------------------------------------------------------------

#: Splits "7:00 PM - 10:00 PM" without touching ISO dates like 2026-08-15:
#: an ASCII hyphen only separates when surrounded by whitespace, while the
#: typographic dashes (U+2010..U+2015, U+2212) separate on their own.
_RANGE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:-|to|until|thru|through|till)\s+|\s*[\u2010-\u2015\u2212]\s*",
    re.IGNORECASE,
)
_FOUR_DIGIT_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{4}\b")
_NOISE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:starts?|begins?|ends?|on|from|at|date|time)\s*[:\-]?\s+", re.IGNORECASE
)

# Tried in order when python-dateutil is unavailable.
_STRPTIME_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y %I:%M %p",
    "%B %d, %Y",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y",
    "%A, %B %d, %Y %I:%M %p",
    "%A, %B %d, %Y",
    "%d %B %Y",
)
# Year-less variants; the year is taken from the parse anchor.
_YEARLESS_FORMATS: Final[tuple[str, ...]] = (
    "%B %d %I:%M %p",
    "%B %d",
    "%b %d %I:%M %p",
    "%b %d",
    "%A %B %d",
    "%a %B %d",
    "%a %b %d",
    "%m/%d %I:%M %p",
    "%m/%d",
)
# Bare clock times ("10:00 PM"); the calendar date comes from the parse anchor.
_TIMEONLY_FORMATS: Final[tuple[str, ...]] = (
    "%I:%M %p",
    "%I:%M%p",
    "%I %p",
    "%I%p",
    "%H:%M",
)


def now_pacific() -> datetime:
    """Return the current time as an aware datetime in America/Los_Angeles."""
    return datetime.now(PACIFIC)


def _anchor() -> datetime:
    """Return midnight today (Pacific), used to fill in omitted date parts."""
    return now_pacific().replace(hour=0, minute=0, second=0, microsecond=0)


def _from_epoch(text: str) -> datetime | None:
    """Interpret *text* as Unix epoch seconds or milliseconds, else ``None``."""
    if not re.fullmatch(r"\d{9,13}", text):
        return None
    number = int(text)
    if len(text) >= 12:  # milliseconds
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=PACIFIC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_naive_or_aware(text: str, anchor: datetime) -> datetime | None:
    """Parse *text* into a datetime using ISO, epoch, dateutil, then strptime."""
    candidate = text.replace("\u00a0", " ").strip()
    candidate = _NOISE_PREFIX_RE.sub("", candidate).strip().strip(",;")
    if not candidate:
        return None

    iso_candidate = candidate.replace("Z", "+00:00") if candidate.endswith("Z") else candidate
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass

    epoch = _from_epoch(candidate)
    if epoch is not None:
        return epoch

    if _dateutil_parser is not None:
        for fuzzy in (False, True):
            try:
                return _dateutil_parser.parse(candidate, default=anchor, fuzzy=fuzzy)
            except (ValueError, OverflowError, TypeError):
                continue
        return None

    # stdlib fallback: dateutil is a declared dependency, but never hard-fail.
    normalized = candidate.replace(",", " ")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    for fmt in _STRPTIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt.replace(",", ""))
        except ValueError:
            continue
    for fmt in _YEARLESS_FORMATS:
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return parsed.replace(year=anchor.year)
    for fmt in _TIMEONLY_FORMATS:
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return anchor.replace(hour=parsed.hour, minute=parsed.minute)
    return None


def normalize_dt(
    value: Any,
    *,
    assume_tz: tzinfo = PACIFIC,
    roll_forward: bool = False,
) -> str | None:
    """Parse *value* and return ISO 8601 in America/Los_Angeles, or ``None``.

    Accepts ISO 8601 (including trailing ``Z``), Unix epoch seconds/ms,
    ``datetime``/``date`` objects, ``time.struct_time``, and the prose
    formats event calendars publish ("Sat, March 5, 2026 7:00 PM").
    Naive values are assumed to be in *assume_tz*; aware values are
    converted. Set *roll_forward* when the source omits the year and the
    listing is known to be upcoming: a parsed date well in the past is
    bumped by one year. Returns ``None`` when nothing parseable is found.
    """
    if value is None:
        return None

    anchor = _anchor()
    parsed: datetime | None
    text_form = ""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=PACIFIC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, time.struct_time):
        # feedparser hands back struct_time already normalized to UTC.
        try:
            parsed = datetime(*value[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    else:
        text_form = str(value).strip()
        if not text_form:
            return None
        parsed = _parse_naive_or_aware(text_form, anchor)

    if parsed is None:
        LOG.debug("Could not parse a datetime from %r", value)
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    parsed = parsed.astimezone(PACIFIC)

    if roll_forward and text_form and not _FOUR_DIGIT_YEAR_RE.search(text_form):
        if parsed < anchor - timedelta(days=45):
            try:
                parsed = parsed.replace(year=parsed.year + 1)
            except ValueError:  # Feb 29 -> Mar 1
                parsed = parsed.replace(year=parsed.year + 1, day=28)

    return parsed.isoformat()


def split_dt_range(text: str | None) -> tuple[str | None, str | None]:
    """Split a displayed range into ``(start_text, end_text)``.

    Handles "7:00 PM - 10:00 PM", en/em dashes and "to"/"through"
    wording without breaking ISO dates such as ``2026-08-15``. Returns
    ``(text, None)`` when no separator is found.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None, None
    parts = [part.strip() for part in _RANGE_SPLIT_RE.split(cleaned) if part.strip()]
    if len(parts) < 2:
        return cleaned, None
    return parts[0], parts[1]


def parse_dt_range(
    text: str | None, *, roll_forward: bool = True
) -> tuple[str | None, str | None]:
    """Parse a displayed date range into ``(start_iso, end_iso)``.

    A bare end time ("7:00 PM - 10:00 PM") inherits the start's calendar
    date. Either element is ``None`` when unparseable.
    """
    start_text, end_text = split_dt_range(text)
    start_iso = normalize_dt(start_text, roll_forward=roll_forward)
    if not end_text:
        return start_iso, None

    end_iso = normalize_dt(end_text, roll_forward=roll_forward)
    if start_iso and end_iso:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        if end_dt < start_dt and not _FOUR_DIGIT_YEAR_RE.search(end_text):
            # "7:00 PM - 10:00 PM": the end carried no date of its own.
            end_dt = end_dt.replace(
                year=start_dt.year, month=start_dt.month, day=start_dt.day
            )
            if end_dt < start_dt:
                end_dt += timedelta(days=1)  # crosses midnight
            end_iso = end_dt.isoformat()
    return start_iso, end_iso


def within_window(
    start_iso: str | None,
    window_days: int = 10,
    *,
    include_undated: bool = True,
) -> bool:
    """Return whether *start_iso* falls inside the collection window.

    The window runs from the start of today (Pacific) through the end of
    the ``window_days``-th day, so events already underway today are kept
    and events far in the future are not. Undated events are kept when
    *include_undated* is true, letting a later enrichment step resolve
    them rather than dropping the listing outright.
    """
    if not start_iso:
        return include_undated
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        LOG.debug("within_window received an unparseable timestamp: %r", start_iso)
        return include_undated
    if start.tzinfo is None:
        start = start.replace(tzinfo=PACIFIC)
    start = start.astimezone(PACIFIC)

    lower = _anchor()
    upper = lower + timedelta(days=max(0, window_days) + 1)
    return lower <= start < upper


# --------------------------------------------------------------------------
# Event construction
# --------------------------------------------------------------------------


def build_event(
    *,
    source: str,
    url: str,
    title: str | None,
    source_id: str | None = None,
    description: str | None = None,
    start_dt: str | None = None,
    end_dt: str | None = None,
    venue_name: str | None = None,
    venue_address: str | None = None,
    price: str | None = None,
    register_url: str | None = None,
    image_url: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> RawEvent:
    """Return a :class:`RawEvent` holding exactly the contract keys.

    Text fields are whitespace-collapsed, blanks become ``None``,
    ``source_id`` falls back to *url*, and ``description``/``title``
    fall back to ``""`` so the three required strings are never ``None``.
    """
    canonical_url = (url or "").strip()
    return {
        "source": source,
        "source_id": clean_text(source_id) or canonical_url,
        "url": canonical_url,
        "title": clean_text(title) or "",
        "description": clean_text(description) or "",
        "start_dt": start_dt,
        "end_dt": end_dt,
        "venue_name": clean_text(venue_name),
        "venue_address": clean_text(venue_address),
        "price": clean_text(price),
        "register_url": clean_text(register_url),
        "image_url": clean_text(image_url),
        "raw": dict(raw or {}),
    }


def validate_event(event: Mapping[str, Any]) -> bool:
    """Return whether *event* has exactly ``EVENT_KEYS`` and a usable url/title."""
    if set(event) != set(EVENT_KEYS):
        missing = sorted(set(EVENT_KEYS) - set(event))
        extra = sorted(set(event) - set(EVENT_KEYS))
        LOG.debug("Event key mismatch (missing=%s extra=%s)", missing, extra)
        return False
    return bool(event.get("url")) and bool(event.get("title"))


def dedupe_events(events: list[RawEvent]) -> list[RawEvent]:
    """Return *events* with same-source duplicates removed, order preserved.

    Identity is ``(source, source_id)``, falling back to the URL.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[RawEvent] = []
    for event in events:
        key = (event.get("source", ""), event.get("source_id") or event.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    dropped = len(events) - len(unique)
    if dropped:
        LOG.debug("Dropped %d duplicate event(s)", dropped)
    return unique
