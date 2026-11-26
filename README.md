# signal-to-crm-engine

i built this to catch two things that usually mean a company is about to spend on go-to-market: a senior gtm hire (vp of marketing, head of growth, cro, that kind of role) and a fresh funding round. when it spots one, it creates a task in hubspot with an owner and a due date, so someone actually follows up.

hires come from public job boards on greenhouse and ashby. funding comes from sec form d filings.

it can also work the other way round: give it a company that sells something (say nozzle.io), and it finds companies that look ready to buy from them right now, with the reason and the founders' names. see [find leads for any seller](#find-leads-for-any-seller).

> the list of accounts comes from my other project, [lead-enrichment-engine](https://github.com/kanchipareek/lead-enrichment-engine).

some numbers from my runs:

- in october 2026 i ran it over 18 boards (about 2,242 live postings). it stored 8 signals and held back 447 postings it wasn't sure about, so i could look at them myself. running it a second time added nothing (`0 new signals, 8 unchanged`), which is what i wanted. the numbers move around as boards change.
- in november 2026 i added ashby, which brought me from 18 boards to 33. the full run went through about 4,200 postings and found 26 signals (15 of them from ashby boards), plus 4 reposts it saved but didn't alert on. running it again gave `0 new, 30 unchanged`.
- for the hubspot demo i used a smaller list of 12 boards. that found 6 signals, held back 61, and all 6 went into hubspot (`delivered 6 | failed 0`). they ended up on 5 companies because two were at salesloft.

## quick start

```
git clone https://github.com/kanchipareek/signal-to-crm-engine.git
cd signal-to-crm-engine
python -m venv .venv
source .venv/bin/activate          # windows (git bash): source .venv/Scripts/activate
pip install -r requirements.txt
python -m pytest tests/ -q         # 224 passed
```

### trying it without project 1

normally this runs against project 1's database. if you just want to see it work on live data, build the same throwaway database ci uses. no keys needed:

```
python scripts/build_ci_db.py                               # companies + project 2 tables
LEAD_DB_PATH=ci.db python scripts/run_detector.py           # job boards (greenhouse + ashby)
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
greenhouse or ashby board api   (config/boards.csv says which)
        │  fetch.py  (retries, same posting shape from both)
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

### greenhouse and ashby

each row in `config/boards.csv` has a `source` (`greenhouse` or `ashby`) and a `board_token`. `fetch.py` turns both into the same posting format, so the rule and everything after it doesn't care where a job came from. on ashby i join the department and team into one string ("Go To Market Marketing"), since that's what the rule checks for clashes. i also skip jobs ashby marks as unlisted.

`scripts/find_boards.py` fills in missing boards. it tries greenhouse first, then ashby. ashby's job list doesn't include the company name, so i check ashby boards a stricter way: the website ashby has for that board has to be the account's domain, and the board needs at least one open job. that check caught a few:

- "circle" on ashby is circle.so, not my circle.com, so it got skipped.
- everbridge's board has no website listed, so i couldn't confirm it.
- deel, loom, stytch and wiz have empty ashby boards. they've probably moved to something else.

to get the website i use a lookup that ashby's own job pages use. it isn't a documented api, so i only use it in the finder. the detector itself only calls ashby's public posting api.

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

for now i've added 8 companies where the legal name and hq city clearly match: vanta, deel, clari, drata, hebbia, zapier, notion and figma. deel, clari and hebbia don't have a job board i can read, so for those this is the only signal i get.

### did it work on real filings?

i ran it once with the window stretched to 10 years. it picked up rounds i already knew about: notion $260m (2021), figma $189.6m (2021), drata $200m (dec 2022), hebbia $93m (2024). it skipped every amendment, held drata's 2025 merger filing for review, and marked deel's small 2024 filings as near misses. with the normal 180-day window none of the 8 has filed recently, so a run today finds 0. that's correct.

## find leads for any seller

everything above watches my own account list. this flips it around. i type in any startup or agency, and it finds companies that look ready to buy from them right now, tells me why, and gives me the founders' names.

```
python scripts/find_leads.py theirdomain.com
```

what it does:

1. **works out who they sell to.** it reads their homepage, pricing, customers and about pages, and gemini writes a profile: what they sell, who buys it (and which industries those buyers are in), which job titles are buying signals, the news searches to run, the competitors to skip, and what size of raise means there's budget. it saves this to `config/sellers/<their-domain>.json` so i can read it, fix it, and re-run without asking gemini again.
2. **finds companies that could be buyers.**
   - **funding news, us and india.** every news search runs on both the us and the india editions of google news, and it also reads inc42, entrackr and yourstory directly. inc42's funding feed stopped updating in 2023, so it pages back through inc42's live feed until the window is covered. some searches are general, and some are aimed at the seller's buyers ("mortgage lender raises", "bengaluru startup raises").
   - **sec form d.** every company that filed a form d in the window, cut down to real raises in the seller's industries (funds, llcs and amendments dropped).
   - my account list, and 20 to 30 likely buyers from the profile. competitors are never on that list.
3. **checks each one for signals.**
   - **funding:** the raise, with the amount, the round and the date. rupees ("rs 40 crore", "₹10 cr"), euros and pounds become rough dollars, and the original wording stays next to it. raises with no amount stay in as "amount not disclosed", because strategic and pe deals often don't say. the window is the last 90 days (`--days` to change it), and a seller's minimum raise can go as low as $1m.
   - **hiring, two kinds.**
     - *team hires* (`hire_keywords`): they're building the team that would buy the product. "head of seo" for an seo tool.
     - *ops-pain hires* (`ops_pain_keywords`): they're hiring people to do by hand what the seller automates. "loan processor" or "claims specialist" for a document-parsing api. this is often the stronger signal, so it scores like a senior hire. the report says which kind hit, and which word matched.
   - the jobs come from greenhouse, ashby, lever and workable, or the company's own `/careers` or `/jobs` page when it doesn't use a job board. a board only counts if it provably belongs to the company: its own site links to it, or the board names the company or lists its domain (the circle.so board doesn't count for circle.com). careers pages are on the company's own domain, so they can't belong to anyone else.

   then a **fit check**: does each company actually buy what the seller sells? it says yes, maybe or no with a one-line reason. "no" gets dropped, and the report lists those at the bottom in case it got one wrong. a clear fit ranks above a company with more signals but an unclear fit.
4. **finds the people.** if the company has a form d, the people on it. if not (any indian or uk startup), the founder, ceo or coo from the raise article and the company's own `/about`, `/team` and `/company` pages. it never guesses: a name only counts if a title like "co-founder & ceo" is right next to it. every person keeps the url they were found on.
5. **checks the evidence again, at run time.** every job url is fetched again at the end. it has to return 200 and still show the role, or the job is dropped. a lead with nothing left gets dropped too. every lead in the report has its signals listed with a date and a source url.
6. **writes a ranked lead list** to `leads/<seller>-<date>.md` and `.csv`.

form d gives a legal name, not a website, so i guess the domain from the name (`Deep Cogito Inc.` → `deepcogito.com`, `Perry AI` → `perry.ai`) and check the homepage actually belongs to them. parked domains and redirects somewhere else get rejected. guessed websites are labelled in the report so i know to double check them.

### running it without a key

everything works without `GEMINI_API_KEY`. the parts that would use gemini fall back to rules, and the report labels what was made that way as `rule-based`:

- **reading headlines:** patterns like "`<company>` raises / secures / bags / nets `$X` `<round>`", "`<investor>` invests in `<company>`", and "launches `<company>` with `$X`". for a launch headline with no amount ("nutanix founder ajeet singh floats new ai martech startup different"), it reads the opening of the article itself for the raise.
- **the fit check:** keyword matching against the profile's `buyers`, `buyer_industries` and `news_queries`. a company hiring ops-pain roles counts as a fit. a competitor, or a company whose description shares nothing with the buyers, is a "no".
- **the seller profile:** there's no rule-based version of this. write it by hand: copy `config/sellers/_template.json`. the other files in `config/sellers/` are filled-in examples.

with a key, gemini does all three, and if a batch fails twice that batch falls back to the rules instead of being skipped.

### the gemini key

get a free key from google ai studio and put it in `.env` as `GEMINI_API_KEY=...`.

two things i ran into:

- **the key has to come from a project with no billing.** my first key was in a project with prepaid billing and no credits, and every call came back `402 prepayment credits are depleted`, even on the free models. a key from a fresh project with no billing works.
- **the bigger free model is often busy.** `gemini-3.8-flash` came back `503 high demand` a few times. if that happens it retries once, then switches to `gemini-3.5-flash-lite`.

some sites block scripts (gusto.com sends a 403 to anything automated). when that happens, gemini writes the profile from what it already knows about the company. the run tells you so, and the profile is marked `check it`. if gemini doesn't know the company, it stops and asks for a hand-written profile instead of guessing.

i went with gemini because the free tier is enough for this. only public text gets sent (websites and headlines), and on the free tier google can use it to improve its products, which is fine for public pages. any llm would do. gemini is just the free one.

google news also rate-limits: it sent a `429` after too many searches in a row. so the searches are spaced 1.5 seconds apart, and a 429 is retried after a pause.

### my test runs

**unsiloed.ai (a document-parsing api), with gemini. this run made me add the news search and the fit check.** the first time i ran it, two things were wrong:

- it missed perry ai, which had just raised $2.2m for ai legal software that reads fund documents. that's exactly unsiloed's buyer. perry is a london company, so it never filed a form d, but six outlets covered the raise. form d alone can't see raises like that.
- it ranked perplexity, anthropic and scale ai near the top. gemini's likely buyers were famous ai companies, one of them a competitor (llamaindex sells its own parser), and its hiring keywords ("machine learning", "llm") matched every ai company. they had signals, but they'd never buy a parsing api.

after the fixes, the same run:

- went through 1,620 form ds and 284 funding headlines from google news (130 raises worldwide), and had 166 companies with a signal.
- the fit check dropped 76 of them, each with a reason, like *"focuses on brain-computer interfaces rather than document parsing"* or *"builds social humanoid robots"*. perplexity, anthropic and scale are gone.
- that left 90 leads, 22 of them clear fits. perry ai is one of the top 15: *"legal tech platform providing ai-powered legal software that requires contract ingestion and processing"*, $2.2m seed, found in the news. lawster (legal) and pedestal ai are up there too.
- gemini's new profile lists competitors to skip and names real document-heavy companies as likely buyers (klarity, ocrolus, robin ai, inscribe). its hiring keywords are specific: "contract analysis", "claims automation", "pdf parsing".

one fit-check batch came back as broken json twice. those 15 companies stayed as "maybe" instead of crashing the run.

**siege media (an seo and content agency), with gemini.** i gave it `siegemedia.com` and nothing else. gemini read their site and worked out that they sell seo, content and digital pr to b2b saas, fintech and e-commerce companies that rely on organic traffic. it picked seo, content and digital pr job titles as the hiring signals, left out paid and social roles, and set the raise range at $2m–$50m. the run went through the same 1,620 form ds, found 73 companies that raised in those industries, and came out with 85 leads.

gemini's profile was a good start, but it was a bit too generous:

- it added biotechnology and health care as industries, so biotech companies like abvc biopharma came through as leads for an seo agency. they raised money, but they're not siege media's buyers.
- it picked keywords like "brand marketing" and "demand generation", so a "brand marketing lead" job counted as a buying signal.

the fit check now catches leads like that. you can also open `config/sellers/siegemedia-com.json`, cut those, and re-run. the saved profile gets reused, so it doesn't call gemini again.

**gusto (payroll and hr), with gemini.** their site blocks scripts, so gemini wrote the profile from what it already knows: small businesses and startups, with hiring signals like head of people, hr manager and people ops. that's very different from an seo agency's, which is the point. the profile is marked `check it`.

**nozzle.io (an seo tracking tool), by hand.** i tested it on nozzle.io (an seo tracking tool), with a profile i wrote by hand because i didn't have a key yet. over the last 14 days of form ds it went through 1,620 filings, kept 388 after dropping funds and llcs, and found 43 companies that raised $5m+ in nozzle's industries. with my account list added, that came out to 49 leads, and 45 of them had founder names from their filing. the top one was the kind of lead i wanted: zeely raised $17m and is hiring a marketing analytics lead.

what's still weak:

- **the fit check is only as good as the description.** when a company's homepage won't load and the headline is vague, gemini has to guess, and it says "maybe". the "dropped" list is at the bottom of every report, so you can catch a wrong "no".
- **leads with the same score are in alphabetical order.** there are a lot of 8s.
- **people pages vary a lot.** founder names come from the raise article and the company's about or team page. when a site loads its team with javascript, or the article only names investors, there's no name. it doesn't guess.
- **about a quarter of the funded companies had no website i could confirm.** the founders' names are still there, so you can search for them.
- **the website guess can be wrong.** alcar inc. (darien, ct) first got matched to a swedish alcar.se. i fixed that one (redirects to foreign domains are rejected now), but guessed sites still need a look.
- **hiring is only checked at companies it already found.** there's no list of every job board out there. a lender hiring loan processors only shows up if it raised in the window, or it's on my list or the profile's likely buyers.
- **the rule-based headline reader can't read every headline.** "startup supporting caregivers raises $3.5m" never names the company, so the description becomes the name. the fit check judges it like any other lead.
- **google news links are redirects,** so the raise article is only read for inc42, entrackr and yourstory items. for the rest, people come from the company's own pages.

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

- `tests/test_match.py` has 33 cases for the hire rule: hits, near misses, review, department clashes, text cleanup, and making sure "chief of staff, sales" goes to review instead of looking like a sales leader.
- `tests/test_webhook.py` has 14 cases for when things go wrong: replays, bad signatures, broken bodies, running out of retries, dead-lettering and re-running the detector.
- `tests/test_payload.py` checks the company domain makes it into the payload.
- `tests/test_ashby.py` has 14 cases for ashby: the posting format, skipping unlisted jobs, picking the right fetcher, and the finder's domain check (including the circle.so case).
- `tests/test_leads.py` has 48 cases for the lead finder: guessing websites (rejecting parked, foreign and site-builder ones, picking different.ai over different.com, elio.mortgage), team and ops-pain hires, sec name matching, scoring, the seller profile, and the gemini client with the network mocked out.
- `tests/test_news_fit.py` has 13 cases for the news search and the fit check: us and india editions, deduping headlines, competitors kept out of the likely buyers, a broken gemini batch falling back to the keyword check, and the keyword check on its own with no key.
- `tests/test_news_rules.py` has 34 cases for reading headlines without gemini, on real headlines: rupees, euros and pounds, rounds, launch and strategic-investment headlines, and things that aren't raises.
- `tests/test_boards.py` has 16 cases for lever, workable and careers pages, and the check that a board really belongs to the company's domain.
- `tests/test_people.py` has 6 cases for founder and ceo names outside sec filings, including that it doesn't guess and doesn't take title words like "global sales" for a name.
- `tests/test_evidence.py` has 8 cases for the run-time recheck: dead or changed jobs dropped, leads with no evidence dropped, every row with a date and a url, and a company with two raises showing up once.
- `tests/test_acceptance.py` has 10 acceptance cases. they run the real lead finder offline, on feeds, job boards and company pages recorded from a live run on 25 nov 2026 (`tests/fixtures/acceptance/`, refreshed with `python scripts/capture_acceptance.py`), with no gemini key, so they give the same answer every time:
  - **rightfit.so** has to surface at least 3 of different.ai, firstwork.com, shamrockus.ai and humynlabs.ai. it surfaces all four, with the right raises ($48m seed, $15m series a, $5m seed, $47m series a). different, firstwork and humyn come with their real engineering, design and product roles. shamrock ai's site can't be found from its name or its raise articles, so its roles can't be read.
  - **unsiloed.ai** has to surface at least 3 of elio.mortgage, trovy.com, tomo.com, aurenity.com and alpine-specialty.com. it surfaces all five. elio, aurenity (great hill investment, amount not disclosed) and alpine specialty come from the news. tomo and trovy come in through a list, the way a real run would only see them, and their real loan processor jobs make them ops-pain fits.
  - every lead in both has signals with a date and a source url, and every person has a source.
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
- only greenhouse and ashby work for job boards. i haven't built lever yet. 33 of my 59 accounts have a board i can read (18 greenhouse, 15 ashby), so the other 26 only get the funding signal if they have a cik, and otherwise nothing.
- the ashby finder relies on an undocumented ashby lookup for the company website. if ashby changes it, the finder stops filling ashby boards. the detector keeps working because it doesn't use that lookup.
- a re-listed job only counts as a repost if the company and title match exactly within 90 days. if they change the title, it looks new.
- logs are plain text, not json.

## project structure

```
config/    match_rule.json (hire rule), funding_rule.json (funding rule),
           boards.csv (job boards, greenhouse or ashby), boards_gtm.csv (12-board demo list),
           sec_issuers.csv (company -> sec cik),
           sellers/ (one profile per seller for the lead finder, plus _template.json)
src/       fetch.py  match.py  sec.py  funding.py  deliver.py  seller.py  leads.py  news.py  fit.py
           people.py  evidence.py  llm.py
scripts/   run_detector.py  run_funding_detector.py  find_leads.py  capture_acceptance.py  find_sec_ciks.py  webhook_server.py
           show_signals.py  run_sql.py  build_ci_db.py  send_test_signals.py  check_invalid_signal.py
n8n/       workflow.json (the hubspot hand-off, secrets swapped for placeholders)
sql/       schema and migrations
schemas/   signal.schema.json
tests/     test_match.py  test_ashby.py  test_funding.py  test_leads.py  test_news_fit.py  test_news_rules.py
           test_boards.py  test_people.py  test_evidence.py  test_acceptance.py  test_webhook.py  test_payload.py
           fixtures/acceptance/ (recorded real data for the acceptance tests)
docs/      signal_rules.md (how repeats are handled), routing_spec.md (the crm routing)
.github/   workflows/detect.yml (the scheduled run)
```

## license

mit. see license.
