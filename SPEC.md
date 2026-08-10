# Article Engine — Specification

Automated pipeline that finds upcoming Seattle fitness, wellness, and lifestyle
events and publishes SEO articles about them to **seattlefitnessmag.com**.

Every decision below is locked. Change them here first, then in code.

---

## 1. Pipeline

Nine stages. An article moves through them one at a time.

| # | Stage | Executor |
|---|-------|----------|
| 1 | Gather topics | Script (fetch/parse) + LLM (relevance scoring) |
| 2 | Clean & organize | Script (schema map, enrich) + LLM (dedupe) |
| 3 | Validate event data | LLM |
| 4 | Write editorial blog | LLM |
| 5 | Fact-check draft | LLM |
| 6 | Judge panel | LLM × 7 dimensions |
| 7 | Forward to Editor in Chief | LLM |
| 8 | EIC adjustments + final gate | LLM |
| 9 | Schedule & publish | LLM (slot) + Script (publish, notify) |

**The rule that decided the split:** fixed retrieval steps go to code, judgment
steps go to a model. Routing is fully predetermined at every branch, which is
why a Python script owns control flow rather than an agent framework.

### Stage 1 — Gather

- Sources: HTML scrape, RSS feeds, Eventbrite free tier
- Window: events starting 7–10 days out
- Keyword pre-filter: fitness + wellness + lifestyle (`config/keywords.yaml`)
- Relevance Scorer scores survivors on category fit, audience fit, timeliness —
  equal weight, 1–10 each, passes at **≥ 7 average**
- Every qualifying event becomes its own article

### Stage 2 — Clean & organize

- Script maps all sources onto one schema
- Dedupe Agent matches the same event across sources
- Merged record keeps **per-field provenance tags**, because Stage 3 needs to
  know which source each field came from
- Conflict priority: `eventbrite > venue_site > visitseattle > aggregator > reddit`
- Script enriches: geocode, neighborhood, image, transit

### Stage 3 — Validate

- Re-fetch the original source, confirm data unchanged
- Cross-check against other sources
- Must match on **date + venue + price**
- Requires 2+ sources agreeing
- **Exception:** a single source passes if it is Eventbrite or the official venue site
- Must cite every URL checked
- Anything unverified is held for human review

### Stage 4 — Write

See §2 for the article contract.

### Stage 5 — Fact-check

- Strips any claim not traceable to verified source data
- **Zero medical or health claims.** The article describes the event, nothing more.
- Logs every change with a reason
- Auto-revises and re-checks, max 3 loops, then holds for human

### Stage 6 — Judge panel

Seven dimensions. **All must pass.** Judges score independently with no
visibility into each other's verdicts.

Six inherited from `Skill-Cabinet/timbr_eval_v2/RUBRIC.md`:

1. `voice_brand_compliance`
2. `structural_format_compliance`
3. `editorial_value`
4. `factual_venue_integrity`
5. `seattle_local_specificity`
6. `ai_pattern_detection`

Plus one added here, because the inherited rubric has no SEO dimension:

7. `seo` — heading structure, keyword placement, meta title and description, internal links

Threshold **≥ 8**. Any fail routes back to Stage 4/5 for targeted revision,
max 3 loops, then holds for human.

Every score below the maximum requires a quoted excerpt as evidence. A score
with no receipt is unauditable.

### Stages 7–8 — Editor in Chief

Runs `Skill-Cabinet/skills/timbr/magazine-eic/SKILL.md`. Receives the article
plus full pipeline history: judge scores, revision count, source verification
status.

**Deviation from that skill:** it states *"Never publish without the owner's go."*
This pipeline auto-publishes when all gates pass. That override is deliberate.

### Stage 9 — Schedule & publish

- Slot Scheduler targets **5–7 days before the event**
- Weighs queue spacing and reader activity
- Reader-activity model starts from research and moves to Wix analytics once
  traffic history exists
- Script publishes to Wix and emails the admin

---

## 2. Article contract

```
[one hook line]

**When:** Saturday, August 22, 9:00 AM
**Where:** Gas Works Park, 2101 N Northlake Way
**Cost:** $35
**Register:** gasworksrun.com/signup
**Skill level:** All paces welcome
**What to bring:** Water, layers for wind off the lake

## [section heading]
[one continuous paragraph]

...5-6 sections total...

[disclaimer line]
```

### Structure

- **Hook line** — one line. No TL;DR block above it.
- **Details box** — sits between the hook and the first H2, *outside any section*.
  This placement is load-bearing: it keeps the box invisible to the
  one-paragraph-per-H2 check in `hardgate.py`, so no gate exemption is needed.
- **Sections** — 5–6 H2s, free-form titles, one continuous paragraph each
- **No H3 anywhere**
- **Disclaimer** — one line at the bottom

### Details box

Bold label + value, one per line. Not bullets, not a table.

Fields in order: `When`, `Where`, `Cost`, `Register`, `Skill level`, `What to bring`.

The `Register` line is **omitted entirely** when an event has no registration page.

### Length

**800–1200 words**, body only, headings stripped. Inherited from the existing
gate. Do not use published posts as a length reference — TIMBR's early articles
ran about half this.

### Metadata

| Field | Rule |
|-------|------|
| Meta title | 60 chars for the title itself, then ` \| Seattle Fitness Mag`. ~82 total. Google truncating the visible suffix is accepted. |
| Meta description | 155 chars. **Must carry the date and the neighborhood.** |
| Slug | Controlled, with a date suffix: `gas-works-5k-2026-08-22` |

The date suffix is not cosmetic. Recurring events produce near-identical titles,
and Wix's auto-slug would collide on them.

### Images

- Featured image **required**
- First choice: a photo from the venue's own site
- Fallback: curated free-stock neighborhood image (`config/neighborhood_images.json`)
- Alt text is descriptive, and states the image is **illustrative** whenever it
  is not the real venue

Alt text that reads as a documentary claim about a real business fails the
factual gate. This is an inherited precedent, not a new rule.

### Location

- **Address text only.** No map embed, no map link.
- **Transit always included**, sourced from King County Metro and Sound Transit
  GTFS feeds
- **No parking.** No authoritative free source exists, and the Fact-Checker
  would strip anything unverified.

Transit is a factual claim. It cannot come from model knowledge.

### Price

Always exact, taken from the highest-priority source. This deliberately
overrides the house rule of printing less when sources conflict.

### Links

1–2 internal links, only when a genuinely related post exists. Skipped silently
when nothing fits.

### Excluded

No FAQ · no JSON-LD schema of any kind · no H3 · no TL;DR · no map · no parking ·
no byline — the publication signs as the brand.

### Em-dashes

**Total ban.** Any em-dash anywhere is a hard fail on this surface. The print
line's one-per-sentence allowance does not apply here.

---

## 3. Post-publish sweep

Runs as a step in every scheduled job, alongside the approval sweep.

- Event date passed → inject an "ended" banner. Article stays up and keeps its URL.
- Event cancelled or sold out → same banner mechanism

This means the sweep re-checks live event status at the source. It is not just
a date comparison.

**Recurrence:** one article per occurrence, **no cooldown**. A weekly run club
generates roughly 52 near-identical articles a year. This risk was accepted
explicitly. The date-suffixed slug is what keeps their URLs distinct.

---

## 4. Orchestration

**Python owns control flow.** The ten LLM roles run through headless Claude Code
(`claude -p`), not direct Anthropic API calls, so usage bills against the Max
subscription rather than pay-as-you-go.

### State

SQLite, committed to this repo every run. It is the source of truth for stage
position, attempt counters, hold flags, and the processed-events registry.

Committing it also defeats GitHub's 60-day inactivity auto-disable of scheduled
workflows — with state living elsewhere, nothing would commit and the cron would
silently stop.

Google Drive holds human-facing artifacts and the hold queue.

### Hold queue

Native Google Docs. Approve by editing a `STATUS` line inside the doc. Swept
twice daily.

### Routing

- **Sequential**, one article at a time through Stages 3–9
- Approved articles **resume at the stage that held them**, attempt counter reset to 0
- **Max 2 re-holds**, then archived and the admin is notified
- **Infrastructure failures** (API 429/500, a scrape breaking, Wix rejecting a
  publish) are a different class from content failures: retry 3× with
  exponential backoff, then requeue for the next run
- A **source going down** at Stage 1 does not stop the run. Continue with partial
  coverage, log it, notify.

### Idempotency

Two levels:

1. **Source fingerprint** — skips repeats at Stage 1, *before* any LLM cost
2. **Canonical ID** — assigned post-dedupe at Stage 2

Re-runs only when date, venue, or price changed at the source.

Without this, the same event would be re-gathered, re-scored, and re-written
every single day it sits in the 7–10 day window.

---

## 5. Schedule

- **Host:** GitHub Actions in this repo
- **Billing:** `CLAUDE_CODE_OAUTH_TOKEN` in repo secrets, from `claude setup-token`.
  Runs bill against Claude Max, not API rates.
- **Never set `ANTHROPIC_API_KEY`.** Claude Code prioritizes an API key env var
  over subscription auth, so setting it silently switches billing to
  pay-as-you-go.
- **Cadence:** one workflow, 2×/day at 8am and 8pm PT
- **Order:** approval sweep → post-publish sweep → pipeline. A single job
  removes any chance of two jobs clobbering the state file.
- Cron is UTC-only and drifts an hour across DST. Irrelevant at this cadence.

### Throughput guards

- **5 articles per run**, plus a hard timeout. Leftovers requeue.
- Highest relevance score processed first
- Events that fall outside the useful lead window are **dropped**, not left to
  starve in the queue

Private repo, so 2,000 free Actions minutes a month — roughly 33 minutes per run.

---

## 6. Inherited from Skill-Cabinet

Read at run time from `github.com/tanzimozer/Skill-Cabinet` (public, no token
needed). One source of truth means a voice fix propagates across every TIMBR
surface.

| Path | What it gives us |
|------|------------------|
| `skills/timbr/magazine-eic/SKILL.md` | The Editor-in-Chief harness |
| `skills/timbr/magazine-eic/references/house.md` | The voice handbook |
| `timbr_eval_v2/hardgate.py` | Banned vocab, banned phrases, word-count ranges, structure checks |
| `timbr_eval_v2/RUBRIC.md` | The judge contract |
| `skills/integrations/wix-api-operations/SKILL.md` | Wix publish mechanics |

Those skills carry macOS paths. Actions runs on Linux, so paths resolve
relative to the checkout.

---

## 7. Sites

Three exist. Only one is a publish target.

| Site | ID | Role |
|------|----|------|
| seattlefitnessmag.com | `25296528-c352-482d-9b5b-e143b426d2cd` | **Publish target** |
| TIMBR-3 | `ab465896-e5c3-4f5d-bc9d-7f495a6d6be1` | Product/store site. Not used here. |
| timbr.fit | `f916c8b1-134a-4691-9241-5a14bf849078` | **Never touch.** |
