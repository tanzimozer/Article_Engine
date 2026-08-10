# Article Engine

Finds upcoming Seattle fitness, wellness and lifestyle events and publishes SEO
articles about them to **seattlefitnessmag.com**.

Runs twice daily on GitHub Actions. No machine of yours has to be awake.

Full decision record: **[SPEC.md](SPEC.md)**. Change it there before changing it
in code.

---

## How it works

```
gather → clean → validate → write → fact-check → judges → EIC → schedule → publish
```

Nine stages. Deterministic steps run in Python; judgment steps run as Claude
Code roles. Routing is fixed at every branch, which is why an orchestration
script owns control flow rather than an agent framework.

An article that fails a content gate is revised and retried up to three times,
then held for a human. An article that hits a transport failure is requeued for
the next run instead — retrying a draft against an HTTP 500 accomplishes
nothing.

---

## Layout

```
config/         locked settings, sources, keyword filter
prompts/        one file per LLM role
pipeline/
  main.py       orchestrator — control flow, routing, retries
  state.py      SQLite store, committed every run
  roles.py      headless Claude Code invocation
  skills.py     reads voice + rubric from Skill-Cabinet
  stages/       one module per stage
  sources/      Eventbrite, RSS, HTML scrapers
  enrich.py     geocode, neighborhood, images
  transit.py    GTFS lookups
  wix.py        Blog API client
  ricos.py      article → Wix rich content nodes
  drive.py      Google Docs hold queue
state/          pipeline.db, committed
```

---

## Running it locally

```bash
pip install -r requirements.txt
python -m pipeline.main --dry-run -v
```

`--dry-run` runs every stage and never writes to Wix.

Other flags:

```bash
python -m pipeline.main --skip-gather     # work the queue, fetch nothing new
python -m pipeline.main --skip-sweeps     # skip approval + post-publish sweeps
```

---

## Secrets

Set these in the repo's Actions secrets.

| Secret | Needed for |
|--------|-----------|
| `CLAUDE_CODE_OAUTH_TOKEN` | **Required.** All ten LLM roles. |
| `WIX_API_KEY`, `WIX_SITE_ID`, `WIX_MEMBER_ID` | Publishing |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Hold queue |
| `EVENTBRITE_TOKEN` | Eventbrite source (optional) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Email notifications |

Generate the Claude token with:

```bash
claude setup-token
```

That token authenticates against a Claude Max subscription, so runs bill
against the plan rather than at API rates.

**Never set `ANTHROPIC_API_KEY` in this repo.** Claude Code prioritises an API
key over subscription auth, so its presence silently moves every role call onto
pay-as-you-go billing. `pipeline/roles.py` strips it from the subprocess
environment as a second line of defence.

Everything except the Claude token degrades gracefully. A missing Eventbrite
token drops one source; missing Google credentials hold articles in the database
instead of Drive; missing SMTP logs notifications rather than sending them.

---

## The hold queue

Articles needing a human become Google Docs. Approve one by changing the line
at the top:

```
STATUS: HELD     →     STATUS: APPROVED
```

The next run picks it up, resumes it **at the stage that held it**, and resets
that stage's attempt counter. Two re-holds is the cap; after that the article
is archived and you get an email.

---

## Inherited specs

Voice, gates and publish mechanics are read at run time from
[Skill-Cabinet](https://github.com/tanzimozer/Skill-Cabinet) rather than copied
here, so a voice fix propagates across every TIMBR surface at once. It is a
public repo, so no token is needed.

| Path | Gives us |
|------|----------|
| `skills/timbr/magazine-eic/SKILL.md` | The Editor-in-Chief harness |
| `skills/timbr/magazine-eic/references/house.md` | Voice handbook |
| `timbr_eval_v2/RUBRIC.md` | Six of the seven judge dimensions |
| `timbr_eval_v2/hardgate.py` | Banned vocab, word counts, structure checks |

The seventh judge, `seo`, is defined here — the inherited rubric has no SEO
dimension and this pipeline is search-driven.

---

## Guardrails worth knowing

- **Never writes to `timbr.fit` or TIMBR-3.** Both site IDs are on a deny list
  enforced in code, not just documented.
- **State is committed every run.** That is the audit trail, and it doubles as
  the repo activity that stops GitHub disabling the schedule after 60 days.
- **Idempotency is two-level.** A source fingerprint skips repeats before any
  model call; a canonical id catches the same event arriving from several
  sources. Without both, an event in the 7-10 day window would be rewritten
  every day it sat there.
- **One job, not two.** The sweeps and the pipeline share a single workflow so
  nothing races on the state file.
