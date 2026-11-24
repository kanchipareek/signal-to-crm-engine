"""Runs the detector: fetch each board (greenhouse or ashby), classify every posting, store the result.

Hits go to `signals`; near-misses and reviews go to `signal_candidates`.
Re-running is safe - the unique keys stop duplicates, so nothing is written twice.

Rules for postings we've seen before (see docs/signal_rules.md):
  - first-seen : not stored yet            -> insert as 'pending' (a fresh alert)
  - unchanged  : same external_id          -> ignored, no new row
  - re-posted  : new external_id, but the same company+title within 90 days
                 -> stored with repost_of set and 'skipped' (no re-alert)
"""
import csv
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fetch import fetch_postings, FetchError
from src.match import classify, load_rule

SIGNAL_TYPE = "gtm_leadership_hire"
REPOST_WINDOW_DAYS = 90


def load_env(path=".env"):
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def find_repost(conn, company_id, title, cutoff):
    row = conn.execute(
        "SELECT id FROM signals WHERE company_id=? AND title=? AND first_seen_at>=? "
        "ORDER BY id LIMIT 1",
        (company_id, title, cutoff),
    ).fetchone()
    return row[0] if row else None


def main():
    load_env()
    db_path = os.environ.get("LEAD_DB_PATH")
    if not db_path or not Path(db_path).exists():
        print("Set LEAD_DB_PATH in .env first.")
        raise SystemExit(1)

    rule = load_rule()
    boards = [r for r in csv.DictReader(Path(os.environ.get("BOARDS_CSV", "config/boards.csv")).open(encoding="utf-8"))
              if r["board_token"].strip()]
    print(f"checking {len(boards)} boards")

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REPOST_WINDOW_DAYS)).isoformat(timespec="seconds")
    new_hits = reposts = unchanged = new_cands = seen_cands = skipped = 0

    for row in boards:
        token = row["board_token"].strip()
        source = (row.get("source") or "greenhouse").strip()
        company_id = int(row["company_id"])
        try:
            postings = fetch_postings(source, token)
        except FetchError as exc:
            print(f"  ! {row['name']}: {exc}")
            continue

        board_hits = 0
        for p in postings:
            label = classify(p["title"], p["department"], rule)
            if label == "hit":
                repost_of = find_repost(conn, company_id, p["title"], cutoff)
                status = "skipped" if repost_of else "pending"
                cur = conn.execute(
                    """INSERT OR IGNORE INTO signals
                       (company_id, signal_type, source, board_token, external_id,
                        source_url, title, department, first_published, first_seen_at,
                        delivery_status, repost_of)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (company_id, SIGNAL_TYPE, source, token, p["external_id"],
                     p["source_url"], p["title"], p["department"], p["first_published"],
                     now, status, repost_of))
                if cur.rowcount:
                    if repost_of:
                        reposts += 1
                    else:
                        new_hits += 1
                        board_hits += 1
                else:
                    unchanged += 1
            elif label in ("near_miss", "review"):
                cur = conn.execute(
                    """INSERT OR IGNORE INTO signal_candidates
                       (company_id, source, board_token, external_id, source_url,
                        title, department, label, first_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (company_id, source, token, p["external_id"],
                     p["source_url"], p["title"], p["department"], label, now))
                if cur.rowcount:
                    new_cands += 1
                else:
                    seen_cands += 1
            else:
                skipped += 1

        conn.commit()
        print(f"  ok {row['name']:22} {source:10} {len(postings):3} postings, {board_hits} new hits")
        time.sleep(0.5)

    conn.close()
    print(f"\nnew signals {new_hits} | reposts {reposts} | unchanged {unchanged} | "
          f"new candidates {new_cands} | not_a_hit {skipped}")


if __name__ == "__main__":
    main()
