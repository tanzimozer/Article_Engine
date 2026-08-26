"""Stage 4 — write the article.

The writer produces the hook line, 5-6 sections, the disclaimer and the
metadata. It does not produce the details box: that is assembled from verified
fields by :mod:`pipeline.ricos`, because a model retyping a date is a chance
to get the date wrong.

Transit is passed in as verified GTFS data and must be used as given. It is a
factual claim, so anything embellished here gets stripped in Stage 5 anyway.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from dateutil import parser as dateparser

from pipeline import roles, skills, state
from pipeline.errors import InfraFailure

log = logging.getLogger(__name__)

# Headroom reserved for the assembled details box, which counts toward the
# article's word ceiling but is not written by the writer. Six fields plus the
# transit line land between 25 and 45 words; the upper bound is used so a long
# venue name cannot push a passing draft over the limit.
DETAILS_BOX_WORD_ALLOWANCE = 45

#: Fraction of a draft that fact-checking typically removes. Every claim it
#: cannot verify against a source is deleted, so what the writer hands over is
#: not what reaches the panel -- 948 words became 457 on the first real run.
#: The writer's floor is raised by this fraction so a normal loss still lands
#: in band.
#:
#: Measured, in order: 52% when the event had no neighborhood and no transit,
#: 20% once geocoding resolved, 14% and 5% once venue_context gave the writer
#: cited material to build on. (A later 71% was a payload bug, not attrition --
#: the checker could not see the research trail, so every sourced fact read as
#: invention.) Stage 3b now hands the writer sourced facts with URLs attached,
#: which is the condition under which 5% was measured.
#:
#: 0.07 is chosen so the floor lands at 860, which is the length of the shipped
#: reference article in tanzimozer/Current-Article-Engine. Provisioning for the
#: starved-era 52% pushed this floor to 1000 -- above the proven standard --
#: and that gap is where the padding the judges kept flagging came from.
#:
#: If attrition spikes again the word gate in factcheck.py catches it and asks
#: for a rewrite, so erring low costs a retry rather than a bad article.
FACTCHECK_ATTRITION = 0.07


def _slugify(stem: str, start_dt: str | None) -> str:
    """Lowercase kebab-case, with the event date appended.

    The date suffix is load-bearing. One article per occurrence and no cooldown
    means a weekly run club produces near-identical titles all year, and Wix's
    auto-slug would collide on every one of them.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60]
    if start_dt:
        try:
            date_part = dateparser.parse(start_dt).strftime("%Y-%m-%d")
            return f"{stem}-{date_part}"
        except (ValueError, TypeError):
            pass
    return stem


def prose_words(draft: dict[str, Any]) -> int:
    """Words in the article's prose: hook line, section bodies, disclaimer.

    Every gate and every prompt must count the same thing. They did not: the
    gates counted section bodies alone while the prompt defined the band as
    "hook_line plus every section body plus disclaimer". That was harmless
    while the hook was capped at 30 words and became a 200-word discrepancy the
    moment the opening grew to match the reference article. Two consecutive
    drafts were thrown away for falling short -- 739 and 765 section words,
    both of which were 900-plus word articles that satisfied the band.

    Headings are excluded deliberately, matching the house rule that headings
    do not count toward length.
    """
    parts = [draft.get("hook_line") or ""]
    parts += [s.get("body", "") for s in draft.get("sections") or []]
    parts.append(draft.get("disclaimer") or "")
    return len(" ".join(parts).split())


def _revision_notes(article: dict[str, Any]) -> list[str]:
    """Judge and fact-check feedback from the previous attempt, if any.

    A rewrite with no notes just reproduces the same failure.
    """
    notes: list[str] = []
    for column in ("judges_json", "factcheck_json"):
        blob = article.get(column)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            for judge, verdict in data.items():
                if isinstance(verdict, dict) and not verdict.get("pass", True):
                    notes.extend(
                        f"[{judge}] {n}" for n in verdict.get("revision_notes", [])
                    )
    return notes


def run(conn: sqlite3.Connection, cfg: dict[str, Any],
        article_id: str) -> dict[str, Any]:
    article = state.get_article(conn, article_id)
    event = state.get_event(conn, article["event_id"])

    try:
        house = skills.voice_handbook(cfg)
    except skills.SkillsUnavailable as exc:
        # Fatal on purpose. Drafting without the voice handbook produces copy
        # the judges will reject, so failing here is cheaper than failing later.
        raise InfraFailure(f"voice handbook unavailable: {exc}") from exc

    article_cfg = cfg["settings"]["article"]

    # The writer's band is not the article's band. The ceiling sits below it to
    # leave room for the details box the writer never sees; the floor sits above
    # it to survive fact-check attrition. Both are computed once and used for
    # the payload and the gate, so the prompt and the code cannot drift apart --
    # they already had, and the looser number won: the prompt asked for 880 and
    # the gate accepted 870.
    prose_max = article_cfg["word_count_max"] - DETAILS_BOX_WORD_ALLOWANCE
    prose_min = round(article_cfg["word_count_min"] / (1 - FACTCHECK_ATTRITION))

    payload = {
        "event": {k: v for k, v in event.items() if not k.endswith("_json")},
        "transit": json.loads(event.get("transit_json") or "[]"),
        # Cited descriptions of the venue. This is the only material in the
        # payload that says what the place is actually like, and every entry
        # carries the URL it came from -- so the writer can ground a sentence
        # about terrain or history in something the fact-checker will keep.
        "venue_context": json.loads(event.get("venue_context_json") or "[]"),
        # The stage 3b fact trail: everything the pipeline went and *learned*
        # about this subject, each fact with its source and a confidence level.
        # Before this existed the writer could only restate the listing it was
        # handed, which is why drafts read as reformatted event listings.
        "research": json.loads(article.get("research_json") or "{}"),
        "constraints": {
            "word_count_min": prose_min,
            "word_count_max": prose_max,
            "sections_min": article_cfg["sections_min"],
            "sections_max": article_cfg["sections_max"],
            "meta_title_max": article_cfg["meta_title_max"],
            "meta_description_max": article_cfg["meta_description_max"],
            "no_em_dashes": True,
            "no_h3": True,
            "no_bullets": True,
            "no_faq": True,
            "no_byline": True,
        },
        "revision_notes": _revision_notes(article),
    }

    try:
        draft = roles.run_role(
            "writer", payload,
            extra_context=f"# VOICE HANDBOOK (authoritative)\n\n{house}",
            # The longest call in the pipeline, and the most variable.
            # Measured: 456s on a starved event, 658s and 962s once geocoding
            # and venue context filled the payload, 981s on a direct re-run of
            # the exact prompt that had just timed out three times at 1800s.
            # Median is ~16 minutes; the tail is much longer, and quota
            # throttling stretches it further. Sized for the tail, because a
            # timeout here costs three attempts and over an hour.
            timeout_s=3000,
        )
    except roles.RoleError as exc:
        raise InfraFailure(f"writer unavailable: {exc}") from exc

    def reject(reason: str) -> dict[str, Any]:
        """Reject the draft, keeping it in the database for inspection.

        A rejected draft used to be discarded, so a write-stage failure left
        no artifact to look at and no reason in the log -- an article could
        fail here three times running and leave nothing behind explaining why.
        The next attempt overwrites it, so keeping it costs nothing.
        """
        state.update_article(conn, article_id, draft_json=draft)
        log.info("%s draft rejected: %s", article_id, reason)
        return {"ok": False, "reason": reason, "retry_from": "s4_write"}

    sections = draft.get("sections") or []
    if not (article_cfg["sections_min"] <= len(sections) <= article_cfg["sections_max"]):
        return reject(
            f"{len(sections)} sections, expected "
            f"{article_cfg['sections_min']}-{article_cfg['sections_max']}"
        )

    words = prose_words(draft)
    body = " ".join(s.get("body", "") for s in sections)

    if not (prose_min <= words <= prose_max):
        return reject(
            f"{words} words of prose, expected {prose_min}-{prose_max} "
            f"(floor raised for fact-check attrition, ceiling leaves room "
            f"for the {DETAILS_BOX_WORD_ALLOWANCE}-word details box)"
        )

    if "—" in body or "—" in draft.get("hook_line", ""):
        return reject("em-dash present, hard fail on this surface")

    slug = _slugify(draft.get("slug_stem", ""), event.get("start_dt"))
    state.update_article(conn, article_id, draft_json=draft, slug=slug)
    log.info("%s drafted: %d words, %d sections", article_id, words, len(sections))
    return {"ok": True}
