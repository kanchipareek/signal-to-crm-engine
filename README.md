# signal-to-crm-engine

**it watches public greenhouse job boards, catch senior go-to-market hires, and turn each one into an owned, dated task in hubspot.**

> it reads the qualified accounts from [project: lead-enrichment-engine](https://github.com/kanchipareek/lead-enrichment-engine).


when a company posts a job for a vp of marketing, head of growth or cro, it's about to spend money on go-to-market. that posting is one of the earliest public signs a company is scaling. i built this to watch a list of job boards, catch those postings, and hand each one to a crm so a human can act on it.

on a full pass in october 2026 it checked **18 boards (~2,242 live postings)**, stored **8 signals** (senior gtm leadership roles, including regional heads), and held back **447 near-miss / review cases** instead of guessing. running it again added nothing: `0 new signals, 8 unchanged`. live numbers drift as boards change.

for the crm demo i used a smaller 12-board list. that run found **6 signals**, held back **61** for review, and delivered all 6 into hubspot with `delivered 6 | failed 0`. they landed as 5 companies, because two of the signals were at the same company (salesloft).

## quick start

```
git clone https://github.com/kanchipareek/signal-to-crm-engine.git
cd signal-to-crm-engine
python -m venv .venv
source .venv/bin/activate          # windows (git bash): source .venv/Scripts/activate
pip install -r requirements.txt
python -m pytest tests/ -q         # 45 passed
```

### try it without project 1

the detector normally runs against project 1's database. to see it work on live greenhouse data with no keys, build the same throwaway database ci uses:

```
python scripts/build_ci_db.py                       # 18 companies + project 2 tables
LEAD_DB_PATH=ci.db python scripts/run_detector.py   # fetch, classify, store
LEAD_DB_PATH=ci.db python scripts/run_detector.py   # again: 0 new, N unchanged
LEAD_DB_PATH=ci.db python scripts/show_signals.py
```

to use the smaller 12-board list from the crm demo, set `BOARDS_CSV=config/boards_gtm.csv` before building and running.

## features

- **one signal, done properly.** a senior gtm leadership hire, not a vague "job change" feed.
- **rule in config, not code.** the match rule lives in `config/match_rule.json`, so i can change it without touching python.
- **precision-first.** unclear postings go to a review queue instead of being guessed at.
- **idempotent by construction.** re-running the detector or replaying a delivery never creates a duplicate.
- **failure-aware delivery.** bounded-backoff retries on transient errors, dead-lettering on permanent 4xx.
- **signed webhook.** hmac sha256 over the raw body in an `X-Signature` header, constant-time compare, one deterministic effect per event.
- **crm hand-off.** an n8n workflow turns each signal into a hubspot company and a dated task for the right owner.
- **scheduled.** the detector runs on github actions every 6 hours, with an overlap guard.

## how it works

```
greenhouse board api
        │  fetch.py  (bounded retries)
        ▼
   match.py  ◄── config/match_rule.json   (the rule lives here)
        │
        ▼
run_detector.py  ◄── github actions cron (every 6h)
        │
        ▼
 signals table   (shared sqlite, keyed to project 1's companies)
        │
        ▼
   deliver.py   (signs the body, retries, dead-letters)
        │  signed post
        ▼
   n8n workflow  ──►  hubspot (company + task)
   (or webhook_server.py ──► webhook_events, if i just want to record events)
```

a posting moves through four stages: **fetch** (greenhouse board api, with retries), **classify** (the rule from `config/match_rule.json`), **store** (a hit becomes a row in `signals`), and **deliver** (a signed post, recorded exactly once). each stage is independent and testable on its own.

## crm hand-off (n8n + hubspot)

`deliver.py` can post to my built-in receiver or to an n8n workflow. the workflow lives in `n8n/workflow.json`. here's what it does with each signal:

1. checks the hmac signature. bad signature gets a `401`.
2. checks the required fields. a missing field like `company_domain` gets a `400`.
3. decides the signal type from the job title.
4. looks the company up in hubspot by domain, and creates it if it isn't there.
5. if the company already has an owner, i keep it and say so in `gtm_routing_note`.
6. if it has no owner, revenue and marketing signals go to the ae owner and review goes to the triage owner. the note says which fallback was used.
7. creates a task linked to the company, with a priority and a due date.
8. if the same signal comes in again, it answers `duplicate` and creates nothing.
9. if hubspot errors, it answers `500` so the detector retries instead of losing the signal.

| signal type | what it catches | task priority | task due |
|---|---|---|---|
| `revenue_leader` | revenue, sales, gtm, commercial leadership | high | 2 days |
| `marketing_leader` | marketing, growth, demand, brand leadership | medium | 3 days |
| `review` | regional and country heads, anything else | low | 5 days |

every company also stores the job title, posting url, posting date, a unique key and an expiry date (30 days by default), so there's evidence behind every task. full details are in [docs/routing_spec.md](docs/routing_spec.md).

### set it up yourself

1. install n8n locally in the project and start it:
   ```
   npm install n8n@2.46.6
   NODE_FUNCTION_ALLOW_BUILTIN=crypto ./node_modules/.bin/n8n
   ```
   the editor opens at `http://localhost:5678`. i ran it on node v24. if `WEBHOOK_URL` is set in your shell, unset it before starting n8n, because n8n reads that name too and prints a wrong editor address.
2. import `n8n/workflow.json` (workflow menu, then import from file).
3. open the **Config** node and replace the placeholders: `PASTE_YOUR_SECRET` with your webhook secret, and `PASTE_YOUR_OWNER_ID` with your hubspot owner id (for both `ae_owner_id` and `triage_owner_id` if you only have one owner).
4. in hubspot, create a service key with these scopes: `crm.objects.companies.read`, `crm.objects.companies.write`, `crm.objects.owners.read`. in my account that was enough to create tasks linked to companies.
5. in n8n, create a **bearer auth** credential with that key, and select it on the four hubspot nodes (authentication: generic credential type, then bearer auth).
6. create these 9 custom company properties in hubspot (settings, properties, company properties). the internal names have to match exactly:

   | internal name | type |
   |---|---|
   | `gtm_signal_type` | dropdown: `revenue_leader`, `marketing_leader`, `review` |
   | `gtm_signal_title` | single-line text |
   | `gtm_signal_url` | single-line text |
   | `gtm_signal_date` | date |
   | `gtm_signal_key` | single-line text |
   | `gtm_signal_status` | dropdown: `new`, `actioned`, `conversation`, `no_action`, `expired` |
   | `gtm_signal_expires` | date |
   | `marketing_stack_hint` | dropdown: `hubspot`, `marketo`, `pardot`, `none_found`, `not_checked` |
   | `gtm_routing_note` | single-line text |

7. publish the workflow, then set these in `.env`:
   ```
   WEBHOOK_URL=http://localhost:5678/webhook/signal
   WEBHOOK_SECRET=<the same secret you put in the Config node>
   ```
8. for the test that expects an existing company, create a hubspot company named `TEST Existing Co` with domain `test-existing-co.example` and yourself as owner.
9. run the 6 acceptance tests:
   ```
   python scripts/send_test_signals.py
   ```

## data model

four tables, all keyed to [project 1](https://github.com/kanchipareek/lead-enrichment-engine)'s `companies(id)`. this project reads that identity. it doesn't rebuild it.

| table | holds |
|---|---|
| `signals` | one row per hit. `UNIQUE(source, board_token, external_id, signal_type)`; `delivery_status`; `repost_of`. |
| `signal_candidates` | the near-miss / review holding pen. |
| `signal_deliveries` | one row per delivery attempt (retry history). |
| `webhook_events` | the receiver's idempotency ledger. |

## configuration

set these in a `.env` file at the project root (copy `.env.example`). `.env` is gitignored.

| variable | default | description |
|---|---|---|
| `LEAD_DB_PATH` | — | path to the shared sqlite database (owned by project 1, or `ci.db` for the demo). required. |
| `WEBHOOK_SECRET` | — | shared secret for hmac signing. required by the receiver and the sender. |
| `WEBHOOK_URL` | `http://127.0.0.1:8000/webhook` | where the sender posts. for n8n use `http://localhost:5678/webhook/signal`. |
| `WEBHOOK_PORT` | `8000` | port the built-in receiver listens on. |
| `BOARDS_CSV` | `config/boards.csv` | which list of job boards to check. i use `config/boards_gtm.csv` for the crm demo. |
| `DELIVER_PAUSE_SECONDS` | — | optional. seconds to wait between deliveries. i set 12 for the hubspot demo so the free tier isn't hammered. |

## usage

```
# 1. detect: fetch the boards, classify, store signals
python scripts/run_detector.py

# 2a. deliver to n8n (n8n running and the workflow published)
python src/deliver.py

# 2b. or receive with the built-in receiver instead (terminal 1), then deliver (terminal 2)
python scripts/webhook_server.py
python src/deliver.py

# inspect what was stored
python scripts/show_signals.py
```

delivery only sends signals that haven't been delivered yet, so running `deliver.py` twice in a row says `delivered 0` the second time. that's the idempotency doing its job.

## testing

```
python -m pytest tests/ -v
```

- `tests/test_match.py`: 30 cases covering the rule (hits, near-misses, review, department conflicts, text normalisation).
- `tests/test_webhook.py`: 14 cases covering failure handling (replay gives one row, tampered signature gives 401, malformed body gives 400, retry exhaustion, dead-letter, detector re-run).
- `tests/test_payload.py`: checks the event payload includes the company domain.

and 6 end-to-end acceptance tests against the real n8n workflow and hubspot, with `scripts/send_test_signals.py`:

1. revenue leader at a new company: created
2. marketing leader at an existing company: owner kept
3. regional head: sent to review
4. replay of test 1: duplicate
5. tampered signature: 401
6. missing domain: 400

all 6 pass against a hubspot free sandbox (run on 15 nov 2026). the script uses fake `.example` domains. `scripts/check_invalid_signal.py` is a small manual check for a bad signal.

## design decisions

- **rule-driven, not ml.** explainable, testable, needs no training data. every decision traces to a line in `config/match_rule.json`.
- **department is a conflict check, not a source of hits.** board department metadata is a coarse bucket ("sales and customer success"), so a hit has to be earned by the title.
- **a review bucket instead of a forced yes/no.** unclear postings are held for a human, never guessed.
- **90-day re-post window.** a job closed and re-listed under a new id is recorded but not re-alerted. a role that reopens after months fires again.
- **existing owners are never overwritten.** the workflow only assigns an owner when the company has none.
- **loud failures.** bad signature is a 401, bad payload is a 400, and a hubspot error is a 500 so the detector retries or dead-letters instead of silently dropping a signal.

## known limitations

- **the company-level signal fields show only the latest signal.** at salesloft, a later review signal overwrote an earlier revenue one on the company record. the tasks keep the full history, but filtering companies by `gtm_signal_type` can hide an earlier signal.
- **one owner in my sandbox.** the ae and triage routing works, but with a single owner i can't show it going to different people.
- **`marketing_stack_hint` is a hint.** `none_found` means i didn't see a tracking script on the page, not that the company has no marketing platform.
- **sandbox only.** this ran on a hubspot free account and a local n8n. i haven't run it on a real client account.
- **n8n runs on my laptop.** the hubspot hand-off isn't hosted anywhere.
- **lever is not built.** only greenhouse is supported. of 59 candidate accounts, 18 have a public greenhouse board. that's a real coverage limit, not a bug.
- **re-posts match exactly.** a re-listed job is caught only when company and title match within 90 days. a retitled re-post looks new.
- **logs are plain text,** not structured json.

## project structure

```
config/    match_rule.json (the rule), boards.csv (target boards), boards_gtm.csv (12-board crm demo list)
src/       fetch.py  match.py  deliver.py
scripts/   run_detector.py  webhook_server.py  show_signals.py  run_sql.py  build_ci_db.py
           send_test_signals.py  check_invalid_signal.py
n8n/       workflow.json (the hubspot hand-off, secrets replaced with placeholders)
sql/       schema migrations (signals, candidates, deliveries, webhook events)
schemas/   signal.schema.json
tests/     test_match.py  test_webhook.py  test_payload.py
docs/      signal_rules.md (first-seen / unchanged / re-posted), routing_spec.md (the crm routing)
.github/   workflows/detect.yml (scheduled run)
```

## license

mit. see license.