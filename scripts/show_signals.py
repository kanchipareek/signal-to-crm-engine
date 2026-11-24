"""Prints the signals I've stored, so I can eyeball whether they're real.

Usage:  python scripts/show_signals.py
"""
import os
import sqlite3
from pathlib import Path


def load_env(path=".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main():
    load_env()
    conn = sqlite3.connect(os.environ["LEAD_DB_PATH"])
    rows = conn.execute("""
        SELECT c.name, s.title, s.department, s.source_url, s.signal_type
        FROM signals s JOIN companies c ON c.id = s.company_id
        ORDER BY s.signal_type, c.name, s.title
    """).fetchall()
    print(f"{len(rows)} signals\n")
    for name, title, dept, url, signal_type in rows:
        print(f"{name:12} | {title}")
        print(f"{'':12} | {signal_type}" + (f", dept={dept}" if signal_type != "money_raised" else ""))
        print(f"{'':12} | {url}")
        print()
    cands = conn.execute("SELECT count(*) FROM signal_candidates").fetchone()[0]
    print(f"held back as candidates: {cands}")


if __name__ == "__main__":
    main()
