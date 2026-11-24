"""Suggests SEC CIKs for the companies in config/boards.csv. It never writes them.

Why it only suggests: a name search on EDGAR is a trap. "Airtable" returns
investor SPVs ("Caffeinated Capital Airtable SPV, LLC"), "ramp" returns an
unrelated RAMP CORP, and many companies file under a legal name nobody uses
(Airtable files as Formagrid). A wrong CIK would fire a false funding signal,
so a human confirms each one and copies it into config/sec_issuers.csv.

For each candidate it prints the EDGAR name, the business address, and how many
Form D filings it has, which is usually enough to tell the real company apart.

Usage:  python scripts/find_sec_ciks.py              # every company in boards.csv
        python scripts/find_sec_ciks.py Vanta Ramp   # just these names
"""
import csv
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fetch import FetchError
from src.sec import SUBMISSIONS, _get, FORM_D_TYPES

TYPEAHEAD = "https://efts.sec.gov/LATEST/search-index?keysTyped={q}"
FUND_WORDS = ("spv", "fund", "capital", "ventures", "partners", " lp", "holdings llc")


def candidates(name):
    data = json.loads(_get(TYPEAHEAD.format(q=urllib.parse.quote(name))))
    for hit in data.get("hits", {}).get("hits", [])[:6]:
        yield int(hit["_id"]), hit["_source"].get("entity", "")


def describe(cik):
    data = json.loads(_get(SUBMISSIONS.format(cik=cik)))
    addr = (data.get("addresses") or {}).get("business") or {}
    forms = data.get("filings", {}).get("recent", {}).get("form", [])
    form_d = sum(f in FORM_D_TYPES for f in forms)
    return f"{addr.get('city') or '?'}, {addr.get('stateOrCountry') or '?'}", form_d


def main():
    names = sys.argv[1:]
    if not names:
        with (ROOT / "config" / "boards.csv").open(encoding="utf-8") as fh:
            names = [r["name"] for r in csv.DictReader(fh)]
    for name in names:
        print(f"\n{name}")
        try:
            found = list(candidates(name))
        except FetchError as exc:
            print(f"  ! {exc}")
            continue
        if not found:
            print("  (no EDGAR entity - likely not a US filer)")
        for cik, entity in found:
            try:
                where, form_d = describe(cik)
            except FetchError:
                where, form_d = "?", 0
            flag = "  <- looks like a fund/SPV" if any(w in f" {entity.lower()}" for w in FUND_WORDS) else ""
            print(f"  cik {cik:<10} {entity[:45]:45} {where:22} form D: {form_d}{flag}")


if __name__ == "__main__":
    main()
