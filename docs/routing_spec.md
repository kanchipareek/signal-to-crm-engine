# how the routing works

## what comes in
my detector posts a json signal to an n8n webhook. it's signed with hmac sha256 in the `X-Signature` header. a bad signature gets a 401. a missing field like `company_domain` gets a 400.

## signal types
| type | what it catches | task priority | task due |
|---|---|---|---|
| revenue_leader | revenue, sales, gtm, commercial leadership | high | 2 days |
| marketing_leader | marketing, growth, demand, brand leadership | medium | 3 days |
| review | regional and country heads, anything else | low | 5 days |

## what happens to the company
1. i look the company up in hubspot by domain. if it's not there, i create it.
2. if it already has an owner, i leave it alone and say so in `gtm_routing_note`.
3. if it has no owner, revenue and marketing go to the ae owner and review goes to the triage owner. the note says which fallback was used.
4. i create a dated task linked to the company.

## evidence and expiry
every company stores the job title, the posting url, the posting date, a unique key and an expiry date (30 days by default).

## duplicates
if the same signal key shows up again, i return `duplicate` and create nothing.

## when things break
if hubspot errors, n8n answers 500 so the detector retries a few times. permanent 4xx errors get dead-lettered.

## my 6 tests (`scripts/send_test_signals.py`)
1. revenue leader at a new company: created
2. marketing leader at an existing company: owner kept
3. regional head: sent to review
4. replay of test 1: duplicate
5. tampered signature: 401
6. missing domain: 400

all 6 passed on 15 nov 2026 against a hubspot free sandbox.

## what's not great yet
- the company level `gtm_signal_type` only shows the latest signal. at salesloft a later review signal overwrote an earlier revenue one. the tasks still keep the full history.
- i only have one owner in my sandbox, so i can't show ae and triage going to different people.
- `marketing_stack_hint` is just a hint. `none_found` doesn't mean they have no marketing platform.
- sandbox only. i haven't run this on a real client account.
