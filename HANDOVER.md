# Handover

State as of 2026-08-21. Written so this can be picked up on another machine,
by another person, or by the same person three weeks from now.

---

## One paragraph

The pipeline turns a Seattle fitness event into a published SEO article, in
eight stages. Until 2026-08-21 it had never been run. It now takes a hand-fed
event all the way through to a scheduled publish, at a quality the judge panel
passes. It cannot yet find its own subjects, or process more than one article
at a time.

**It runs on demand, not on a clock.** The cron is disabled in
`.github/workflows/pipeline.yml`; `workflow_dispatch` remains, so it can still
be triggered manually from the Actions tab. Locally, just run it.

---

## Run it

```bash
pip install -r requirements.txt
claude auth status                       # must show a Max subscription
python -m pipeline.main --dry-run -v     # full pipeline, writes nothing
```

One article costs **50-70 minutes and ~15 model calls**. Budget accordingly.

### Debugging without waiting an hour

`pipeline/replay.py` runs a single stage against whatever is already in the
database. Same prompts, same model, same config — it only skips re-running
upstream stages.

```bash
python -m pipeline.replay --list
python -m pipeline.replay --article art_x --snapshot before-my-change
python -m pipeline.replay --article art_x --stage s6_judges
python -m pipeline.replay --article art_x --restore before-my-change
python -m pipeline.replay --article art_x --stage s5_factcheck --payload
```

**Use `--payload` on any stage you touch.** It prints what the stage would be
sent, costs nothing, and catches the single most expensive class of bug in this
codebase (below). Publishing is dry by default; `--live` is explicit.

---

## The stages

```
s3_validate    cross-check the listing against live sources
s3b_research   fetch primary sources, return a fact trail        (new)
s4_write       draft from the trail
s4b_voice      one pass for voice only                           (new)
s5_factcheck   delete anything not traceable to a source
s6_judges      seven dimensions, gate on average + floor
s7_eic         Editor in Chief, final read
s9_publish     schedule and write to Wix
```

Stages 1-2 (gather, clean) run once per invocation, not per article.

---

## What is proven, and what is not

**Proven** — one article, all eight stages, panel average 8.14, EIC pass.

**Never exercised at all:**

| | |
|---|---|
| dedupe across sources | it has only ever seen one source. Merging the same event across Eventbrite / venue site / aggregator, and resolving field conflicts by priority, is its entire job and has never happened |
| `venue_feature` content type | schema is wired, no venue article has ever been built |
| the revision loop | articles have been requeued; none has been revised and passed |
| multiple articles per run | cap is 3, only ever done 1 |
| both sweeps | approval needs Drive; post-publish needs a published article |
| real Wix publishing | 0 published |
| stage 1 producing a usable event | every article so far was hand-injected |

Dedupe is the biggest untested risk: it is the stage most likely to behave
differently on real multi-source input, and the one guarding against publishing
the same race twice.

---

## Blocked on

1. **`WIX_API_KEY` / `WIX_MEMBER_ID`** not set. Nothing publishes. This is the
   only blocker between the current pipeline and a live article.
2. **No automatic event source.** Every configured source is a general city
   calendar — Visit Seattle returns 66 music events, 53 museums, 3 sports.
   **RunSignUp is verified viable**: documented public API, robots.txt clean,
   17 Seattle races with real dates and addresses. Nothing reads it yet, so
   events are hand-fed.

Not blockers while running locally, but they become blockers the moment anything
runs on GitHub:

- **`CLAUDE_CODE_OAUTH_TOKEN`** is not in repo secrets. Local runs use your own
  `claude auth login`; a runner has nothing.
- **`Skill-Cabinet` is now private.** `skills.py` clones it without credentials
  and its comment still says "it's a PUBLIC repo, so no PAT is needed". Three
  stages depend on it (voice handbook, rubric, EIC harness). Works locally
  because your git is authenticated; fails on a runner.

### What the schedule actually did

It ran twice daily from 2026-08-22 to 2026-08-26 on the pre-fix code: **33 runs,
0 events, 0 articles, 0 published, one failure every run.** Gather died, the run
continued with partial coverage exactly as designed, found nothing, committed
state and exited. Nothing was ever published, and no model call was ever
attempted — which is why those runs prove the workflow fires but say nothing
about whether it can authenticate.

The cron was retired rather than repaired: unattended runs against paths that
have never been exercised spend quota to produce untested output. Uncomment the
two cron lines to restore it.

---

## Decisions worth not re-litigating

**The judge gate is calibrated, not chosen.** Requiring all seven dimensions at
8 failed the shipped reference article in `tanzimozer/Current-Article-Engine`
**1/7**, while machine output scored 3/7. A gate that published work fails and
machine output beats is measuring the wrong thing. It is now average ≥ 6.5 with
no dimension below 5, derived from that article's own scores. Recalibrate
against the reference, not against intuition.

**Word specs match the reference.** 860 floor, 150-200 word lede, 100-150 word
sections. The previous floor demanded ~25% more words than the proven standard,
from source material that could not support it — which is where the padding the
judges flagged came from. Raising it again will reproduce that.

**The voice pass runs before the fact-checker, deliberately.** Vivid prose is
where invention creeps in. Keeping the fact-checker last means anything the
voice pass invents is still caught. Reordering reopens the hallucination gap.

**Three TEC design decisions were overruled and all three had to be reversed** —
the citations manifest, the researcher/writer split, and the tonality pass. Each
was rejected on the grounds that the built system already covered it; each time
it covered the label and not the function.

---

## The bug class that cost the most time

Five separate instances of one shape: **a stage judging content against a
payload missing the evidence it needs.**

- judges shown `["169169"]` instead of the research trail
- fact-checker's prompt listed `research` as a valid source; the payload omitted
  it, so every sourced fact read as invention and **71% of a draft was deleted**
- gate counted section bodies while the prompt defined the band as hook +
  sections + disclaimer — two good articles thrown away as "too short"
- EIC shown a reference to evidence instead of the evidence, and held an article
  for seven claims that were all in the trail at high confidence
- judges given the four-entry grouped trail rather than the thirty-one facts

Every one is a duplicated definition of a single fact. When adding a field to
any payload, run `--payload` on the stages downstream of it.

---

## Companion documents

- **`SFM-MERGE.md`** — the merge specification. Every stage decision with its
  reasoning and what was rejected, the SFM Score design, neighborhood priority
  scores, and the venue dataset analysis. Read this before changing any stage.
- **`SPEC.md`** — the original decision record. **Now out of sync**: settings
  were changed against measured evidence and SPEC was not updated. Trust
  `settings.yaml` and `SFM-MERGE.md` over SPEC where they disagree.

## Datasets

Not in this repo. `C:\Users\14255\Desktop\Transfar\`:

- `Seattle_Fitness_Dataset.csv` — 253 venues, judge-reviewed, 14 neighborhoods
- `Seattle_Trainers_FINAL.csv` — 1,813 trainers with affiliations

27 neighborhood-and-specialty combinations hold 5+ venues, so 27 venue roundups
are writable today. Coverage is central-Seattle only: **zero rows** in Ballard,
West Seattle, University District, Green Lake.

---

## Suggested next moves

1. **Store the Wix credentials** and publish one article for real. Everything
   upstream is proven; this is the last untested link, and one live article is
   worth more than any amount of further dry-running.
2. **Build the RunSignUp adapter.** The only verified fitness-event source, and
   the thing standing between hand-feeding and self-service.
3. **Exercise dedupe with two real sources** before trusting it. It has never
   merged anything, and it is what stops the same race publishing twice.
4. **Then venue features** — the dataset exists, the reference article is a
   venue piece, and venues have no publishing deadline, so a failed attempt
   costs nothing but time.

Restoring the schedule is worth doing only after 2 and 3, and it needs the two
GitHub-side items above resolved first.
