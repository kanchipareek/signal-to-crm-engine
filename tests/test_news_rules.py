"""Tests for the news parts that don't need gemini: money parsing (dollars,
rupees, euros), the rule-based headline extractor, and dedupe.

The headlines are real ones from oct-nov 2026 coverage.

Run with:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.news import dedupe, parse_amount, parse_round, rule_extract


@pytest.mark.parametrize("text, usd, original", [
    ("raises $15 Mn in Series A", 15_000_000, "$15 Mn"),
    ("raises $5.1m pre-seed", 5_100_000, "$5.1m"),
    ("raises $550K in pre-seed", 550_000, "$550K"),
    ("raises Rs 40 crore", 4_761_904, "Rs 40 crore"),
    ("bags ₹10 cr", 1_190_476, "₹10 cr"),
    ("secures INR 50 lakh", 59_523, "INR 50 lakh"),
    ("raises €2 million", 2_160_000, "€2 million"),
    ("raises 40 crore rupees", 4_761_904, "40 crore rupees"),
])
def test_parse_amount(text, usd, original):
    assert parse_amount(text) == (usd, original)


def test_parse_amount_none_when_not_stated():
    assert parse_amount("Great Hill invests in Aurenity") == (None, None)


@pytest.mark.parametrize("text, rnd", [
    ("$15 Mn In Series A Round", "series a"),
    ("₹6 crore pre-seed funding", "pre-seed"),
    ("Pre Seed round", "pre-seed"),
    ("Strategic Investment in Aurenity", "strategic"),
])
def test_parse_round(text, rnd):
    assert parse_round(text) == rnd


@pytest.mark.parametrize("headline, company, amount, rnd", [
    ("Firstwork Raises $15 Mn In Series A Round Led By Earlybird Venture Capital", "Firstwork", 15_000_000, "series a"),
    ("Y Combinator-Backed Firstwork Nets $15 Mn To Expand Footprint Across US And Europe", "Firstwork", 15_000_000, None),
    ("Shamrock AI raises $5 mn seed funding led by Endiya Partners", "Shamrock AI", 5_000_000, "seed"),
    ("India’s Humyn Labs raises $47M to feed US robotics companies the data they need", "Humyn Labs", 47_000_000, None),
    ("Ajeet Singh Launches AI Marketing Startup Different With $48 Million Seed Funding", "Different", 48_000_000, "seed"),
    ("Elio Mortgage launches with $5.1m in pre-seed funding", "Elio Mortgage", 5_100_000, "pre-seed"),
    ("Great Hill Partners makes investment in E&S underwriter Aurenity", "Aurenity", None, "strategic"),
    ("Alpine Specialty raises $3m to automate small contractor E&S underwriting", "Alpine Specialty", 3_000_000, None),
    ("Investment Fund Management Startup Perry AI Announces $2.2M Seed Round", "Perry AI", 2_200_000, "seed"),
    ("CurvetAI raises ₹6 crore pre-seed funding led by Merak Ventures", "CurvetAI", 714_285, "pre-seed"),
])
def test_rule_extract_real_headlines(headline, company, amount, rnd):
    r = rule_extract({"title": headline})
    assert (r["company"], r["amount"], r["round"]) == (company, amount, rnd)
    assert r["extracted_by"] == "rule-based"


def test_undisclosed_amount_is_kept_and_labelled():
    r = rule_extract({"title": "Great Hill Partners Announces Strategic Investment in Aurenity"})
    assert r["amount"] is None
    assert r["amount_text"] == "not disclosed"


def test_purpose_clause_becomes_the_description():
    r = rule_extract({"title": "Alpine Specialty raises $3m to automate small contractor E&S underwriting"})
    assert r["what_they_do"] == "automate small contractor E&S underwriting"


@pytest.mark.parametrize("headline", [
    "Shadowfax to acquire WeFast India assets, Borzo IP for Rs 23.3 Cr",
    "Seed Funding Explained: A Guide for Tech Startups",
    "Acme secures contract with US Navy",
    "Gateway EduConnect appoints Jitendra Singh Gusain as CBO",
])
def test_rule_extract_ignores_non_raises(headline):
    assert rule_extract({"title": headline}) is None


def raise_row(company, amount, rnd, url, what="", date="2026-10-20"):
    return {"company": company, "amount": amount, "amount_text": str(amount), "round": rnd,
            "what_they_do": what, "country": None, "url": url, "date": date, "headline": url}


def test_dedupe_merges_outlets_on_company_amount_round():
    rows = [raise_row("Firstwork", 15_000_000, "series a", "a", "applied ai for frontline workforce", "2026-10-22"),
            raise_row("Firstwork", 15_000_000, None, "b", "expand across us and europe", "2026-10-21"),
            raise_row("Perry AI", 2_200_000, "seed", "c"),
            raise_row("Perry AI", 2_160_000, "seed", "d")]          # €2m vs $2.2m: same raise
    out = dedupe(rows)
    assert len(out) == 2
    firstwork = next(r for r in out if r["company"] == "Firstwork")
    assert firstwork["sources"] == ["a", "b"]
    assert firstwork["date"] == "2026-10-21"                       # earliest report wins
    assert "frontline" in firstwork["what_they_do"] and "europe" in firstwork["what_they_do"]


def test_dedupe_keeps_two_different_rounds_apart():
    out = dedupe([raise_row("Acme", 2_000_000, "seed", "a"), raise_row("Acme", 20_000_000, "series a", "b")])
    assert len(out) == 2
