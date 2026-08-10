"""Source adapters for the Seattle events article pipeline.

Every adapter in this package exposes the same entry point::

    def fetch(cfg: dict, window_days: int = 10) -> list[dict]

and returns dicts with exactly the keys in
:data:`pipeline.sources.base.EVENT_KEYS`. Shared HTTP, date and event
construction helpers live in :mod:`pipeline.sources.base`.

Adapters are imported lazily: ``rss`` needs ``feedparser`` and ``scrape``
needs ``beautifulsoup4``, so importing this package never fails just
because one optional dependency is missing.

Typical use::

    from pipeline import sources

    events = sources.fetch_all(cfg, window_days=10)
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any, Mapping, Sequence

from .base import (
    EVENT_KEYS,
    PACIFIC,
    RawEvent,
    RobotsDisallowedError,
    SourceError,
    build_event,
    dedupe_events,
    http_get,
    normalize_dt,
    validate_event,
    within_window,
)

LOG = logging.getLogger(__name__)

#: Adapter name -> module name within this package.
ADAPTERS: dict[str, str] = {
    "rss": "rss",
    "scrape": "scrape",
    "eventbrite": "eventbrite",
}

__all__ = [
    "ADAPTERS",
    "EVENT_KEYS",
    "PACIFIC",
    "RawEvent",
    "RobotsDisallowedError",
    "SourceError",
    "build_event",
    "dedupe_events",
    "fetch_all",
    "get_adapter",
    "http_get",
    "normalize_dt",
    "validate_event",
    "within_window",
]


def get_adapter(name: str) -> ModuleType:
    """Return the adapter module registered under *name*.

    Raises :class:`KeyError` for an unknown adapter and
    :class:`SourceError` when the adapter's dependencies are missing.
    """
    if name not in ADAPTERS:
        raise KeyError(f"Unknown source adapter {name!r} (known: {', '.join(sorted(ADAPTERS))})")
    try:
        return importlib.import_module(f".{ADAPTERS[name]}", __name__)
    except ImportError as exc:
        raise SourceError(f"Adapter {name!r} is unavailable: {exc}") from exc


def fetch_all(
    cfg: Mapping[str, Any],
    window_days: int = 10,
    *,
    adapters: Sequence[str] | None = None,
    continue_on_error: bool = True,
) -> list[dict]:
    """Run every configured adapter and return their combined raw events.

    Adapters come from *adapters*, else ``cfg["sources"]``, else all
    registered adapters. With *continue_on_error* (the default) a failing
    adapter is logged and skipped; set it to ``False`` to let the first
    :class:`SourceError` propagate. Duplicate ``(source, source_id)``
    pairs are removed while preserving order.
    """
    names = list(adapters or cfg.get("sources") or ADAPTERS)

    events: list[RawEvent] = []
    for name in names:
        try:
            adapter = get_adapter(name)
            produced = adapter.fetch(dict(cfg), window_days)
        except (SourceError, KeyError) as exc:
            if not continue_on_error:
                raise
            LOG.error("Source %r failed, continuing: %s", name, exc)
            continue
        LOG.info("Source %r produced %d event(s)", name, len(produced))
        events.extend(produced)  # type: ignore[arg-type]

    combined = list(dedupe_events(events))
    LOG.info("fetch_all collected %d event(s) from %d source(s)", len(combined), len(names))
    return combined
