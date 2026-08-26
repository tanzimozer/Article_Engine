# SFM Article Engine — merge spec

Merging `timbr-event-creator` (design only) into `Article_Engine` (built).
Article_Engine is the skeleton. TEC ideas are grafted in only where they earn new code.

Decisions here are **not yet folded into `SPEC.md` or `settings.yaml`**. That happens once
all nine stages are agreed.

Status: **all nine stages locked, plus the SFM Score and the post-publish sweep.**
Open work is listed at the end.

---

## Stage 1 — Collection

### S1-1 · Scope = events + manual drops + all five TEC categories
Events are auto-collected as today. The four non-event categories arrive through a new
manual drop path. No new automated collectors in v1.

Categories: `events` · `health-tech` · `gyms-studios` · `food-nutrition` · `supplements`

Rejected: events-only (too narrow); building non-event collectors now (needs paid APIs).

### S1-2 · Three article types, not five
The four non-event categories collapse into two shapes:

| Type | Categories | Shape |
|---|---|---|
| `event` | events | one venue, one date, details box, transit — **built** |
| `place-roundup` | gyms-studios, food-nutrition | many venues, addresses, transit, no date — **new** |
| `product-roundup` | health-tech, supplements | no venue, no transit, affiliate links — **new** |

### S1-3 · Manual drop is the collector for non-event categories
User supplies a link, screenshot, or text. It normalises into a candidate and enters the
pipeline at stage 2 with `type` and `category` already set.

Rejected: Google Places (needs a billing account — violates the free-sources rule),
Google Trends (no official API).

### S1-4 · No social scraping
TEC decision D2 carries over intact. Editorial-site scraping (Visit Seattle, do206,
TheStranger, Seattle.gov, venue sites) is unaffected — it was never in scope for D2.

### S1-5 · Supplements ships in v1, auto-publishing, same rules as every category
No special human gate. The zero-medical-claims rule binds it like everything else, which
forces two pieces of new work **before first run**:

**Claims policy.** Supplement articles cover the product, not its effects. Permitted:
ingredients and dose per serving, price per serving, third-party testing (NSF Certified
for Sport, Informed Sport), form, taste, mixability, availability. Label text may be
reported when attributed — "the label states 5g per scoop" is reporting. Efficacy claims
— "creatine improves output" — are stripped by the fact-checker as they are today.

**FTC disclosure.** A fixed disclosure line above the fold on any article containing
affiliate links, plus `rel="sponsored"` on every affiliate link. Emitted in stage 4,
enforced in stage 9. Nothing in the pipeline does this today.

### S1-6 · Relevance scoring needs a per-type rubric
The scorer averages `category_fit`, `audience_fit`, `timeliness` with equal weight and
passes at ≥7.0. A guide has no timeliness, so it loses a third of the rubric and fails
under the current threshold. Non-event types need their own dimensions.

Carried to stage 2 for the exact rubric.

---

## Stage 2 — Clean, dedupe, enrich

### S2-1 · No Categorizer agent
TEC proposed one because it assumed all five categories arrived through a single
undifferentiated funnel. They do not: auto-collection only produces events, and manual
drops carry their category from the user. Nothing is left to classify.

Rejected: TEC's Categorizer agent (an LLM call with no decision to make).

### S2-2 · Type → Wix category is a static map
Replaces the hardcoded `the_guide` at `pipeline/stages/publish.py:149`, which currently
sends every article to the guide category regardless of what it is.

| Type | Wix category |
|---|---|
| `event` | `culture` |
| `place-roundup` | `the_guide` |
| `product-roundup` | `the_guide` |
| technique pieces | `training` (unused in v1) |

### S2-3 · Keep the global cap of 5 per run, add per-category fairness
TEC's ≤10-per-category allows 50 articles per run against a ~33 minute Actions budget.
`max_articles_per_run: 5` stays. A fairness rule stops a busy events week starving
roundups entirely.

Rejected: TEC's per-category cap of 10.

### S2-4 · Roundups rewrite in place; events keep no-cooldown
A re-dropped roundup subject matches the existing Wix post, rewrites the body, keeps the
same URL, and bumps the updated date. One canonical page per subject, accruing authority.

Events are unchanged: one article per occurrence, no cooldown, date-suffixed slugs, as
already decided.

Rejected: new post per drop (splits search traffic across near-identical guides);
blocking re-drops (pipeline could never refresh a stale guide).

### S2-5 · Roundups need a subject key, not a canonical ID
`_canonical_id` is `hash(title + start_dt[:16] + venue_name)`. With no date and no single
venue, both of those are empty and it collapses to a bare title hash — so any title
variation reads as a brand-new subject and S2-4 fails to match.

Roundups key on an explicit subject instead, supplied at drop time:

```
place-roundup:hot-yoga:south-lake-union
product-roundup:creatine
```

Neighborhood comes from the controlled vocabulary already in
`config/neighborhood_images.json` (22 entries), so "SLU" and "South Lake Union" resolve to
the same key. Deterministic by construction — a fuzzy match would let near-duplicates
through, which is the exact failure S2-4 exists to prevent.

---

## Stage 3 — Validate before writing

### S3-1 · ~~No separate citations manifest~~ — **REVERSED 2026-08-21**
Originally rejected TEC decision D9 on the grounds that per-field provenance plus the stage
5 fact-checker already delivered its intent, making a manifest a third overlapping
mechanism.

**That was wrong, and the pipeline proved it.** Per-field provenance lives on the event
record and never travels to the panel. What the judges actually received was
`sources_json` — a list of internal member ids, literally `["169169"]`. The
factual-integrity judge scored a *factually correct* article **3/8** and said exactly why:

> Bus Route 62 stop names and exact meter distances are stated as fact with no documented
> verification source... the provided `research_trail (["169169"])` is an opaque token.

Its prescription was D9 almost verbatim: log each claim, the source consulted, the date,
and whether the source confirmed it. Every fact in the pipeline *was* sourced; only the
record of it was missing — and precision made it worse, because "404 metres" reads as
invention to a judge who cannot check it.

Implemented as `research_trail()` in `pipeline/stages/judges.py`: field groups mapped to
source, URL and timestamp, covering the event listing, the GTFS feeds and the venue
context sources.

### S3-2 · Roundup provenance is per-venue-per-field
For an event, "address" is one value with one source. For a ten-venue roundup it is ten
values with ten sources. Requires the subject table from S1-2.

### S3-3 · Venue selection comes from an owned dataset
Roundup lists are drawn from a curated venue database, not scraped and not extracted from
whatever link was dropped.

| Asset | Rows | Location |
|---|---|---|
| `Seattle_Fitness_Dataset.csv` | 253 venues | `C:\Users\14255\Desktop\Transfar\` |
| `Seattle_Trainers_FINAL.csv` | 1,813 trainers | same |

Rejected: extracting the list from the dropped source (re-publishes another outlet's
editorial selection); OpenStreetMap discovery (new collector, deferred by S1-3, and it
carries locations without hours or prices).

### S3-4 · The dataset selects; the live fetch supplies facts
The venue database answers *which venues*. Stage 3's per-venue site fetch answers address,
hours and current price. Nothing volatile is published from the dataset.

This matters because the CSV's `Pricing` column holds promos with a shelf life — a sample
row reads `New Client 2 week Unlimited $94*; New Client 4 Pack $64*`. Google rating and
review count drift the same way. Under this split, a stale dataset changes *who gets
picked*, never what gets published.

### S3-5 · Normalize `Specialty` into controlled tags, store in SQLite
The column is free text: 204 distinct values across 253 rows. `Personal Training` and
`Personal training` are separate values; `Brazilian jiu-jitsu` and `Brazilian jiu-jitsu
(gi and no-gi)` are separate values. It cannot be queried as it stands.

A one-time pass adds a normalized tag column. Storage moves to a SQLite table inside
`Article_Engine`, matching the existing commit-state-each-run pattern, rather than a loose
CSV on the Desktop.

### S3-6 · Launch on the 27 viable cells; coverage expansion does not block publishing
Neighborhood × specialty cells holding 5+ venues: **27**. Cells holding 10+: **8**.

Densest: strength/gym in Capitol Hill (21), personal training in Capitol Hill (16) and
Fremont (15), strength/gym in Belltown (14), Fremont and South Lake Union (12 each),
Queen Anne (11).

Coverage is central-Seattle only — 14 neighborhoods, six of which hold 206 of 253 rows.
**Zero rows** in Ballard, West Seattle, University District, Green Lake, Georgetown,
Columbia City, Magnolia, Beacon Hill, Rainier Valley, while `neighborhood_images.json`
carries images for all 22. Expanding coverage runs as a background job.

Rejected: filling gaps before launch (delays weeks of writable content); publishing only
the 8 densest cells (too few articles to matter).

### S3-7 · Trainers dataset is a future roundup subtype
1,813 rows carrying `discipline_primary`, `certifications`, `rate_published`,
`neighborhood` and `primary_affiliation` — the last of which links trainers to venues.
Supports "personal trainers in Capitol Hill" style articles. Noted, not scoped for v1.

---

## Stage 4 — Write

### S4-1 · No Adjustor agent
TEC splits tone into a separate agent between the Writer and the Reviewer. `house.md`
already governs voice at write time and seven judges check it afterwards. A third pass is
a third opinion with no tiebreaker.

Rejected: TEC's Adjustor agent.

### S4-2 · Wire `hardgate.py` in as a real pre-judge gate
The other half of TEC decision D5 — a deterministic banned-word linter as a hook — is
worth taking, because the gate it describes does not currently run.

`hardgate.py` (41 banned words, 9 banned phrases, word-count ranges, passive-voice cap) is
named in `config/settings.yaml:91`, `README.md`, `SPEC.md`, and quoted verbatim in
`prompts/writer.md`. **No pipeline code executes it.** `skills.py` clones Skill-Cabinet but
nothing runs the file.

So `prompts/writer.md:125` tells the model "one hit fails the article before a judge reads
it", and that is not true today. The banned-vocab list is advice the model may ignore, and
the only thing catching a hit is a judge that may not.

Adopting D5 closes a real hole rather than adding a layer.

### S4-3 · Per-type article profiles in `settings.yaml`
The `article:` block becomes three profiles.

| | `event` | `place-roundup` | `product-roundup` |
|---|---|---|---|
| details box | one, at top | one per venue | one per product |
| sections | 5–6 | one per venue | one per product |
| meta description | date + neighborhood | neighborhood, no date | no date, no place |
| slug | date-suffixed | subject key, stable | subject key, stable |
| disclaimer | "details can change" | own text | own text + FTC line |

Roundup slugs stay stable because S2-4 rewrites in place; a date suffix would break the
subject match on every refresh.

### S4-4 · One H2 per venue; per-type section and word caps
`write.py:117` hard-rejects anything outside 5–6 sections and H3 is banned, so a ten-venue
roundup has nowhere to put venues seven through ten.

Roundups get `sections_max: 12` and `word_count_max: 1600`. One paragraph per H2 is
preserved, so `hardgate.py` needs no exemption.

This is a smaller change than it looks: only 8 of the 27 viable cells hold 10+ venues.
Nineteen hold five to eight, so most roundups land inside the existing range anyway.

Rejected: capping roundups at six venues (truncates the eight densest cells); venue blocks
outside the section count (turns the article into a directory listing, weak against the
editorial-value judge); thematic grouping (two venues per paragraph complicates
fact-checking).

### S4-5 · FTC disclosure is emitted at write time
Any article carrying affiliate links gets the fixed disclosure line above the fold and
`rel="sponsored"` on every affiliate link. Enforced again at stage 9. Required by S1-5.

---

## Stage 5 — Fact-check the draft

### S5-1 · Verification is cached against the venue record with a TTL
A ten-venue roundup carries 40+ checkable claims against an event's ~5. Per venue the
pipeline must confirm: still operating, address, hours, current price. That cost is
irreducible — the worst failure mode is a closed gym sitting at number three.

Venues repeat across roundups (a Capitol Hill gym appears in both strength/gym and
personal training), so verification results cache against the venue with a TTL. Across 27
roundups drawn from 253 venues most verifications are cache hits.

### S5-2 · Transit trimmed for roundups, not dropped
Nearest stop and route, one line per venue, still GTFS-sourced. Ten full transit blocks
would bury the article; dropping it silently would break the rule that transit is a
factual claim and cannot come from model knowledge.

### S5-3 · Judge-flagged venues are excluded until re-verified
The audit file carries 27 flagged rows, 7 of them flagged on *existence*. These stay out
of roundups regardless of score.

---

## The SFM Score

A composite venue rating. **10 factors, each scored 1–10, total 100.** Published as the
magazine's own rating. There is no venue rating in `Article_Engine` today — every existing
score is relevance (does this event deserve an article), the seven judges (is this article
publishable), or `enrich.py:676` picking an image by pixel area.

### R-1 · Location drives coverage priority and enters the score at one slot of ten
A rating where the same gym scores differently by zip code is indefensible to a reader and
to the venue. Capped at one factor, neighborhood shades ordering within a cluster rather
than driving it.

Neighborhood points are assigned editorially, one neighborhood at a time. Those points
also drive **coverage priority** — which of the 27 viable roundups get written first and
refreshed most, given five article slots per run.

### R-1a · Neighborhood points — all 23 assigned
Assigned editorially by the owner, not computed. Scores do **not** track venue density.

| Neighborhood | Points | Venues | Viable roundups |
|---|---|---|---|
| South Lake Union | **10** | 25 | 3 |
| Downtown | **10** | 8 | 1 |
| Belltown | **9** | 33 | 4 |
| Pioneer Square | **9** | 6 | 0 |
| Queen Anne | **8** | 41 | 7 |
| Fremont | **8** | 39 | 4 |
| Westlake | **8** | 4 | 0 |
| Ballard | **8** | 0 | 0 |
| West Seattle | **8** | 0 | 0 |
| Capitol Hill | **7** | 45 | 5 |
| Wallingford | **7** | 23 | 2 |
| Madrona | **7** | 7 | 0 |
| Madison Park | **7** | 4 | 0 |
| Leschi | **7** | 2 | 0 |
| First Hill | **6** | 3 | 0 |
| University District | **6** | 0 | 0 |
| Green Lake | **6** | 0 | 0 |
| Magnolia | **6** | 0 | 0 |
| Eastlake | **5** | 13 | 1 |
| Columbia City | **5** | 0 | 0 |
| Rainier Valley | **3** | 0 | 0 |
| Beacon Hill | **2** | 0 | 0 |
| Georgetown | **1** | 0 | 0 |

### R-1b · Raw scores are used directly, not rescaled
The final range is **1–10**, so factor 10 carries its full intended weight without any
transformation. The mid-pass concern about clustering at 7–10 was resolved by the later
scores rather than by rescaling.

Rejected: rescaling min→1 / max→10 (unnecessary once the full range was used);
percentile-ranking (would force SLU and Downtown apart despite being scored equal).

### R-1c · Priority and content availability are inverted
Roundup capacity by score band:

| Band | Neighborhoods | Viable roundups |
|---|---|---|
| 10 | SLU, Downtown | 4 |
| 9 | Belltown, Pioneer Square | 4 |
| 8 | Queen Anne, Fremont, Westlake, Ballard, West Seattle | 11 |
| 7 | Capitol Hill, Wallingford, Madrona, Madison Park, Leschi | 7 |
| 5 | Eastlake, Columbia City | 1 |

Pioneer Square scores 9 with zero roundups. Capitol Hill holds the most venues in the
dataset and scores lowest of the central cluster. Publishing order therefore starts thin:
8 roundups across the 9–10 band, then 11 in the 8 band.

This is the clearest signal for where collection expands first — the downtown core and
Pioneer Square, not the neighborhoods already dense in the dataset.

### R-1d · Collection queue for the nine uncollected neighborhoods
In score order: Ballard (8), West Seattle (8), University District (6), Green Lake (6),
Magnolia (6), Columbia City (5), Rainier Valley (3), Beacon Hill (2), Georgetown (1).

**Open issue:** Columbia City and Rainier Valley are separate labels in
`neighborhood_images.json` but overlap in the City Clerk atlas. Boundary must be settled
before collection runs there, or venues will double-assign.

**Open issue — Belltown geography.** Only 13 of its 33 venues carry Google's Belltown
label; the rest are Seattle Waterfront (7), Denny Triangle (6) and Pike-Pine Retail Core
(6). Not an error — neighborhoods were assigned by point-in-polygon against the City Clerk
atlas, whose Belltown polygon is wider than the commercial one. But "10 gyms in Belltown"
listing a Pike-Pine studio will read as wrong to a Seattle reader and may fail the
`factual_venue_integrity` judge. Resolve by tightening the polygon for roundups or
titling by the wider area. Belltown is the only neighborhood where the two geographies
diverge this far.

### R-2 · The slate

| # | Factor | Source |
|---|---|---|
| 1 | Price transparency — full / intro-only / "contact us" | live fetch |
| 2 | Schedule published | live fetch |
| 3 | Online booking or trial | live fetch |
| 4 | Credentials published by the venue | live fetch |
| 5 | Transit access — GTFS density vs stored lat/lon | computed |
| 6 | Site completeness — hours, address, contact | live fetch |
| 7 | Google standing | held + refresh |
| 8 | Community programming — workshops, events, socials | live fetch |
| 9 | Accessibility signals | live fetch |
| 10 | Neighborhood | assigned |

Topic fit is deliberately **not** a factor. It would make the score contextual — the same
gym showing 82 in one article and 74 in another reads as a bug. It belongs upstream as an
inclusion filter deciding who is *in* a roundup.

### R-3 · The Google factor is a volume-shrunk rating scored by percentile rank
Raw stars do not discriminate in this dataset:

| | |
|---|---|
| mean / median rating | 4.70 / **4.90** |
| venues rating ≥4.9 | **142 of 233 — 61%** |
| ≥4.9 on under 20 reviews | **66** |
| review count median / max | 44 / 1,448 |
| under 5 reviews | 33 venues |

Scoring raw stars puts 61% of venues in the top notch — 10% of the score contributing
nothing to the ordering.

Bayesian shrink toward the dataset mean (C = 4.70, m = 30) fixes fairness: "5.0 from 1
review" falls to 4.71 while "5.0 from 374" holds at 4.98. But the whole dataset then sits
in a **0.27-wide band**, so a linear map to 1–10 scores everyone 9-point-something.

The factor therefore scores on **percentile rank of the shrunk value, against all 253
Seattle venues**. Top of the distribution scores 10, bottom scores 1, spread guaranteed.
One absolute number per venue that does not change between articles.

Rejected: raw rating (no discrimination); review volume alone (measures establishment,
rewards chains); per-category ranking (28 categories, thin ones give meaningless
percentiles); per-neighborhood ranking (a 10 in Leschi's 2 venues ≠ a 10 in Capitol Hill's
45).

**Missing data:** 20 of 253 venues have no Google rating. Those score a neutral 5 —
absence is not evidence of poor quality, and zero would punish new or small venues.

---

## Stage 6 — The judge panel

### S6-1 · No fresh-reviewer loop
TEC decision D6 proposes a generalist reviewer, new each round, capped at 3. The built
panel already runs seven blind single-dimension judges, each required to quote an excerpt
for any deduction, with the same cap. Strictly stronger.

Rejected: TEC's Reviewer agent and bounded review loop.

### S6-2 · Roundup dimensions go into the shared `RUBRIC.md`
`structural_format_compliance` reads the inherited rubric, which describes an event: hook,
45-word details box, 5–6 sections, no H3. S4-4 gives roundups up to 12 sections, so the
judge scores a correct roundup against the wrong standard, fails it, and the article
exhausts its three revise attempts and lands in the hold queue. Every roundup, every time.

Fix: `RUBRIC.md` in `tanzimozer/Skill-Cabinet` gains type-conditional dimensions covering
`event`, `place-roundup` and `product-roundup`.

**Consequence accepted:** Skill-Cabinet is a public repo — that is why `skills.py` clones
it without a token. SFM's article structure spec becomes public, and every other TIMBR
surface reading the file inherits roundup rules it may never use.

Rejected: local per-type supplement in Article_Engine (keeps the shared file clean but
splits structure across two documents); per-type judge lists (new judge to write, scores
not comparable across types); exempting roundups from structural judging (`write.py` only
counts sections — it cannot detect bad ordering, unbalanced venue coverage, or an article
that reads as a directory).

### S6-3 · Panel depends on a live clone, and that stays
`skills.rubric()` shallow-clones Skill-Cabinet each run; an unreachable repo raises
`InfraFailure` for every article in the batch. Degrades to retry-and-requeue rather than
losing work, so it is recorded rather than fixed.

---

## Stages 7–8 — Editor in Chief

### S7-1 · Harness retained unchanged
Runs `skills/timbr/magazine-eic/SKILL.md` (persona Maya, 8 steps) with the existing
override of its "never publish without the owner's go" instruction. The EIC receives the
revision history as well as the article — a piece that limped through on its third attempt
should not read like one that passed cleanly. Keep that.

### S7-2 · FTC compliance is checked deterministically, twice
The EIC is a generic magazine persona with no knowledge of FTC disclosure, and a missing
affiliate disclosure is a regulatory failure rather than an editorial one — the kind an LLM
drops by not thinking about it, not by disagreeing.

Disclosure line and `rel="sponsored"` are therefore regex-checked in `hardgate.py` at
stage 4, so a bad draft never reaches the judges, **and** re-checked immediately before the
Wix write, because the EIC can revise the article after stage 4.

Rejected: publish-gate only (burns full judge and EIC cost on a doomed draft); write-time
only (leaves the EIC's edits unchecked); instructing the EIC (makes a regulatory
requirement depend on an LLM remembering).

---

## Stage 9 — Publish

### S9-1 · Adopt TEC steps 7–9; rebuild the site onto CMS Data Collections
SFM currently publishes through `/blog/v3/draft-posts` — the Wix Blog API. Blog feeds
order by date and featured status, so arbitrary ranking is not achievable on it. TEC
identified this as its number-one blocking unknown and never resolved it.

Decision: rebuild seattlefitnessmag.com onto CMS Data Collections with dynamic pages and
a repeater sorted by a `rank` field, then implement the Ranker, rank storage and the
reorder-and-shift publish sequence.

**Scope this pulls in:**

| Area | Change |
|---|---|
| `pipeline/wix.py` | rewritten against the Wix Data API; the draft-then-publish two-step disappears |
| URLs | `/post/<slug>` → `/<collection-page>/<slug>`; **301 redirects required for every existing article** or search equity is lost |
| existing posts | must migrate into the collection, or two content systems run in parallel |
| `config/settings.yaml` | the three Blog category IDs (`the_guide`, `training`, `culture`) stop applying; taxonomy rebuilt on dynamic pages |
| `pipeline/main.py` | `concurrency: 1` sequential flow must gain a barrier — ranking needs all approved articles plus everything live |
| new | Ranker stage, rank storage, reorder-and-shift sequence |

The URL migration is the largest real exposure — larger than the engineering — for a
publication whose value is search traffic.

Rejected: skipping site ranking (SFM Score already orders venues *within* a roundup, which
is the ordering readers act on); featured-post pinning (controls only the first few
slots); deferring the decision.

### S9-2 · Ranking auto-applies behind a snapshot
No human gate. Consistent with auto-publishing everything else. The current order is
snapshotted before each reorder pass, so a bad ranking is restored in one command rather
than repaired article by article.

Rejected: TEC's step 8 human rank gate (reintroduces a blocking step into an autopilot
pipeline); gating only large moves (threshold tuning with no clear right answer);
auto-apply with no snapshot (manual repair across every affected article).

### S9-3 · TEC's D8 safety requirements are already met
| D8 requires | Already present |
|---|---|
| dry-run | `publish.py:119` — `cfg.get("dry_run")` |
| wrong-site guardrail | `wix.py:229` — `_assert_writable()`, enforced in code |
| non-destructive update | `update_published` with `fieldMask` protecting `richContent` |

`update_published` is also what makes S2-4's rewrite-in-place possible.

### S9-4 · Roundups publish immediately on passing the gates
No slot scheduling. Events keep their 5–7 day lead window measured from the event date;
roundups go live as soon as they clear the panel and the EIC.

Clumping is bounded by `max_articles_per_run: 5` and the S2-3 per-category fairness rule.
The rebuild also decouples this from prominence — with a `rank` field, *when* an article
publishes and *where it sits* are independent.

Rejected: gap-filling against the events calendar; fixed cadence; manual slot assignment.

---

## Post-publish sweep

### S10-1 · Banner and demote, never unpublish
The built behaviour stays: an event whose date has passed gets an "ended" banner and keeps
its URL and its search equity. The sweep re-checks live status at the source, so cancelled
and sold-out events get the same treatment without waiting for the date.

TEC's unpublish-on-expiry is rejected — it discards the search equity the banner preserves.

TEC's *re-rank* half is adopted, and only becomes meaningful now that S9-1 gives articles
a rank: an ended event drops in the ordering rather than holding a top slot.

### S10-2 · Roundup staleness triggers on venue change
Roundups have no expiry date and decay gradually — a venue closes, a price moves, a new
studio opens that belongs on the list.

The S5-1 verification cache already re-checks venues on a rolling TTL. When a venue inside
a published roundup is found closed or materially changed, that roundup queues for a
rewrite-in-place under S2-4.

Rejected: fixed periodic refresh (burns runs on unchanged articles); no refresh (a closed
gym sits in the guide indefinitely — precisely what the `factual_venue_integrity` judge
exists to prevent).

---

## First run — 2026-08-20

The pipeline had never executed. It now has. The orchestrator, state layer, error
handling, notify and partial-coverage logic all behaved correctly; every failure was in
the source layer or in Windows portability.

### Fixed and verified

| # | Bug | Fix |
|---|---|---|
| 1 | `scrape.py:783` defaulted `continue_on_error` to False, so visitseattle failing aborted the whole adapter before seattlegov, do206 and thestranger were tried — contradicting `sources/__init__.py` (default True) and the documented rule that a source failing does not stop the run | `continue_on_error: true` in `config/sources.yaml` |
| 2 | Visit Seattle's listing markup was rebuilt; no configured selector matched | new `_visitseattle_from_api` reads `/wp-json/visitseattle/v1/events`, HTML spec kept as fallback. robots.txt permits `/wp-json/` |
| 3 | Keyword pre-filter used plain substring matching — `run` fired on "brunch" and "runs through November", `ride` on "bride" | `_term_pattern` in `gather.py` compiles whole-word lookarounds. **16 of 53 false positives removed** on a real 161-event calendar |
| 4 | Dead sources retried every run | disabled with documented cause (below) |
| 5 | `subprocess` resolves a bare command against `.exe` only on Windows, but the npm install is `claude.CMD` — every role died with `WinError 2` | `_claude_executable()` uses `shutil.which`, avoiding `shell=True` with a prompt in argv |

### Sources, verified 2026-08-20

| Source | State |
|---|---|
| visitseattle | **working** via JSON route — 1 in-window event |
| seattlegov (scrape) | dead — JS-rendered shell: HTTP 200, 1.4k of text, 0 event elements |
| seattlegov (rss) | dead — 404 |
| thestranger (rss) | dead — 404; `everout.com/seattle/events/feed/` also 404 |
| thestranger (scrape) | dead — bot-protected, HTTP 202 with empty body |
| do206 | unusable — returns only today's events and ignores every date parameter tried, so it can never intersect the 7–10 day window. Music and nightlife, not fitness |
| eventbrite | needs `organization_ids`; `/events/search/` is restricted and returns 404/403 to most tokens |

### Second session — stages 2 through 6 executed

Auth resolved (`claude auth login`, `timbr.tools@gmail.com`, Max). A real RunSignUp race
(Summer Spirit Run, Magnuson Park, 2026-08-29) was injected by hand, standing in for the
adapter that does not exist yet, and driven through the pipeline.

**Reached the judge panel and was rejected 0-for-7.** All seven scored below the passing
mark of 8: seattle_local_specificity 3, editorial_value 4, seo 4, voice 5,
ai_pattern_detection 5, structural_format 7, factual_venue_integrity 7.

The rejection was correct, and traceable to a single upstream cause: geocoding failed, so
the article had no neighborhood and no transit, so the writer wrote about registration
format instead of place, so the local-specificity judge scored it 3.

**Bugs 6 to 10, all fixed:**

| # | Bug | Fix |
|---|---|---|
| 6 | Prompts passed as argv hit the `cmd.exe` 8191-char ceiling; 5 of 7 prompt files exceed it before payload (writer.md is 19k) | `roles.py` pipes the prompt on stdin — also removes every quoting hazard, verified at 21,630 chars |
| 7 | `clean.run()` sits inside `if not skip_gather:`, so `--skip-gather` strands the candidates it exists to recover | recorded, not yet fixed |
| 8 | Two spec gaps found by the validator (see below) | prompt amended |
| 9 | `write.py:133` checks word count at draft only; fact-check removed 28 claims and halved 948 words to 457, and the panel saw an undersized article | `factcheck.py` re-measures after a clean pass and routes back to `s4_write` below the floor |
| 10 | `geocode()` made one attempt; an intersection ("74th and Sand Point Wy") defeats Nominatim outright | query ladder in `enrich.py` — Magnuson Park now resolves on attempt 2 to **Sand Point** |

**Two spec decisions the run forced:**

- **`runsignup` joins the single-source exception tier** (`cross_check_validator.md`).
  Without it every race the pipeline finds holds forever, because the source it is built
  on could not corroborate itself. The guardrail still reads "not extensible by you", so
  only the spec can widen it.
- **Youth tiers are excluded from the lowest-price rule.** The page's true minimum was the
  $6 Kids Dash against a $32 5K entry. Multi-distance races are the norm in running, so
  the unamended rule would have printed a children's price as the event price on nearly
  every race.

### Capacity — measured, and the constraint removed

One article, one failed attempt: **29.7 minutes.** Fact-check is 56% of that (16.7 min
across 3 loops), write 26%, judges 15%.

At 2 runs/day that is 1,783 min/month for a *single* article — 89% of the 2,000-minute
free tier on a private repo. The configured 5 articles/run worked out at 8,915 min/month,
4.5× over, and `timeout-minutes: 35` could not hold even one article that needed a rewrite.

**Resolved by making `tanzimozer/Article_Engine` public** (2026-08-20) — Actions is
unlimited on public repos, so minutes stop being the constraint. Git history was scanned
across all four commits first: no secrets, no `.env`, no credentials. `SFM-MERGE.md` is
gitignored, since neighborhood priorities and dataset paths are strategy rather than code.

Settings corrected to match measured reality: `max_articles_per_run` 5 → 3,
`run_timeout_seconds` 1800 → 13200, job `timeout-minutes` 35 → 240 (20 minutes of headroom
so the run always commits state). What binds now is the 6-hour job ceiling and the Max
quota, not minutes.

⚠️ `settings.yaml` states that its values are locked decisions from `SPEC.md` and that
SPEC changes first. These were changed against measured evidence — **`SPEC.md` still needs
syncing.**

### Auth — resolved
Headless `claude -p` reported `Not logged in`; `.credentials.json` dated 2026-04-30 and had
expired. Cleared with `claude auth login` as `timbr.tools@gmail.com` (Max). `claude auth
status` is the way to confirm the account without reading the credentials file.

CI still needs its own: `claude setup-token` produces a long-lived token for
`CLAUDE_CODE_OAUTH_TOKEN` in repo secrets. **Still outstanding — no scheduled run can
work without it.**

### The root problem is still source coverage
Every configured source is a general city calendar. Visit Seattle carries 66 Music,
53 Museums, 21 Festivals and **3 Sports** across 161 events.

**RunSignUp is verified viable** — documented public REST API, robots.txt blocks only
named bots (Yandex, Semrush, Ahrefs), 17 Seattle races returned, two inside the 7–10 day
window: `Run for Breast Cancer 5K/10K/13.1` (2026-08-29) and `Billy Mills Run/Walk for
Life` (2026-08-30). This is the adapter to write next.

---

## Third session — the article reached the panel

The first article to clear write and fact-check and be judged on its merits. Two judges
passed; five did not. Getting there took six more fixes.

### The deadlock — the session's most important finding
`seattle_local_specificity` demands terrain, history and neighborhood texture. The
fact-checker deletes any claim it cannot trace to a source. **No configured source supplied
verified description**, so the two gates pulled in opposite directions and an article could
satisfy one only by failing the other. The same draft had "flat course" cut twice as
invention *while* being failed 3/8 for having no sense of place.

No amount of rewriting resolves that — it is a missing input, and three rewrite attempts
would have burned 75 minutes reaching the same result.

**Fixed by `pipeline/venue_context.py`**: cited descriptions fetched from Wikipedia (action
API, plain-text extract capped at 2500 chars) and the City of Seattle's own parks pages
(`main` element only — the page opens with a site-wide burn-ban banner). Both carry the URL
they came from, so a sentence built on them survives fact-checking and a judge can audit it.
Wired through `enrich_event`, the writer payload, the fact-checker's `verified_source_data`
and the judge payload. `writer.md` and `fact_checker.md` both had explicit allow-lists of
fact sources that would otherwise have excluded it.

Magnuson Park now yields the former Naval Station Puget Sound, 350 acres, the Sand Point
peninsula, Lake Washington, and four miles of shoreline trails.

### Bugs 11–15

| # | Bug | Fix |
|---|---|---|
| 11 | Writer timeout 600s was marginal at 456s and failed outright once the payload carried transit and neighborhood — three attempts, 74 minutes wasted | raised to 1800s; fact-checker 600 → 1200 (it was at 529, 88% of budget) |
| 12 | A 600s timeout took **35 minutes** to fire. `subprocess` kills only the direct child; on Windows that is `cmd.exe`, and the node grandchild survives holding the stdout pipe | `_kill_tree()` in `roles.py` uses `taskkill /T`. Verified: 12.2s against a 12s ceiling |
| 13 | `int(verdict.get("score", 0))` raised `TypeError` on a judge returning `"score": null` — the default only applies when the key is *absent* — taking down the whole article | coerce defensively, score 0, fail the article not the run. Verdicts now persist per judge rather than after the panel, so a crash keeps the ones already paid for |
| 14 | `main.py` computed a rejection `reason` and used it only on the hold path, dropping it on the far more common requeue path | reason added to the requeue log |
| 15 | A rejected draft was discarded entirely, so a write-stage failure left no reason *and* no artifact | `reject()` in `write.py` stores the draft before returning |

### Config decisions

**Writer floor raised for fact-check attrition.** The writer aimed at the article's own 800
floor; fact-checking then removed 20% and it landed at 695. `write.py` now derives
`prose_min` from `FACTCHECK_ATTRITION = 0.20`, giving a 1000–1155 band, and `writer.md` was
updated to match — the prompt already said 880 while the gate accepted 800, and the looser
number won.

Worth noting the ceiling this exposes: 20% is about the most the inherited 800–1200 house
band can absorb. At 30% attrition the writer's floor (1143) would nearly meet its ceiling
(1155). If fact-checking routinely strips more than a fifth, the house band itself is what
needs revisiting.

**Fact-check loops separated from rewrites.** `max_revise_attempts: 3` governed both. A
fact-check loop is 4–5 minutes and mechanical; a judge-triggered rewrite is 25+ because it
re-runs the writer and the whole fact-check sequence. Capping the cheap one to match the
expensive one held a publishable article one loop short — changes ran 3, +4, +1, plainly
converging. New `max_factcheck_loops: 5`; on the next run it reached clean in **1 loop with
0 changes**, confirming it had genuinely converged.

### Attrition across runs — the geocoding fix compounding

| run | local data | drafted | after fact-check | stripped |
|---|---|---|---|---|
| 8 | none | 948 w | 457 w | **52%** |
| 10 | neighborhood + transit | 870 w | 695 w | 20% |
| 13 | same, higher target | 1065 w | 1010 w | **5%** |

Give the writer verified local material and it stops inventing; starve it and the
fact-checker deletes what it invents. That was the bet behind fixing geocoding first.

### Panel scores, run 15

| dimension | run 8 | run 15 |
|---|---|---|
| voice_brand_compliance | 5 | **8 PASS** |
| structural_format_compliance | 7 | **9 PASS** |
| editorial_value | 4 | 5 |
| ai_pattern_detection | 5 | 5 |
| seo | 4 | 4 |
| seattle_local_specificity | 3 | 3 |
| factual_venue_integrity | 7 | 3 |

The two regressions are the deadlock and the missing trail, both now addressed. `seo` has
never moved and its complaint is mechanical and unaddressed: the primary phrase appears in
neither the hook line nor any H2.

---

## Open work

Ordered roughly by blocking-ness, not by size.

1. **Confirm the pipeline can run at all.** No end-to-end run has ever executed. Verify
   `CLAUDE_CODE_OAUTH_TOKEN` is present in the `Article_Engine` repo secrets.
2. **Site rebuild** (S9-1) — Wix Blog → CMS Data Collections. Largest item. The URL
   migration and 301 redirects are the real exposure, not the code.
3. **Wire `hardgate.py` in** (S4-2) — it is referenced in four places and executed in none.
4. **Normalize the venue dataset** (S3-5) — map the 28 primary Google categories onto ~12
   tags, move from CSV to SQLite. Smaller than first assessed: the audit file already
   carries structured categories and 253/253 lat/lon.
5. **Roundup dimensions into `RUBRIC.md`** (S6-2) — public repo; do this before any
   roundup reaches the panel or every one fails structurally.
6. **Supplements claims policy and FTC disclosure text** (S1-5, S7-2) — required before
   first run, not after.
7. **Per-type article profiles** in `settings.yaml` (S4-3) and the section/word caps (S4-4).
8. **Resolve two geographies** — Belltown's polygon vs commercial boundary (R-1a), and the
   Columbia City / Rainier Valley overlap (R-1d).
9. **Collection expansion**, in neighborhood-score order, starting with the downtown core —
   where priority is highest and content thinnest (R-1c).

---

## Known code touchpoints from stage 1

Recorded now, resolved in later stages.

| File | Change |
|---|---|
| `pipeline/state.py` | `articles.event_id` must become nullable; new subject table for multi-venue articles |
| `pipeline/state.py` | `events` table is one-venue by construction (`venue_name`, `start_dt`, `register_url`, `skill_level`, `what_to_bring`) |
| `config/settings.yaml` | `article:` block is singular — needs per-type profiles |
| `config/settings.yaml` | `scheduling:` is entirely event-date-relative; guides have no anchor date |
| `pipeline/stages/gather.py` | candidate filter is a 7–10 day date window |
| `pipeline/stages/clean.py` | `_canonical_id` = title + `start_dt[:16]` + `venue_name`; collapses to a title hash when both are empty |
| `pipeline/stages/sweep.py` | ended-banner keys on the event date passing |
| `prompts/relevance_scorer.md` | `timeliness` dimension does not apply to guides |
| slug rule | date suffix exists to stop recurring events colliding; guides need a different collision rule |
