"""Orchestrator.

Control flow lives here, in plain Python, because every branch in this pipeline
is predetermined. Nothing needs a model to decide what happens next — the
models make judgments, this file decides where those judgments go.

Run order for a single invocation:

1. Approval sweep    — pick up anything the human cleared in the Drive hold queue
2. Post-publish sweep — banner events that ended, were cancelled, or sold out
3. Gather + clean    — find new events, dedupe, enrich
4. Article loop      — walk queued articles through stages 3-9, one at a time

The three sweeps and the pipeline share one job deliberately. Two jobs would
mean two processes racing on the same state file, and the loser's work would
vanish.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline import drive, notify, state
from pipeline.errors import Held, InfraFailure
from pipeline.stages import (
    clean,
    eic,
    factcheck,
    gather,
    judges,
    publish,
    research,
    sweep,
    validate,
    voice,
    write,
)

log = logging.getLogger("article_engine")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Stages 3-9. Stage 1 and 2 are batch operations that run once per invocation,
# not per article, so they sit outside this list.
STAGE_ORDER = [
    "s3_validate",
    "s3b_research",
    "s4_write",
    "s4b_voice",
    "s5_factcheck",
    "s6_judges",
    "s7_eic",
    "s9_publish",
]

STAGE_FN = {
    "s3_validate": validate.run,
    "s3b_research": research.run,
    "s4_write": write.run,
    "s4b_voice": voice.run,
    "s5_factcheck": factcheck.run,
    "s6_judges": judges.run,
    "s7_eic": eic.run,
    "s9_publish": publish.run,
}


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for name in ("settings", "sources", "keywords"):
        path = CONFIG_DIR / f"{name}.yaml"
        with path.open(encoding="utf-8") as fh:
            cfg[name] = yaml.safe_load(fh)
    return cfg


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def _hold_article(conn, cfg: dict[str, Any], article_id: str, stage: str,
                  reason: str) -> None:
    """Park an article for human review.

    Creates the Drive doc first so its id can be stored alongside the hold, and
    emails either way. If Drive is unconfigured the article still holds in the
    database — it just has to be cleared some other way.
    """
    article = state.get_article(conn, article_id) or {}
    draft = json.loads(article.get("draft_json") or "{}")

    body_parts = [draft.get("hook_line", "")]
    for section in draft.get("sections", []):
        body_parts.append(f"\n## {section.get('heading', '')}\n\n{section.get('body', '')}")
    body_parts.append(f"\n{draft.get('disclaimer', '')}")

    title = draft.get("meta_title") or article_id
    doc_id = None
    try:
        doc_id = drive.create_hold_doc(
            cfg, article_id, title, stage, reason, "\n".join(body_parts)
        )
    except Exception:
        log.exception("could not create hold doc for %s", article_id)

    state.hold(conn, article_id, stage, reason, doc_id)
    doc_url = f"https://docs.google.com/document/d/{doc_id}" if doc_id else None
    notify.held(cfg, article_id, stage, reason, doc_url)


def process_article(conn, cfg: dict[str, Any], article: dict[str, Any]) -> str:
    """Walk one article from its current stage to publication.

    Returns one of ``published``, ``held``, ``scheduled``, ``requeued``.
    """
    article_id = article["id"]
    thresholds = cfg["settings"]["thresholds"]
    max_attempts = thresholds["max_revise_attempts"]

    start_index = STAGE_ORDER.index(article["stage"]) if article["stage"] in STAGE_ORDER else 0
    state.update_article(conn, article_id, status="in_progress")

    for stage_name in STAGE_ORDER[start_index:]:
        state.update_article(conn, article_id, stage=stage_name)
        fn = STAGE_FN[stage_name]

        try:
            outcome = fn(conn, cfg, article_id)
        except InfraFailure as exc:
            log.error("infra failure in %s for %s: %s", stage_name, article_id, exc)
            state.update_article(conn, article_id, status="queued")
            return "requeued"
        except Held as exc:
            _hold_article(conn, cfg, article_id, exc.stage, exc.reason)
            return "held"

        if outcome.get("ok"):
            continue

        reason = outcome.get("reason", "stage returned not-ok")

        # A killed assignment is finished, not failed. Research returns this
        # when a subject has no verifiable angle at all; retrying would spend
        # three more attempts rediscovering that there is nothing to write.
        if outcome.get("drop"):
            log.info("%s dropped at %s: %s", article_id, stage_name, reason)
            return "dropped"

        # Content failure. Revise and retry, up to the cap, then hand to a human.
        attempt = state.bump_attempt(conn, article_id, stage_name)
        if attempt >= max_attempts:
            _hold_article(conn, cfg, article_id, stage_name,
                          f"failed {attempt} attempts: {reason}")
            return "held"

        # Judge and fact-check failures route back to the writer with notes,
        # rather than re-running the failing stage against unchanged text.
        retry_from = outcome.get("retry_from", "s4_write")
        # The reason travels with the hold path but used to be dropped here,
        # on the far more common requeue path -- leaving a log that said an
        # article failed without ever saying why.
        log.info("%s failed %s (attempt %d), routing back to %s: %s",
                 article_id, stage_name, attempt, retry_from, reason)
        state.update_article(conn, article_id, stage=retry_from, status="queued")
        return "requeued"

    return "published"


def run(cfg: dict[str, Any], *, skip_sweeps: bool = False,
        skip_gather: bool = False) -> int:
    started = time.monotonic()
    settings = cfg["settings"]["pipeline"]
    timeout_s = settings["run_timeout_seconds"]
    cap = settings["max_articles_per_run"]

    processed = published_count = holds = failures = 0

    with state.connect() as conn:
        run_id = state.start_run(conn)

        if not skip_sweeps:
            try:
                approved = sweep.run_approval_sweep(conn, cfg)
                log.info("approval sweep: %d article(s) cleared", approved)
            except Exception:
                log.exception("approval sweep failed; continuing")
                failures += 1

            try:
                banners = sweep.run_postpublish_sweep(conn, cfg)
                log.info("post-publish sweep: %d banner(s) applied", banners)
            except Exception:
                log.exception("post-publish sweep failed; continuing")
                failures += 1

        if not skip_gather:
            try:
                found = gather.run(conn, cfg)
                log.info("gather: %d qualifying event(s)", found)
            except Exception:
                log.exception("gather failed; continuing with existing queue")
                failures += 1

            try:
                cleaned = clean.run(conn, cfg)
                log.info("clean: %d event(s) ready", cleaned)
            except Exception:
                log.exception("clean failed; continuing with existing queue")
                failures += 1

        for article in state.queued_articles(conn, cap):
            if time.monotonic() - started > timeout_s:
                log.warning("run timeout reached, %d article(s) requeued",
                            cap - processed)
                break

            article_id = article["id"]
            log.info("--- article %s (relevance %.1f) ---",
                     article_id, article["relevance_avg"] or 0.0)
            try:
                result = process_article(conn, cfg, article)
            except Exception:
                log.exception("unhandled error on %s", article_id)
                state.update_article(conn, article_id, status="queued")
                failures += 1
                continue

            processed += 1
            if result == "published":
                published_count += 1
            elif result == "held":
                holds += 1

        state.finish_run(
            conn, run_id,
            processed=processed, published_count=published_count,
            holds=holds, failures=failures,
        )

    log.info("run complete: %d processed, %d published, %d held, %d failure(s)",
             processed, published_count, holds, failures)
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seattle events article pipeline")
    parser.add_argument("--skip-sweeps", action="store_true",
                        help="skip the approval and post-publish sweeps")
    parser.add_argument("--skip-gather", action="store_true",
                        help="work the existing queue without fetching new events")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every stage but never write to Wix")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    cfg["dry_run"] = args.dry_run
    if args.dry_run:
        log.warning("DRY RUN: nothing will be published")

    return run(cfg, skip_sweeps=args.skip_sweeps, skip_gather=args.skip_gather)


if __name__ == "__main__":
    raise SystemExit(main())
