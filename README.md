# signal-to-crm-engine

i built this to catch two things that usually mean a company is about to spend on go-to-market: a senior gtm hire (vp of marketing, head of growth, cro, that kind of role) and a fresh funding round. when it spots one, it creates a task in hubspot with an owner and a due date, so someone actually follows up.

hires come from public greenhouse job boards. funding comes from sec form d filings.

> the list of accounts comes from my other project, [lead-enrichment-engine](https://github.com/kanchipareek/lead-enrichment-engine).

some numbers from my runs:

- in october 2026 i ran it over 18 boards (about 2,242 live postings). it stored 8 signals and held back 447 postings it wasn't sure about, so i could look at them myself. running it a second time added nothing (`0 new signals, 8 unchanged`), which is what i wanted. the numbers move around as boards change.
- for the hubspot demo i used a smaller list of 12 boards. that found 6 signals, held back 61, and all 6 went into hubspot (`delivered 6 | failed 0`). they ended up on 5 companies because two were at salesloft.

## quick start

```
git clone https://github.com/kanchipareek/signal-to-crm-engine.git
cd signal-to-crm-engine
python -m venv .venv
source .venv/bin/activate          # windows (git bash): source .venv/Scripts/activate
pip install -r requirements.txt
python -m pytest tests/ -q         # 72 passed
```

### trying it without project 1

normally this runs against project 1's database. if you just want to see it work on live data, build the same throwaway database ci uses. no keys needed:

```
python scripts/build_ci_db.py                               # companies + project 2 tables
LEAD_DB_PATH=ci.db python scripts/run_detector.py           # job boards
LEAD_DB_PATH=ci.db python scripts/run_detector.py           # run it again: 0 new
LEAD_DB_PATH=ci.db python scripts/run_funding_detector.py   # sec form d filings
LEAD_DB_PATH=ci.db python scripts/show_signals.py
```

for the 12-board list from the hubspot demo, set `BOARDS_CSV=config/boards_gtm.csv` before you build and run.

## what it does

- looks for two signals: a senior gtm hire, and a round of $5m or more on a form d. i kept it to these two so each one could be tuned properly.
- the rules sit in json (`config/match_rule.json` for hires, `config/funding_rule.json` for funding), so i can change them without touching python.
- when it can't tell, it holds the posting or filing back for me to check. it doesn't guess.
- running it twice, or sending the same signal twice, never creates a duplicate.
- if a delivery fails for a temporary reason, it retries a few times and waits longer each time. a permanent 4xx goes to a dead-letter status so it doesn't retry forever.
- every webhook is signed (hmac sha256 in an `X-Signature` header) and the receiver checks it.
- an n8n workflow turns each signal into a hubspot company plus a task for the right person.
- github actions runs it every 6 hours, and two runs can't overlap.

## how the hire signal works

```
greenhouse board api
        │  fetch.py  (retries)
        ▼
   match.py  ◄── config/match_rule.json
        │
        ▼
run_detector.py  ◄── github actions, every 6h
        │
        ▼
 signals table   (shared sqlite, keyed to project 1's companies)
        │
        ▼
   deliver.py   (signs, retries, dead-letters)
        │
        ▼
   n8n workflow  ──►  hubspot (company + task)
   (or webhook_server.py if i just want to log the events)
```

so it fetches postings, classifies them with the rule, saves the hits, and sends each one as a signed post. i can test each of those steps on its own.

## how the money-raised signal works

when a us company sells shares in a private round, it has to file a form d with the sec within 15 days. the filing is public and free, and it says how much was sold. i read those filings and turn the big ones into signals next to the hire ones.

```
config/sec_issuers.csv  (company -> its sec id, called a cik)
        │  sec.py  (edgar api, retries, stays under sec's 10 requests/sec)
        ▼
   funding.py  ◄── config/funding_rule.json   ($5m bar, 180-day window)
        │
        ▼
run_funding_detector.py  ──►  signals / signal_candidates
```

this is how i decide on each filing, checked in this order:

| filing | what i do |
|---|---|
| an amendment (d/a) | skip it. the original filing already had the news |
| a pooled investment fund (spv, fund) | skip it. that money went into a fund, not the company |
| filed more than 180 days ago | skip it. too old |
| nothing sold and no amount given | skip it. usually an internal restructuring |
| a merger or restructuring | hold it for me to look at |
| $5m or more sold | signal |
| something sold, but under $5m | near miss |
| nothing sold yet, but a $5m+ offering filed | hold it. the round's announced but the money isn't in yet |

each signal saves the amount in `amount_usd` and gets a title like `Raised $25M (SEC Form D)`. the filing's accession number is its id, so re-running doesn't add anything. some companies file two form ds on the same day for one round (one per share class). when that happens the bigger one becomes the signal and the smaller one gets linked to it, so hubspot only gets one task.

### why i match companies by hand

i couldn't trust searching sec by company name:

- "airtable" only brings up investor spvs like `Caffeinated Capital Airtable SPV, LLC`. airtable itself files as formagrid.
- "ramp" brings up `RAMP CORP`, which is a different company.
- "gong", "harvey" and "sierra" bring up random people and a bank.

a wrong match means a fake funding signal. so `config/sec_issuers.csv` has one cik per company, and i check each one myself. `scripts/find_sec_ciks.py` helps with that. it lists possible matches with their address and how many form ds they've filed, and it flags names that look like funds. it never writes to the csv.

```
python scripts/find_sec_ciks.py Vanta Ramp
```

for now i've added 8 companies where the legal name and hq city clearly match: vanta, deel, clari, drata, hebbia, zapier, notion and figma. 7 of them don't have a greenhouse board, so this signal covers accounts the hire one misses.

### did it work on real filings?

i ran it once with the window stretched to 10 years. it picked up rounds i already knew about: notion $260m (2021), figma $189.6m (2021), drata $200m (dec 2022), hebbia $93m (2024). it skipped every amendment, held drata's 2025 merger filing for review, and marked deel's small 2024 filings as near misses. with the normal 180-day window none of the 8 has filed recently, so a run today finds 0. that's correct.

## crm hand-off (n8n + hubspot)

`deliver.py` can send to my own little receiver or to an n8n workflow (`n8n/workflow.json`). this is what the workflow does with each signal:

1. checks the signature. a bad one gets a `401`.
2. checks the required fields. if something like `company_domain` is missing, it sends back a `400`.
3. works out the signal type. funding events are `funding_round`. for hires it goes by the job title.
4. looks up the company in hubspot by domain, and creates it if it's not there.
5. if the company already has an owner, it keeps them and writes that in `gtm_routing_note`.
6. if there's no owner, hires and funding rounds go to the ae owner and review signals go to the triage owner. the note says which one it used.
7. creates a task on the company with a priority and due date.
8. if the same signal shows up again, it says `duplicate` and doesn't create anything.
9. if hubspot throws an error, it returns `500` so the detector retries and the signal doesn't get lost.

| signal type | what it catches | priority | due in |
|---|---|---|---|
| `revenue_leader` | revenue, sales, gtm, commercial leaders | high | 2 days |
| `marketing_leader` | marketing, growth, demand, brand leaders | medium | 3 days |
| `funding_round` | a $5m+ round on a form d | medium | 3 days |
| `review` | regional and country heads, or anything unclear | low | 5 days |

each company also gets the job title (or `Raised $25M (SEC Form D)`), the link, the date, a unique key and an expiry date (30 days by default). that way every task has proof behind it. full details are in [docs/routing_spec.md](docs/routing_spec.md).

### setting it up yourself

1. install n8n in the project and start it:
   ```
   npm install n8n@2.46.6
   NODE_FUNCTION_ALLOW_BUILTIN=crypto ./node_modules/.bin/n8n
   ```
   the editor opens at `http://localhost:5678`. i used node v24. if you have `WEBHOOK_URL` set in your shell, unset it first. n8n reads that same name and shows the wrong editor address.
2. import `n8n/workflow.json` (workflow menu, import from file).
3. open the **Config** node and fill in the placeholders. put your webhook secret in place of `PASTE_YOUR_SECRET`, and your hubspot owner id in place of `PASTE_YOUR_OWNER_ID` (use it for both `ae_owner_id` and `triage_owner_id` if you only have one owner).
4. in hubspot, make a service key with `crm.objects.companies.read`, `crm.objects.companies.write` and `crm.objects.owners.read`. that was enough for me to create tasks on companies.
5. in n8n, add a **bearer auth** credential with that key and pick it on the four hubspot nodes (authentication: generic credential type, then bearer auth).
6. add these 9 custom company properties in hubspot (settings, properties, company properties). the internal names have to match exactly:

   | internal name | type |
   |---|---|
   | `gtm_signal_type` | dropdown: `revenue_leader`, `marketing_leader`, `funding_round`, `review` |
   | `gtm_signal_title` | single-line text |
   | `gtm_signal_url` | single-line text |
   | `gtm_signal_date` | date |
   | `gtm_signal_key` | single-line text |
   | `gtm_signal_status` | dropdown: `new`, `actioned`, `conversation`, `no_action`, `expired` |
   | `gtm_signal_expires` | date |
   | `marketing_stack_hint` | dropdown: `hubspot`, `marketo`, `pardot`, `none_found`, `not_checked` |
   | `gtm_routing_note` | single-line text |

7. publish the workflow and put these in `.env`:
   ```
   WEBHOOK_URL=http://localhost:5678/webhook/signal
   WEBHOOK_SECRET=<the same secret as in the Config node>
   ```
8. one test needs a company that already exists. create one in hubspot called `TEST Existing Co` with the domain `test-existing-co.example`, and make yourself the owner.
9. run the acceptance tests:
   ```
   python scripts/send_test_signals.py
   ```

## data model

there are four tables, and all of them point at project 1's `companies(id)`. i reuse project 1's companies here instead of making my own.

| table | what's in it |
|---|---|
| `signals` | one row per signal (hire or funding). unique on `(source, board_token, external_id, signal_type)`. also has `delivery_status`, `repost_of`, and `amount_usd` for funding. for funding rows, `source` is `sec_form_d`, `board_token` is the cik and `external_id` is the accession number. |
| `signal_candidates` | the near misses and the ones held for review. |
| `signal_deliveries` | one row per delivery attempt, so i can see the retries. |
| `webhook_events` | what the receiver has already seen, so it doesn't record anything twice. |

## configuration

put these in a `.env` file at the root (copy `.env.example`). `.env` is gitignored.

| variable | default | what it's for |
|---|---|---|
| `LEAD_DB_PATH` | — | path to the shared sqlite database (project 1's, or `ci.db` for the demo). required. |
| `WEBHOOK_SECRET` | — | secret for signing webhooks. both the sender and the receiver need it. |
| `WEBHOOK_URL` | `http://127.0.0.1:8000/webhook` | where signals get sent. for n8n use `http://localhost:5678/webhook/signal`. |
| `WEBHOOK_PORT` | `8000` | port for my built-in receiver. |
| `BOARDS_CSV` | `config/boards.csv` | which job boards to check. i use `config/boards_gtm.csv` for the hubspot demo. |
| `SEC_ISSUERS_CSV` | `config/sec_issuers.csv` | which companies to check on sec (company id to cik). |
| `DELIVER_PAUSE_SECONDS` | — | optional wait between deliveries. i use 12 for hubspot so i don't hammer the free tier. |

## usage

```
# 1. find signals
python scripts/run_detector.py
python scripts/run_funding_detector.py

# 2a. send them to n8n (with n8n running and the workflow published)
python src/deliver.py

# 2b. or use my built-in receiver (terminal 1), then send (terminal 2)
python scripts/webhook_server.py
python src/deliver.py

# see what got stored
python scripts/show_signals.py
```

if your database is from before i added funding, run this once first. otherwise `deliver.py` breaks on the missing column:

```
python scripts/run_sql.py sql/05_signals_amount.sql
```

new databases already have the column.

`deliver.py` only sends signals that haven't gone out yet. if you run it twice in a row, the second run says `delivered 0`.

## testing

```
python -m pytest tests/ -v
```

- `tests/test_match.py` has 30 cases for the hire rule: hits, near misses, review, department clashes and text cleanup.
- `tests/test_webhook.py` has 14 cases for when things go wrong: replays, bad signatures, broken bodies, running out of retries, dead-lettering and re-running the detector.
- `tests/test_payload.py` checks the company domain makes it into the payload.
- `tests/test_funding.py` has 27 cases for the funding signal: reading form d, every branch of the rule, the $5m line, the 180-day window, no duplicates, same-day filings and the payload.

`scripts/send_test_signals.py` also runs 7 end-to-end tests against the real n8n workflow and hubspot:

1. revenue leader at a new company: created
2. marketing leader at a company that already has an owner: owner kept
3. regional head: sent to review
4. test 1 sent again: duplicate
5. tampered signature: 401
6. missing domain: 400
7. money raised: comes through as `funding_round`

tests 1 to 6 passed on a free hubspot sandbox on 15 nov 2026. i haven't run test 7 yet. before i can, i need to re-import the updated workflow and add `funding_round` to the `gtm_signal_type` dropdown. all the test data uses fake `.example` domains. `scripts/check_invalid_signal.py` is a quick manual check that sends a bad signal.

## why i built it this way

- i used rules, not ml, because i can explain every decision and test it, and it doesn't need training data. any call it makes comes back to a line in the json.
- the job board's department field is too rough to trust ("sales and customer success"). so the title has to qualify a posting, and the department can only flag a clash.
- if i'm not sure, it goes to review. i'd rather look at a few extra than get wrong ones.
- funding comes from the sec and not from news. a form d gives me an actual dollar amount, so there's no headline to parse. the downside is it only covers us companies.
- if a job gets closed and re-listed within 90 days, i save it but don't send another alert. if it comes back after that, it fires again.
- if a company already has an owner in hubspot, i never change it.
- when something breaks i want to know. a bad signature is a 401, a bad payload is a 400, and a hubspot error is a 500 so the signal gets retried or dead-lettered.

## what's not great yet

- the signal fields on a hubspot company only show the latest signal. at salesloft, a review signal overwrote an earlier revenue one. the tasks still have the full history, but filtering companies by `gtm_signal_type` can hide the older one. a funding signal can overwrite a hire signal the same way.
- i only have one owner in my sandbox, so i can't show tasks going to different people.
- `marketing_stack_hint` is just a guess. `none_found` means i didn't see a tracking script on their homepage. they could still use one of these tools.
- i've only run this on a free hubspot sandbox with n8n on my laptop. it hasn't touched a real client account and isn't hosted anywhere.
- form d is us only. companies like canva, airwallex and miro never file one, and some us companies raise without filing. rippling, stytch and workato didn't show up on edgar at all.
- a form d doesn't always mean a new round. tender offers and secondary sales get one too (figma's $416m filing in 2024 looks like that). the form doesn't say which, so i treat the amount as something to check.
- i skip amendments, so the amount is from the first filing. vanta's 2022 round went from $103m to $150m in its amendment, but the signal says $103m.
- only 8 companies have a cik so far. i still need to go through the rest with `find_sec_ciks.py`.
- only greenhouse works for job boards. i haven't built lever. 18 of my 59 accounts have a public greenhouse board, so that limits how many i can watch.
- a re-listed job only counts as a repost if the company and title match exactly within 90 days. if they change the title, it looks new.
- logs are plain text, not json.

## project structure

```
config/    match_rule.json (hire rule), funding_rule.json (funding rule),
           boards.csv (job boards), boards_gtm.csv (12-board demo list),
           sec_issuers.csv (company -> sec cik)
src/       fetch.py  match.py  sec.py  funding.py  deliver.py
scripts/   run_detector.py  run_funding_detector.py  find_sec_ciks.py  webhook_server.py
           show_signals.py  run_sql.py  build_ci_db.py  send_test_signals.py  check_invalid_signal.py
n8n/       workflow.json (the hubspot hand-off, secrets swapped for placeholders)
sql/       schema and migrations
schemas/   signal.schema.json
tests/     test_match.py  test_funding.py  test_webhook.py  test_payload.py
docs/      signal_rules.md (how repeats are handled), routing_spec.md (the crm routing)
.github/   workflows/detect.yml (the scheduled run)
```

## license

mit. see license.
