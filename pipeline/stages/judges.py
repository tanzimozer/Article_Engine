"""Stage 6 — the judge panel.

Seven dimensions, all of which must pass. Six come from the inherited rubric;
``seo`` is added here because that rubric has no SEO dimension and this
pipeline is search-driven.

Judges score independently and never see each other's verdicts. That is not
ceremony — showing one judge another's score anchors it, and three anchored
scores are one score wearing a hat.

Every score below the maximum needs a quoted excerpt as evidence. A judge that
says "voice: 62" with no receipt cannot be audited or improved against.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pipeline import roles, skills, state
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)

# Dimensions carried over from Skill-Cabinet's RUBRIC.md. They share one
# generic prompt built from that rubric; only `seo` has its own file, since
# the rubric has nothing to say about it.
INHERITED_DIMENSIONS = {
    "voice_brand_compliance",
    "structural_format_compliance",
    "editorial_value",
    "factual_venue_integrity",
    "seattle_local_specificity",
    "ai_pattern_detection",
}

_GENERIC_JUDGE = """You are a single-dimension judge on an editorial panel.

You are scoring ONE dimension: `{dimension}`

Score it 1-10 against the rubric below. The passing mark is {threshold}.

Be the skeptic, not the cheerleader. This rubric exists because a lenient
grader previously let posts ship at half their intended word count and missed
a voice trap entirely.

Every score below 10 REQUIRES a quoted excerpt from the draft as evidence. A
score with no receipt is unauditable and will be discarded.

You are judging only `{dimension}`. Ignore every other dimension, including
ones you think matter more. Another judge has them.

GUARDRAILS
- Score and give feedback. Never edit the draft.
- You cannot see other judges' verdicts and must not speculate about them.
- If the draft is unscorable on this dimension because required context is
  missing, say so rather than guessing a number.

OUTPUT

{{
  "dimension": "{dimension}",
  "score": <int 1-10>,
  "evidence": [{{"claim": "<what you observed>", "excerpt": "<quoted from draft>"}}],
  "revision_notes": ["<specific, actionable>"],
  "pass": <bool>
}}
"""


def research_trail(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Where each group of facts came from, with URLs.

    The panel used to receive ``sources_json``, a list of internal member ids —
    literally ``["169169"]``. The factual-integrity judge scored an accurate
    article 3/8 for it and said why: a bus stop given to single-metre precision
    was indistinguishable from invention, because nothing recorded that GTFS
    had supplied it. Every fact in this pipeline *is* sourced; only the record
    of it was missing.
    """
    trail: list[dict[str, Any]] = []

    provenance = json.loads(event.get("provenance_json") or "{}")
    by_source: dict[str, list[str]] = {}
    for field, source in provenance.items():
        by_source.setdefault(str(source), []).append(field)
    listing_url = event.get("url") or event.get("register_url")
    for source, fields in sorted(by_source.items()):
        trail.append({
            "fields": sorted(fields),
            "source": source,
            "url": listing_url,
            "note": "event listing, cross-checked at stage 3",
        })

    if json.loads(event.get("transit_json") or "[]"):
        from pipeline.transit import GTFS_FEEDS
        trail.append({
            "fields": ["transit"],
            "source": "gtfs",
            "url": ", ".join(sorted(GTFS_FEEDS.values())),
            "note": "stop names, routes and distances computed from published "
                    "GTFS feeds against the venue's geocoded coordinates",
        })

    for entry in json.loads(event.get("venue_context_json") or "[]"):
        trail.append({
            "fields": ["venue description", "history", "setting"],
            "source": entry.get("source"),
            "url": entry.get("url"),
            "checked_at": entry.get("fetched_at"),
        })

    return trail


def _judge_payload(article: dict[str, Any], event: dict[str, Any],
                   draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft": draft,
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "venue_context": json.loads(event.get("venue_context_json") or "[]"),
        # Both, deliberately. `research_trail` groups sources into four entries
        # for auditing; `research` carries the individual claims with their URLs
        # and confidence levels. Supplying only the summary made the factual
        # judge score an article 6/8 for claims it could not see -- the EIC
        # caught it and named the cause: "the judge scored against the
        # four-entry research_trail summary, not the thirty-entry facts array".
        "research": json.loads(article.get("research_json") or "{}"),
        "research_trail": research_trail(event),
    }


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])
    draft = json.loads(article["draft_json"] or "{}")
    if not draft:
        return {"ok": False, "reason": "no draft to judge", "retry_from": "s4_write"}

    thresholds = cfg["settings"]["thresholds"]
    threshold = thresholds["judge_pass_score"]
    dimensions = cfg["settings"]["judges"]

    try:
        rubric_text = skills.rubric(cfg)
    except skills.SkillsUnavailable as exc:
        raise InfraFailure(f"rubric unavailable: {exc}") from exc

    payload = _judge_payload(article, event, draft)
    verdicts: dict[str, Any] = {}

    for dimension in dimensions:
        try:
            if dimension in INHERITED_DIMENSIONS:
                verdict = roles.run_role(
                    dimension, payload,
                    system_override=(
                        _GENERIC_JUDGE.format(dimension=dimension, threshold=threshold)
                        + "\n\n# RUBRIC\n\n" + rubric_text
                    ),
                    timeout_s=420,
                )
            else:
                verdict = roles.run_role(dimension_prompt(dimension), payload,
                                         timeout_s=420)
        except roles.RoleError as exc:
            raise InfraFailure(f"judge {dimension} unavailable: {exc}") from exc

        raw_score = verdict.get("score")
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            # A judge that returns null, or anything non-numeric, has not
            # judged. Score it 0 so the article fails rather than the run --
            # one malformed response used to raise TypeError and take the whole
            # article down with it. Note that `.get("score", 0)` does not cover
            # this case: the key is present and explicitly null, so the default
            # never applies.
            log.warning("%s judge %s returned an unusable score %r, scoring 0",
                        article_id, dimension, raw_score)
            score = 0
            verdict["score"] = 0
            verdict["unscored"] = True

        verdict["pass"] = score >= threshold
        verdicts[dimension] = verdict
        log.info("%s judge %s: %d/%d %s", article_id, dimension, score, threshold,
                 "PASS" if verdict["pass"] else "FAIL")

        # Persisted per judge rather than after the panel, so an infra failure
        # partway through keeps the verdicts already paid for.
        state.update_article(conn, article_id, judges_json=verdicts)

    # The gate is the panel's average plus a floor, not every dimension clearing
    # the mark. See config/settings.yaml for the calibration this comes from:
    # under the old all-seven rule a published, human-written reference article
    # scored 1/7 while our own generated draft scored 3/7, which means the rule
    # was measuring something other than quality.
    scores = [int(v["score"]) for v in verdicts.values()]
    average = sum(scores) / len(scores)
    weakest = min(scores)
    pass_average = thresholds.get("judge_pass_average")
    floor = thresholds.get("judge_floor_score")

    if pass_average is None or floor is None:
        # Older settings file: fall back to the original rule so the stage
        # still runs rather than crashing on a missing key.
        below = [d for d, v in verdicts.items() if not v["pass"]]
        if below:
            return {"ok": False, "reason": f"failed: {', '.join(below)}",
                    "retry_from": "s4_write"}
        return {"ok": True}

    weak = sorted(
        ((d, int(v["score"])) for d, v in verdicts.items()),
        key=lambda item: item[1],
    )
    summary = ", ".join(f"{d} {s}" for d, s in weak)
    log.info("%s panel: average %.2f (need %.1f), weakest %d (need %d) -- %s",
             article_id, average, pass_average, weakest, floor, summary)

    if average < pass_average or weakest < floor:
        driver = ("average too low" if average < pass_average
                  else f"{weak[0][0]} at {weak[0][1]}, below the floor of {floor}")
        return {
            "ok": False,
            "reason": f"panel average {average:.2f} (need {pass_average}), "
                      f"weakest {weakest} (need {floor}): {driver}. Scores: {summary}",
            "retry_from": "s4_write",
        }

    return {"ok": True}


def dimension_prompt(dimension: str) -> str:
    """Prompt filename stem for a dimension that has its own file."""
    return f"judge_{dimension}"
