"""SQLite state store.

This file is committed to the repo every run. That is deliberate: it gives a
git audit trail, and the commit doubles as the activity that stops GitHub
disabling a scheduled workflow after 60 days of repo silence.

Idempotency lives here, at two levels:

1. ``source_fingerprints`` — one row per (source, source_id). Checked in Stage 1
   *before* any LLM call, so a repeat event costs nothing.
2. ``events.id`` — the canonical id, assigned after the Dedupe Agent has
   collapsed the same real event across sources.

Without both, an event sitting in the 7-10 day window would be re-gathered,
re-scored and re-written every single day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "state" / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_fingerprints (
    fingerprint       TEXT PRIMARY KEY,   -- "<source>:<source_id>"
    canonical_id      TEXT,               -- FK to events.id, NULL until deduped
    content_hash      TEXT NOT NULL,      -- hash of date+venue+price
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);

-- Stage 1 writes here, Stage 2 drains it. Persisting between the two stages
-- rather than passing them in memory means a crash mid-run does not throw away
-- the scoring calls already paid for.
CREATE TABLE IF NOT EXISTS candidates (
    id                TEXT PRIMARY KEY,   -- "<source>:<source_id>"
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    priority          INTEGER NOT NULL,
    raw_json          TEXT NOT NULL,
    relevance_json    TEXT,
    relevance_avg     REAL,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT,
    start_dt          TEXT,
    end_dt            TEXT,
    venue_name        TEXT,
    venue_address     TEXT,
    venue_url         TEXT,               -- the venue's OWN site, not the listing
    lat               REAL,
    lon               REAL,
    neighborhood      TEXT,
    price             TEXT,
    register_url      TEXT,
    skill_level       TEXT,
    what_to_bring     TEXT,
    image_url         TEXT,
    image_alt         TEXT,
    transit_json      TEXT,
    venue_context_json TEXT,             -- cited descriptions of the venue
    content_type      TEXT NOT NULL DEFAULT 'event',  -- event | venue_feature
    provenance_json   TEXT,
    sources_json      TEXT,
    relevance_json    TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id                TEXT PRIMARY KEY,
    event_id          TEXT NOT NULL REFERENCES events(id),
    stage             TEXT NOT NULL,      -- s3_validate .. s9_publish
    status            TEXT NOT NULL,      -- queued|in_progress|held|scheduled|published|archived|dropped
    attempts_json     TEXT NOT NULL DEFAULT '{}',   -- {"s5_factcheck": 2, ...}
    reholds           INTEGER NOT NULL DEFAULT 0,
    relevance_avg     REAL,
    draft_json        TEXT,
    research_json     TEXT,              -- stage 3b fact trail
    voice_json        TEXT,              -- stage 4b edit log
    factcheck_json    TEXT,
    judges_json       TEXT,
    eic_json          TEXT,
    slug              TEXT,
    publish_at        TEXT,
    hold_stage        TEXT,
    hold_reason       TEXT,
    hold_doc_id       TEXT,               -- Google Doc in the hold queue
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published (
    post_id           TEXT PRIMARY KEY,
    article_id        TEXT NOT NULL REFERENCES articles(id),
    event_id          TEXT NOT NULL REFERENCES events(id),
    url               TEXT,
    slug              TEXT,
    event_start_dt    TEXT,
    state             TEXT NOT NULL DEFAULT 'live',  -- live|ended|cancelled|sold_out
    banner            TEXT,
    published_at      TEXT NOT NULL,
    last_checked_at   TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    articles_processed INTEGER NOT NULL DEFAULT 0,
    published         INTEGER NOT NULL DEFAULT 0,
    holds             INTEGER NOT NULL DEFAULT 0,
    failures          INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id);
CREATE INDEX IF NOT EXISTS idx_published_state ON published(state);
CREATE INDEX IF NOT EXISTS idx_fingerprint_canonical ON source_fingerprints(canonical_id);
"""


def now() -> str:
    """UTC timestamp, ISO 8601. Used for every ``*_at`` column."""
    return datetime.now(timezone.utc).isoformat()


def content_hash(start_dt: str | None, venue: str | None, price: str | None) -> str:
    """Hash of the three fields that decide whether an event has materially changed.

    Stage 1 re-runs an already-seen event only when this hash moves. Title
    wording drifts constantly on event listings; date, venue and price do not.
    """
    payload = f"{start_dt or ''}|{venue or ''}|{price or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint(source: str, source_id: str) -> str:
    """Stable per-source key. Cheap enough to check before any model call."""
    return f"{source}:{source_id}"


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the store, guaranteeing the schema exists and rows come back as dicts."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------

def seen_before(conn: sqlite3.Connection, source: str, source_id: str,
                new_hash: str) -> bool:
    """True when this exact source record has been processed and has not changed.

    Returning True means Stage 1 skips the event before spending a relevance
    scoring call on it.
    """
    fp = fingerprint(source, source_id)
    row = conn.execute(
        "SELECT content_hash FROM source_fingerprints WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if row is None:
        return False
    if row["content_hash"] != new_hash:
        log.info("%s changed since last run, reprocessing", fp)
        return False
    conn.execute(
        "UPDATE source_fingerprints SET last_seen = ? WHERE fingerprint = ?",
        (now(), fp),
    )
    return True


def record_fingerprint(conn: sqlite3.Connection, source: str, source_id: str,
                       new_hash: str, canonical_id: str | None = None) -> None:
    """Upsert a source fingerprint, refreshing its hash and last-seen stamp."""
    fp = fingerprint(source, source_id)
    ts = now()
    conn.execute(
        """
        INSERT INTO source_fingerprints (fingerprint, canonical_id, content_hash,
                                         first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            content_hash = excluded.content_hash,
            last_seen    = excluded.last_seen,
            canonical_id = COALESCE(excluded.canonical_id, source_fingerprints.canonical_id)
        """,
        (fp, canonical_id, new_hash, ts, ts),
    )


# --------------------------------------------------------------------------
# Candidates (Stage 1 -> Stage 2 handoff)
# --------------------------------------------------------------------------

def add_candidate(conn: sqlite3.Connection, raw: dict[str, Any], priority: int,
                  relevance: dict[str, Any], avg: float) -> None:
    """Stash a keyword-passing, relevance-passing event for Stage 2."""
    cid = fingerprint(raw["source"], raw["source_id"])
    conn.execute(
        """
        INSERT OR REPLACE INTO candidates
            (id, source, source_id, priority, raw_json, relevance_json,
             relevance_avg, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid, raw["source"], raw["source_id"], priority,
            json.dumps(raw, default=str), json.dumps(relevance), avg,
            content_hash(raw.get("start_dt"), raw.get("venue_name"), raw.get("price")),
            now(),
        ),
    )


def take_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read every pending candidate. Stage 2 clears them once merged."""
    rows = conn.execute(
        "SELECT * FROM candidates ORDER BY priority ASC, relevance_avg DESC"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["raw"] = json.loads(item.pop("raw_json"))
        item["relevance"] = json.loads(item.pop("relevance_json") or "{}")
        out.append(item)
    return out


def clear_candidates(conn: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    conn.executemany("DELETE FROM candidates WHERE id = ?", [(i,) for i in ids])


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

# Only these persist. A key not listed here is silently dropped by
# upsert_event, which is why enrichment writes `transit_json` rather than
# `transit` — the near-miss cost a debugging round already.
_EVENT_FIELDS = (
    "title description start_dt end_dt venue_name venue_address venue_url "
    "lat lon neighborhood price register_url skill_level what_to_bring "
    "image_url image_alt transit_json venue_context_json content_type "
    "provenance_json sources_json "
    "relevance_json verified"
).split()

#: Columns added after the schema first shipped. `CREATE TABLE IF NOT EXISTS`
#: never touches an existing table, and this database is committed to the repo
#: and outlives every deploy, so a new column has to be added explicitly.
#: Additive only: SQLite can add a column but not drop one.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("events", "venue_context_json", "TEXT"),
    ("events", "content_type", "TEXT NOT NULL DEFAULT 'event'"),
    ("articles", "research_json", "TEXT"),
    ("articles", "voice_json", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column introduced after this database was created."""
    for table, column, decl in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            log.info("migrating %s: adding column %s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def upsert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    """Insert or update a canonical event. Returns its id.

    ``event`` uses plain Python values; the ``*_json`` columns accept either a
    pre-serialised string or a structure this will serialise.
    """
    event_id = event["id"]
    ts = now()
    row = {k: event.get(k) for k in _EVENT_FIELDS}
    for key in ("transit_json", "venue_context_json", "provenance_json",
                "sources_json", "relevance_json"):
        if row.get(key) is not None and not isinstance(row[key], str):
            row[key] = json.dumps(row[key])
    row["verified"] = int(bool(row.get("verified")))
    row["content_hash"] = content_hash(
        event.get("start_dt"), event.get("venue_name"), event.get("price")
    )

    cols = ["id"] + list(row.keys()) + ["created_at", "updated_at"]
    vals = [event_id] + list(row.values()) + [ts, ts]
    updates = ", ".join(f"{c} = excluded.{c}" for c in list(row.keys()) + ["updated_at"])
    conn.execute(
        f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        vals,
    )
    return event_id


def get_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Articles
# --------------------------------------------------------------------------

def create_article(conn: sqlite3.Connection, article_id: str, event_id: str,
                   relevance_avg: float, stage: str = "s3_validate") -> None:
    ts = now()
    conn.execute(
        """
        INSERT OR IGNORE INTO articles
            (id, event_id, stage, status, relevance_avg, created_at, updated_at)
        VALUES (?, ?, ?, 'queued', ?, ?, ?)
        """,
        (article_id, event_id, stage, relevance_avg, ts, ts),
    )


def update_article(conn: sqlite3.Connection, article_id: str, **fields: Any) -> None:
    """Patch an article row. Dict and list values are JSON-encoded automatically."""
    if not fields:
        return
    for key, value in list(fields.items()):
        if isinstance(value, (dict, list)):
            fields[key] = json.dumps(value)
    fields["updated_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE articles SET {assignments} WHERE id = ?",
        list(fields.values()) + [article_id],
    )


def get_article(conn: sqlite3.Connection, article_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return dict(row) if row else None


def queued_articles(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Next articles to work, highest relevance first.

    Highest-score-first can starve a low scorer until its event date passes.
    That is handled upstream by dropping events inside the useful lead window
    rather than by reordering here.
    """
    rows = conn.execute(
        """
        SELECT a.* FROM articles a
        JOIN events e ON e.id = a.event_id
        WHERE a.status IN ('queued', 'in_progress')
        ORDER BY a.relevance_avg DESC, e.start_dt ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def bump_attempt(conn: sqlite3.Connection, article_id: str, stage: str) -> int:
    """Increment and return the attempt counter for one stage of one article."""
    article = get_article(conn, article_id)
    if article is None:
        raise KeyError(article_id)
    attempts = json.loads(article["attempts_json"] or "{}")
    attempts[stage] = attempts.get(stage, 0) + 1
    update_article(conn, article_id, attempts_json=attempts)
    return attempts[stage]


def reset_attempts(conn: sqlite3.Connection, article_id: str, stage: str) -> None:
    """Zero one stage's counter. Called when a human approves a held article."""
    article = get_article(conn, article_id)
    if article is None:
        raise KeyError(article_id)
    attempts = json.loads(article["attempts_json"] or "{}")
    attempts.pop(stage, None)
    update_article(conn, article_id, attempts_json=attempts)


def hold(conn: sqlite3.Connection, article_id: str, stage: str, reason: str,
         doc_id: str | None = None) -> None:
    update_article(
        conn, article_id, status="held", hold_stage=stage,
        hold_reason=reason, hold_doc_id=doc_id,
    )
    log.warning("held %s at %s: %s", article_id, stage, reason)


def held_articles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM articles WHERE status = 'held' ORDER BY updated_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Published articles
# --------------------------------------------------------------------------

def record_published(conn: sqlite3.Connection, post_id: str, article_id: str,
                     event_id: str, url: str, slug: str,
                     event_start_dt: str | None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO published
            (post_id, article_id, event_id, url, slug, event_start_dt,
             state, published_at, last_checked_at)
        VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?)
        """,
        (post_id, article_id, event_id, url, slug, event_start_dt, now(), now()),
    )


def live_published(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Published articles whose events have not yet been marked ended or cancelled.

    The post-publish sweep walks these. It re-checks live status at the source,
    so it is not simply a date comparison.
    """
    rows = conn.execute(
        "SELECT * FROM published WHERE state = 'live' ORDER BY event_start_dt ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_published_state(conn: sqlite3.Connection, post_id: str, state: str,
                         banner: str | None = None) -> None:
    conn.execute(
        "UPDATE published SET state = ?, banner = ?, last_checked_at = ? WHERE post_id = ?",
        (state, banner, now(), post_id),
    )


# --------------------------------------------------------------------------
# Run log
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, processed: int = 0,
               published_count: int = 0, holds: int = 0, failures: int = 0,
               notes: str = "") -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, articles_processed = ?, published = ?,
                        holds = ?, failures = ?, notes = ?
        WHERE id = ?
        """,
        (now(), processed, published_count, holds, failures, notes, run_id),
    )
