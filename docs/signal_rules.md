# Signal rules: first-seen, unchanged, and re-posted

These are the explicit rules for how the detector treats a posting it has
already encountered. They exist so that "we saw this before" is a decision we
made, not an accident of the database.

## 1. First-seen
A posting whose `(source, board_token, external_id)` is **not** already stored.

- Action: insert a new signal with `delivery_status = 'pending'`.
- It is the only case that produces a fresh alert.

## 2. Unchanged
A posting whose `(source, board_token, external_id)` **is** already stored and
whose fields are the same as before.

- Action: **no-op.** The `UNIQUE (source, board_token, external_id, signal_type)`
  constraint makes a re-insert harmless (`INSERT OR IGNORE`), so re-running the
  detector on the same board adds nothing.
- No new alert.

## 3. Likely re-posted
A posting with a **new** `external_id` but the same `(company_id, title)` as a
signal already seen **within the last 90 days**.

- Rationale: boards close a role and re-list it under a new posting id. The role
  is the same job, so re-alerting would be noise.
- Action: insert the row, but set `repost_of` to the original signal's id and
  `delivery_status = 'skipped'`, so it is recorded but **not delivered**.
- Outside the 90-day window it is treated as first-seen again — a role that
  reopens after months is a genuinely new signal.

## Money raised (SEC Form D)
The funding detector (`scripts/run_funding_detector.py`) follows the same three
rules, keyed on the filing instead of the posting:

- **First-seen:** a Form D accession number not yet stored. If the funding
  rule (`src/funding.py`, thresholds in `config/funding_rule.json`) calls it a
  hit, insert as `pending`.
- **Unchanged:** the same accession number again. `INSERT OR IGNORE`, no-op.
- **Same-day duplicate:** a second hit for the same company **filed on the same
  day** (companies often file one Form D per share class for one round). Insert
  with `repost_of` set to the first and `delivery_status = 'skipped'`.

Amendments (D/A) are never signals. The original filing carried the news, and
an amendment only revises it. Filings older than `lookback_days` (180) are
ignored, so the first run doesn't flood the CRM with years-old rounds.

## Why a time window
Without one, a company re-hiring the same title years later would be silently
swallowed as a "re-post". The 90-day window keeps recent duplicates quiet while
letting genuine re-openings through. The window is `REPOST_WINDOW_DAYS` in
`scripts/run_detector.py`.
