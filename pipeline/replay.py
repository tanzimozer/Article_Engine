"""Run one stage against stored state, without walking the pipeline.

Every observation in this pipeline used to cost a full end-to-end run: 35 to 60
minutes to see whether a one-line change worked. That is why debugging it felt
like a loop. A missing key in the fact-checker payload took fifty minutes to
surface and one minute to fix.

Nothing about accuracy changes here. Same stage function, same prompts, same
model, same config. The only thing skipped is re-running upstream stages whose
output is already sitting in the database.

    # what state is everything in
    python -m pipeline.replay --list

    # keep a copy of the article before experimenting
    python -m pipeline.replay --article art_x --snapshot before-gate-change

    # run one stage, repeatedly, against identical input
    python -m pipeline.replay --article art_x --stage s6_judges

    # put it back and try a different change
    python -m pipeline.replay --article art_x --restore before-gate-change

    # show what a stage would actually be sent, and spend nothing
    python -m pipeline.replay --article art_x --stage s5_factcheck --payload
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from pipeline import state
from pipeline.errors import Held, InfraFailure
from pipeline.main import STAGE_FN, STAGE_ORDER, load_config, setup_logging

log = logging.getLogger("replay")

SNAPSHOT_TABLE = """
CREATE TABLE IF NOT EXISTS replay_snapshots (
    name        TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    article     TEXT NOT NULL,
    event       TEXT NOT NULL,
    taken_at    TEXT NOT NULL,
    PRIMARY KEY (name, article_id)
);
"""

#: Stages whose payload can be shown without calling a model, so a payload bug
#: costs nothing to find. Each returns the dict that stage would send.
_PAYLOAD_PREVIEW = {
    "s5_factcheck": lambda a, e: {
        "draft": json.loads(a.get("draft_json") or "{}"),
        "verified_source_data": {
            **{k: v for k, v in e.items() if not k.endswith("_json")},
            "transit": json.loads(e.get("transit_json") or "[]"),
            "venue_context": json.loads(e.get("venue_context_json") or "[]"),
            "research": json.loads(a.get("research_json") or "{}"),
        },
    },
    "s6_judges": lambda a, e: {
        "draft": json.loads(a.get("draft_json") or "{}"),
        "event": {k: v for k, v in e.items() if not k.endswith("_json")},
        "venue_context": json.loads(e.get("venue_context_json") or "[]"),
        "research_trail": __import__(
            "pipeline.stages.judges", fromlist=["research_trail"]
        ).research_trail(e),
    },
}


def _rows(conn) -> list[dict[str, Any]]:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)")]
    return [dict(zip(cols, r)) for r in conn.execute("SELECT * FROM articles")]


def cmd_list(conn) -> int:
    articles = _rows(conn)
    if not articles:
        print("no articles in state")
        return 0
    print(f"{'article':22} {'stage':16} {'status':10} {'draft':6} {'research':9} attempts")
    for a in articles:
        draft = json.loads(a.get("draft_json") or "{}")
        words = sum(len(s.get("body", "").split()) for s in draft.get("sections") or [])
        trail = json.loads(a.get("research_json") or "{}")
        facts = len(trail.get("facts") or [])
        print(f"{a['id']:22} {a['stage']:16} {a['status']:10} "
              f"{(str(words) + 'w') if words else '-':6} "
              f"{(str(facts) + ' facts') if facts else '-':9} {a['attempts_json']}")
    return 0


def cmd_snapshot(conn, article_id: str, name: str) -> int:
    conn.executescript(SNAPSHOT_TABLE)
    article = state.get_article(conn, article_id)
    if not article:
        print(f"no such article: {article_id}", file=sys.stderr)
        return 1
    event = state.get_event(conn, article["event_id"])
    conn.execute(
        "INSERT OR REPLACE INTO replay_snapshots "
        "(name, article_id, article, event, taken_at) VALUES (?, ?, ?, ?, ?)",
        (name, article_id, json.dumps(dict(article)), json.dumps(dict(event)), state.now()),
    )
    print(f"snapshot '{name}' taken for {article_id}")
    return 0


def cmd_restore(conn, article_id: str, name: str) -> int:
    conn.executescript(SNAPSHOT_TABLE)
    row = conn.execute(
        "SELECT article, event FROM replay_snapshots WHERE name = ? AND article_id = ?",
        (name, article_id),
    ).fetchone()
    if row is None:
        print(f"no snapshot '{name}' for {article_id}", file=sys.stderr)
        return 1
    article = json.loads(row["article"])
    event = json.loads(row["event"])
    state.upsert_event(conn, event)
    state.update_article(
        conn, article_id,
        **{k: v for k, v in article.items()
           if k not in ("id", "event_id", "created_at", "updated_at")},
    )
    print(f"restored '{name}' onto {article_id} (stage {article['stage']}, "
          f"status {article['status']})")
    return 0


def cmd_payload(conn, article_id: str, stage: str) -> int:
    preview = _PAYLOAD_PREVIEW.get(stage)
    if preview is None:
        print(f"no payload preview for {stage}; available: "
              f"{', '.join(sorted(_PAYLOAD_PREVIEW))}", file=sys.stderr)
        return 1
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    payload = preview(dict(article), dict(event))
    text = json.dumps(payload, indent=2, default=str)
    print(f"--- {stage} payload for {article_id}: {len(text)} chars ---")

    def show(mapping: dict[str, Any], indent: str = "  ") -> None:
        """One level of nesting, because that is where the bugs hide.

        The fact-checker silently lost every researched claim because
        ``research`` was missing from inside ``verified_source_data``. A
        top-level summary would have shown one healthy-looking blob.
        """
        for key, value in mapping.items():
            size = len(json.dumps(value, default=str))
            empty = "  <-- EMPTY" if value in ({}, [], None, "") else ""
            print(f"{indent}{key:24} {size:8} chars{empty}")
            if isinstance(value, dict) and indent == "  ":
                show(value, indent + "    ")

    show(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run one pipeline stage against stored state")
    p.add_argument("--article")
    p.add_argument("--stage", choices=STAGE_ORDER)
    p.add_argument("--list", action="store_true")
    p.add_argument("--snapshot", metavar="NAME")
    p.add_argument("--restore", metavar="NAME")
    p.add_argument("--payload", action="store_true",
                   help="print what the stage would be sent, and call nothing")
    p.add_argument("--live", action="store_true",
                   help="allow a stage to write to the real site. Off by default: "
                        "s9_publish otherwise runs dry.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)

    with state.connect() as conn:
        if args.list:
            return cmd_list(conn)
        if not args.article:
            p.error("--article is required unless --list")
        if args.snapshot:
            return cmd_snapshot(conn, args.article, args.snapshot)
        if args.restore:
            return cmd_restore(conn, args.article, args.restore)
        if not args.stage:
            p.error("--stage is required")
        if args.payload:
            return cmd_payload(conn, args.article, args.stage)

        cfg = load_config()
        # Dry-run by default, and loudly. This is a debugging tool: running
        # s9_publish here once attempted a real write to the live magazine and
        # was stopped only by absent credentials. A tool for poking at stages
        # must not be one keystroke away from publishing unreviewed copy.
        cfg["dry_run"] = not args.live
        if args.live:
            log.warning("--live: %s may write to the real site", args.stage)
        started = time.monotonic()
        try:
            outcome = STAGE_FN[args.stage](conn, cfg, args.article)
        except (InfraFailure, Held) as exc:
            print(f"{args.stage}: {type(exc).__name__}: {exc}")
            return 1
        print(f"\n{args.stage} finished in {time.monotonic() - started:.0f}s")
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
