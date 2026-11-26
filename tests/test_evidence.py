"""Tests for the run-time evidence recheck: jobs have to still be up and still
list the role, and a lead with nothing left is dropped.

Run with:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evidence import PageCache, evidence_rows, page_lists, verify_lead

PAGES = {
    "https://jobs.ashbyhq.com/tomo/1": (200, "<title>Mortgage Loan Processor @ Tomo Mortgage</title>"),
    "https://jobs.ashbyhq.com/tomo/2": (404, ""),
    "https://acme.ai/careers": (200, "<h2>Senior Backend Engineer/ Architect</h2><h2>AI Engineer</h2>"),
}


def cache():
    calls = []

    def web(url):
        calls.append(url)
        status, page = PAGES.get(url, (404, ""))
        return status, url, page
    c = PageCache(web=lambda url: web(url))
    c.calls = calls
    return c


def job(title, url, kind="ops_pain"):
    return {"title": title, "source_url": url, "kind": kind, "senior": False}


def test_page_lists_ignores_case_and_punctuation():
    assert page_lists("Senior Backend Engineer / Architect", "<h2>senior backend engineer/ architect</h2>")
    assert not page_lists("Loan Processor", "<h2>Loan Officer</h2>")


def test_dead_or_changed_jobs_are_dropped_with_a_reason():
    lead = {"hires": [job("Mortgage Loan Processor", "https://jobs.ashbyhq.com/tomo/1"),
                      job("Underwriting Assistant", "https://jobs.ashbyhq.com/tomo/2"),
                      job("Claims Specialist", "https://acme.ai/careers")]}
    assert verify_lead(lead, cache())
    assert [h["title"] for h in lead["hires"]] == ["Mortgage Loan Processor"]
    assert lead["hires"][0]["checked_at"]
    assert [d["drop_reason"] for d in lead["dropped_jobs"]] == ["url returned 404", "role no longer listed on the page"]


def test_lead_with_no_evidence_left_is_dropped():
    lead = {"hires": [job("Underwriting Assistant", "https://jobs.ashbyhq.com/tomo/2")]}
    assert not verify_lead(lead, cache())


def test_funding_needs_a_date_and_a_url():
    lead = {"funding": {"amount": 1, "date": "", "url": "x"}, "hires": []}
    assert not verify_lead(lead, cache())
    lead = {"funding": {"amount": 1, "date": "2026-10-22", "url": "https://inc42.com/a"}, "hires": []}
    assert verify_lead(lead, cache())


def test_careers_page_is_fetched_once_for_many_roles():
    c = cache()
    lead = {"hires": [job("Senior Backend Engineer/ Architect", "https://acme.ai/careers", "team"),
                      job("AI Engineer", "https://acme.ai/careers", "team")]}
    verify_lead(lead, c)
    assert len(lead["hires"]) == 2
    assert c.calls == ["https://acme.ai/careers"]


def test_people_without_a_source_are_dropped():
    lead = {"funding": {"date": "2026-10-22", "url": "u"}, "hires": [],
            "people": [{"name": "Ajeet Singh", "title": "CEO", "source": "https://different.ai/company"},
                       {"name": "Someone", "title": "CEO", "source": ""}]}
    verify_lead(lead, cache())
    assert [p["name"] for p in lead["people"]] == ["Ajeet Singh"]


def test_evidence_rows_have_signal_date_and_url():
    lead = {"funding": {"date": "2026-10-22", "round": "seed", "source": "news",
                        "sources": ["https://inc42.com/a", "https://entrackr.com/b"]},
            "hires": [{**job("AI Engineer", "https://acme.ai/careers", "team"), "checked_at": "2026-11-25T10:00:00+00:00"}]}
    assert evidence_rows(lead) == [
        ("raise (seed, news)", "2026-10-22", "https://inc42.com/a"),
        ("raise (seed, news)", "2026-10-22", "https://entrackr.com/b"),
        ("team hire: AI Engineer", "open on 2026-11-25", "https://acme.ai/careers"),
    ]


def test_a_company_with_two_raises_is_one_lead_with_both_as_evidence():
    from scripts.find_leads import merge_same_company
    seed = {"amount": 16_000_000, "amount_text": "$16m", "round": "seed", "source": "news",
            "date": "2026-09-01", "sources": ["https://a.example/seed"]}
    a_round = {"amount": 38_000_000, "amount_text": "$38m", "round": "series a", "source": "news",
               "date": "2026-11-01", "sources": ["https://b.example/series-a"]}
    leads = [{"name": "Noah", "domain": "noah.com", "score": 12, "funding": seed, "hires": [], "why": ["seed"]},
             {"name": "Noah", "domain": "noah.com", "score": 13, "funding": a_round, "hires": [], "why": ["a"]},
             {"name": "Other", "domain": "other.com", "score": 5, "funding": seed, "hires": [], "why": []}]
    merged = merge_same_company(leads)
    assert [l["name"] for l in merged] == ["Noah", "Other"]
    noah = merged[0]
    assert noah["funding"] is a_round and noah["other_raises"] == [seed]
    urls = [u for _, _, u in evidence_rows(noah)]
    assert urls == ["https://b.example/series-a", "https://a.example/seed"]
    assert any("also raised $16m" in w for w in noah["why"])
