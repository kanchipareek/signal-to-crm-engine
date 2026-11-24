"""Runs the money-raised detector: list each company's SEC Form D filings,
classify them, and store the result.

Hits go to `signals` (signal_type 'money_raised'); near-misses and reviews go to
`signal_candidates`. The filing's accession number is the external id, so
re-running adds nothing - same idempotency as the job-board detector.

Which companies to check lives in config/sec_issuers.csv: one confirmed SEC
CIK per company. Fill it with scripts/find_sec_ciks.py, and confirm every CIK
by hand - a name search also returns investor SPVs and unrelated companies.

Usage:  python scripts/run_funding_detector.py
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fetch import FetchError
from src.funding import classify, in_window, load_rule, signal_title
from src.sec import fetch_form_d, filing_url, list_form_d
from scripts.run_detector import load_env

SOURCE = "sec_form_d"


def load_issuers(path):
    with Path(path).open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["cik"].strip()]


def store(conn, rule, company_id, cik, filing, label, reason, now):
    """Write one classified filing.

    Returns 'new_hit', 'same_day', 'unchanged', 'new_cand', 'seen_cand' or None (not stored).
    """
    url = filing_url(cik, filing["accession"])
    if label == "hit":
        # Companies often file two Form Ds on the same day for one round (one per
        # share class). Record the second, but link it to the first and don't alert.
        same_day = conn.execute(
            "SELECT id FROM signals WHERE company_id=? AND signal_type=? AND first_published=? "
            "AND external_id<>? ORDER BY id LIMIT 1",
            (company_id, rule["signal_type"], filing["filing_date"], filing["accession"])).fetchone()
        repost_of = same_day[0] if same_day else None
        cur = conn.execute(
            """INSERT OR IGNORE INTO signals
               (company_id, signal_type, source, board_token, external_id, source_url,
                title, matched_term, amount_usd, first_published, first_seen_at,
                delivery_status, repost_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, rule["signal_type"], SOURCE, cik, filing["accession"], url,
             signal_title(filing), reason, filing["total_sold"], filing["filing_date"], now,
             "skipped" if repost_of else "pending", repost_of))
        if not cur.rowcount:
            return "unchanged"
        return "same_day" if repost_of else "new_hit"
    if label in ("near_miss", "review"):
        cur = conn.execute(
            """INSERT OR IGNORE INTO signal_candidates
               (company_id, source, board_token, external_id, source_url, title,
                label, reason, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (company_id, SOURCE, cik, filing["accession"], url,
             f"SEC Form D filed {filing['filing_date']}", label, reason, now))
        return "new_cand" if cur.rowcount else "seen_cand"
    return None


def main():
    load_env()
    db_path = os.environ.get("LEAD_DB_PATH")
    if not db_path or not Path(db_path).exists():
        print("Set LEAD_DB_PATH in .env first.")
        raise SystemExit(1)

    rule = load_rule()
    issuers = load_issuers(os.environ.get("SEC_ISSUERS_CSV", ROOT / "config" / "sec_issuers.csv"))
    print(f"checking {len(issuers)} companies on SEC EDGAR")

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"new_hit": 0, "same_day": 0, "unchanged": 0, "new_cand": 0, "seen_cand": 0, None: 0}

    for row in issuers:
        cik = str(int(row["cik"]))
        company_id = int(row["company_id"])
        try:
            filings = [f for f in list_form_d(cik) if in_window(f["filing_date"], rule)]
        except FetchError as exc:
            print(f"  ! {row['name']}: {exc}")
            continue

        parsed_filings = []
        for f in filings:
            try:
                parsed_filings.append({**fetch_form_d(cik, f["accession"]), **f})
            except FetchError as exc:
                print(f"  ! {row['name']} {f['accession']}: {exc}")

        # Oldest first, and the biggest filing first within a day, so that when one
        # round has two same-day filings the larger one is the alert.
        parsed_filings.sort(key=lambda x: (x["filing_date"], -x["total_sold"]))
        company_hits = 0
        for filing in parsed_filings:
            label, reason = classify(filing, rule)
            result = store(conn, rule, company_id, cik, filing, label, reason, now)
            counts[result] += 1
            company_hits += result == "new_hit"

        conn.commit()
        print(f"  ok {row['name']:22} {len(filings):3} recent form d, {company_hits} new hits")

    conn.close()
    print(f"\nnew signals {counts['new_hit']} | same-day duplicates {counts['same_day']} | "
          f"unchanged {counts['unchanged']} | "
          f"new candidates {counts['new_cand']} | not_a_hit {counts[None]}")


if __name__ == "__main__":
    main()
