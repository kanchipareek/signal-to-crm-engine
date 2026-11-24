"""Tests for the money-raised signal: Form D parsing, the funding rule, storage
idempotency, and the webhook payload.

Run with:  python -m pytest tests/ -v
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.funding import classify, format_usd, load_rule, signal_title
from src.sec import parse_form_d, filing_url
from src.deliver import build_event
from scripts.run_funding_detector import store

RULE = load_rule()
TODAY = date(2026, 11, 24)


def form_d_xml(sold="25000000", offering="30000000", amendment="false",
               industry="Other Technology", combination="false", sub_type="D"):
    """A trimmed Form D primary_doc.xml with the fields the parser reads."""
    return f"""<?xml version="1.0"?>
<edgarSubmission>
  <submissionType>{sub_type}</submissionType>
  <primaryIssuer><cik>0001934145</cik><entityName>Vanta Inc.</entityName></primaryIssuer>
  <offeringData>
    <industryGroup><industryGroupType>{industry}</industryGroupType></industryGroup>
    <typeOfFiling>
      <newOrAmendment><isAmendment>{amendment}</isAmendment></newOrAmendment>
      <dateOfFirstSale><value>2026-10-01</value></dateOfFirstSale>
    </typeOfFiling>
    <businessCombinationTransaction>
      <isBusinessCombinationTransaction>{combination}</isBusinessCombinationTransaction>
    </businessCombinationTransaction>
    <offeringSalesAmounts>
      <totalOfferingAmount>{offering}</totalOfferingAmount>
      <totalAmountSold>{sold}</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>""".encode()


def filing(filing_date="2026-10-10", **kw):
    return {**parse_form_d(form_d_xml(**kw)), "filing_date": filing_date, "accession": "0001934145-26-000001"}


# --- parsing -----------------------------------------------------------------

def test_parse_reads_amounts_and_flags():
    f = parse_form_d(form_d_xml())
    assert f["entity_name"] == "Vanta Inc."
    assert f["total_sold"] == 25_000_000
    assert f["total_offering"] == 30_000_000
    assert f["is_amendment"] is False
    assert f["business_combination"] is False
    assert f["date_of_first_sale"] == "2026-10-01"


def test_parse_indefinite_offering_is_none():
    assert parse_form_d(form_d_xml(offering="Indefinite"))["total_offering"] is None


def test_filing_url_strips_dashes_and_padding():
    assert filing_url("0001934145", "0001934145-26-000001") == \
        "https://www.sec.gov/Archives/edgar/data/1934145/000193414526000001/"


# --- the rule ----------------------------------------------------------------

@pytest.mark.parametrize("kwargs, label", [
    ({}, "hit"),                                                      # $25M sold
    ({"sold": "5000000"}, "hit"),                                     # exactly the bar
    ({"sold": "4999999"}, "near_miss"),                               # just under
    ({"sold": "750000", "offering": "1000000"}, "near_miss"),         # small raise
    ({"sold": "0", "offering": "40000000"}, "review"),                # announced, nothing sold yet
    ({"sold": "0", "offering": "Indefinite"}, "not_a_hit"),           # nothing at all
    ({"sold": "0", "offering": "2000000"}, "not_a_hit"),              # small and unsold
    ({"amendment": "true"}, "not_a_hit"),                             # D/A
    ({"sub_type": "D/A"}, "not_a_hit"),
    ({"industry": "Pooled Investment Fund"}, "not_a_hit"),            # SPV / fund
    ({"combination": "true"}, "review"),                              # merger money, not clearly a raise
])
def test_classify(kwargs, label):
    assert classify(filing(**kwargs), RULE, today=TODAY)[0] == label


def test_restructuring_with_nothing_sold_is_not_a_hit():
    # the real shape of Formagrid's (Airtable's) July 2026 filing
    f = filing(sold="0", offering="Indefinite", combination="true")
    assert classify(f, RULE, today=TODAY)[0] == "not_a_hit"


def test_old_filing_is_outside_the_window():
    label, reason = classify(filing(filing_date="2025-01-15"), RULE, today=TODAY)
    assert label == "not_a_hit"
    assert "window" in reason


def test_threshold_comes_from_config():
    rule = {**RULE, "min_amount_usd": 50_000_000}
    assert classify(filing(), rule, today=TODAY)[0] == "near_miss"


@pytest.mark.parametrize("amount, text", [
    (25_000_000, "$25M"), (7_500_000, "$7.5M"), (400_000, "$400K"), (1_250_000_000, "$1.25B"),
])
def test_format_usd(amount, text):
    assert format_usd(amount) == text


def test_signal_title():
    assert signal_title(filing()) == "Raised $25M (SEC Form D)"


# --- storage -----------------------------------------------------------------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, domain TEXT);"
                    "INSERT INTO companies VALUES (141,'Vanta','vanta.com');")
    for name in ("01_signals.sql", "02_signal_candidates.sql"):
        c.executescript((ROOT / "sql" / name).read_text())
    yield c
    c.close()


def test_hit_is_stored_once(conn):
    f = filing()
    first = store(conn, RULE, 141, "1934145", f, "hit", "sold $25M", "2026-11-24")
    again = store(conn, RULE, 141, "1934145", f, "hit", "sold $25M", "2026-11-24")
    assert (first, again) == ("new_hit", "unchanged")
    row = conn.execute("SELECT signal_type, source, title, amount_usd, delivery_status FROM signals").fetchall()
    assert row == [("money_raised", "sec_form_d", "Raised $25M (SEC Form D)", 25_000_000, "pending")]


def test_second_filing_same_day_is_linked_not_alerted(conn):
    store(conn, RULE, 141, "1934145", filing(), "hit", "sold $25M", "2026-11-24")
    second = {**filing(sold="9000000"), "accession": "0001934145-26-000002"}
    assert store(conn, RULE, 141, "1934145", second, "hit", "sold $9M", "2026-11-24") == "same_day"
    rows = conn.execute("SELECT id, delivery_status, repost_of FROM signals ORDER BY id").fetchall()
    assert rows == [(1, "pending", None), (2, "skipped", 1)]


def test_review_goes_to_candidates_not_signals(conn):
    f = filing(sold="0", offering="40000000")
    assert store(conn, RULE, 141, "1934145", f, "review", "offering filed", "2026-11-24") == "new_cand"
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0
    assert conn.execute("SELECT label FROM signal_candidates").fetchone()[0] == "review"


def test_not_a_hit_stores_nothing(conn):
    assert store(conn, RULE, 141, "1934145", filing(), "not_a_hit", "amendment", "2026-11-24") is None
    assert conn.execute("SELECT count(*) FROM signals").fetchone()[0] == 0


# --- payload -----------------------------------------------------------------

def test_funding_event_payload():
    row = (7, 141, "Vanta", "vanta.com", "sec_form_d", "1934145", "0001934145-26-000001",
           "https://www.sec.gov/Archives/edgar/data/1934145/000193414526000001/",
           "Raised $25M (SEC Form D)", None, "2026-10-10", "money_raised", 25_000_000)
    event = build_event(row)
    assert event["event"] == "money_raised"
    assert event["amount_usd"] == 25_000_000
    assert event["company_domain"] == "vanta.com"
