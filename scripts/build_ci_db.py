"""Builds a throwaway database for the CI smoke run.

GitHub Actions has no access to Project 1's real database, so this makes a
minimal stand-in: `companies` comes from config/boards.csv and config/sec_issuers.csv, and the Project 2
tables are created from the SQL files. The detector then runs against this
exactly as it would against the real one - it just starts from empty signals.
"""
import csv
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    db = Path("ci.db")
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, domain TEXT);")
    with Path(os.environ.get("BOARDS_CSV", ROOT / "config" / "boards.csv")).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["board_token"].strip():
                conn.execute(
                    "INSERT OR IGNORE INTO companies(id, name, domain) VALUES (?,?,?)",
                    (int(row["company_id"]), row["name"], row["domain"]),
                )
    # companies checked by the money-raised detector, even without a job board
    with Path(os.environ.get("SEC_ISSUERS_CSV", ROOT / "config" / "sec_issuers.csv")).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["cik"].strip():
                conn.execute(
                    "INSERT OR IGNORE INTO companies(id, name, domain) VALUES (?,?,?)",
                    (int(row["company_id"]), row["name"], row["domain"]),
                )
    for name in ("01_signals.sql", "02_signal_candidates.sql", "03_webhook_events.sql"):
        conn.executescript((ROOT / "sql" / name).read_text())
    conn.commit()
    count = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    conn.close()
    print(f"built {db} with {count} companies")


if __name__ == "__main__":
    main()
