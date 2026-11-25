"""Tests for the news funding source and the fit check, with gemini and the
network mocked out.

Run with:  python -m pytest tests/ -v
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.fit as fit
import src.leads as leads
import src.news as news
from src.funding import format_usd
from src.seller import clean

PROFILE = clean({
    "seller": "unsiloed.ai", "seller_name": "Unsiloed",
    "sells": "document parsing apis for llm apps",
    "buyers": "companies building ai products on top of documents: legal, finance, insurance",
    "competitors": ["llamaindex.ai", "reducto.ai"],
    "news_queries": ["legal ai", "insurance claims automation"],
    "hire_keywords": ["document processing"], "hire_reason": "Hiring for x.",
    "form_d_industry_groups": ["Other Technology"], "min_raise_usd": 1000000,
    "max_raise_usd": 30000000, "funding_reason": "money.",
    "example_buyers": [{"name": "LlamaIndex", "domain": "llamaindex.ai"},
                       {"name": "Perry AI", "domain": "perry.ai"}],
})

RSS = """<rss><channel>
<item><title>Perry AI raises $2.2M seed to give private capital a legal memory - Dealroom</title>
<link>https://news.example/1</link><pubDate>Wed, 18 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Seed Funding Explained: A Guide for Tech Startups - NetSuite</title>
<link>https://news.example/2</link><pubDate>Wed, 18 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Perry AI raises $2.2M seed to give private capital a legal memory - Dealroom</title>
<link>https://news.example/3</link><pubDate>Wed, 18 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


# --- seller profile -------------------------------------------------------------

def test_competitors_are_removed_from_example_buyers():
    assert [b["domain"] for b in PROFILE["example_buyers"]] == ["perry.ai"]


def test_perry_ai_guesses_perry_dot_ai_first():
    assert leads.guess_domains("Perry AI")[0] == "perry.ai"


# --- news ----------------------------------------------------------------------

class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_headlines_are_deduped_and_dated(monkeypatch):
    import datetime as real
    monkeypatch.setattr(news, "GOOGLE_GAP", 0)
    monkeypatch.setattr(news.urllib.request, "urlopen", lambda req, timeout=0: Resp(RSS.encode()))
    monkeypatch.setattr(news, "datetime", type("FixedNow", (), {
        "now": staticmethod(lambda tz=None: real.datetime(2026, 11, 24, tzinfo=tz))}))
    got = news.search_headlines(PROFILE, 14)
    titles = [h["title"] for h in got]
    assert titles.count("Perry AI raises $2.2M seed to give private capital a legal memory - Dealroom") == 1
    assert got[0]["date"] == "2026-11-18"


def test_buyer_queries_are_searched(monkeypatch):
    seen = []
    monkeypatch.setattr(news, "_rss", lambda q, days, edition="us": seen.append((q, edition)) or [])
    monkeypatch.setattr(news, "_feed", lambda name, cutoff="": [])
    news.search_headlines(PROFILE, 14)
    assert ("legal ai raises", "us") in seen
    assert ("legal ai raises", "in") in seen          # every query runs on the india edition too
    assert ("insurance claims automation raises", "in") in seen


def test_extract_raises_maps_gemini_reply_to_headlines(monkeypatch):
    headlines = [{"title": "Perry AI raises $2.2M seed", "link": "u1", "date": "2026-11-18"},
                 {"title": "Seed Funding Explained", "link": "u2", "date": "2026-11-18"}]
    reply = {"raises": [
        {"headline": 0, "company": "Perry AI", "amount_usd": 2200000, "round": "seed",
         "what_they_do": "AI legal software for private capital", "country": "London"},
        {"headline": 9, "company": "Ghost", "amount_usd": 1},       # bad index -> ignored
        {"headline": 1, "company": "", "amount_usd": None},         # no company -> ignored
    ]}
    monkeypatch.setattr(news, "ask_json", lambda prompt, model=None: reply)
    got = news.extract_raises(headlines, use_llm=True)
    assert len(got) == 1
    perry = got[0]
    assert (perry["company"], perry["amount"], perry["round"], perry["country"]) == ("Perry AI", 2200000, "seed", "London")
    assert perry["what_they_do"] == "AI legal software for private capital"
    assert perry["extracted_by"] == "gemini"
    assert perry["sources"] == ["u1"]


# --- fit check -----------------------------------------------------------------

def test_fit_check_labels_and_falls_back_for_unjudged(monkeypatch):
    monkeypatch.setattr(fit, "describe", lambda lead: lead["desc"])
    replies = {"leads": [{"id": 0, "fit": "yes", "reason": "legal ai on fund documents"},
                         {"id": 1, "fit": "no", "reason": "builds its own models, a peer"}]}
    seen = {}

    def fake_ask(prompt, model=None):
        seen["prompt"] = prompt
        return replies
    monkeypatch.setattr(fit, "ask_json", fake_ask)
    batch = [{"name": "Perry AI", "desc": "x"}, {"name": "Perplexity AI", "desc": "y"},
             {"name": "Mystery Co", "desc": "legal ai for law firms"}]
    fit.check_fit(batch, PROFILE, use_llm=True)
    assert [l["fit"] for l in batch] == ["yes", "no", "yes"]
    assert [l["fit_by"] for l in batch] == ["gemini", "gemini", "rule-based"]  # gemini skipped #2
    assert "llamaindex.ai" in seen["prompt"]       # competitors are passed to gemini


def test_one_broken_batch_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(fit, "describe", lambda lead: lead["desc"])
    monkeypatch.setattr(fit, "BATCH", 1)
    calls = []

    def flaky(prompt, model=None):
        calls.append(prompt)
        if "0. Broken Co" in prompt:
            raise fit.LLMError("model didn't return valid json")
        return {"leads": [{"id": 0, "fit": "no", "reason": "peer"}]}
    monkeypatch.setattr(fit, "ask_json", flaky)
    batch = [{"name": "Broken Co", "desc": "insurance claims automation"}, {"name": "Peer Co", "desc": "z"}]
    failed = fit.check_fit(batch, PROFILE, use_llm=True)
    assert failed == 1
    assert [l["fit"] for l in batch] == ["yes", "no"]
    assert [l["fit_by"] for l in batch] == ["rule-based", "gemini"]
    assert len(calls) == 3                               # bad batch retried once


def test_without_a_key_everything_is_rule_based(monkeypatch):
    monkeypatch.setattr(fit, "describe", lambda lead: lead["desc"])
    monkeypatch.setattr(fit, "ask_json", lambda *a, **k: pytest.fail("no llm without a key"))
    batch = [{"name": "A", "desc": "legal ai for funds"}, {"name": "B", "desc": "dog food delivery"},
             {"name": "C", "desc": "no description found"}, {"name": "D", "domain": "reducto.ai", "desc": "parsing"}]
    fit.check_fit(batch, PROFILE, use_llm=False)
    assert [l["fit"] for l in batch] == ["yes", "no", "maybe", "no"]
    assert all(l["fit_by"] == "rule-based" for l in batch)
    assert batch[3]["fit_reason"] == "a competitor"


def test_ops_pain_hire_makes_a_rule_based_yes():
    lead = {"name": "Tomo", "description": "home buying", "hires": [
        {"title": "Mortgage Loan Processor", "kind": "ops_pain"}]}
    fit_, reason = fit.rule_fit(lead, PROFILE)
    assert fit_ == "yes"
    assert "Mortgage Loan Processor" in reason


def test_descriptions_from_every_outlet_are_merged(monkeypatch):
    headlines = [{"title": "Investment Fund Management Startup Perry AI Announces $2.2M", "link": "a", "date": "d"},
                 {"title": "Perry AI raises $2.2M to give private capital a legal memory", "link": "b", "date": "d"}]
    reply = {"raises": [
        {"headline": 0, "company": "Perry AI", "amount_usd": None, "what_they_do": "investment fund management startup"},
        {"headline": 1, "company": "Perry AI", "amount_usd": 2200000, "what_they_do": "AI legal software for private capital"},
    ]}
    monkeypatch.setattr(news, "ask_json", lambda prompt, model=None: reply)
    [perry] = news.extract_raises(headlines, use_llm=True)
    assert perry["what_they_do"] == "investment fund management startup; AI legal software for private capital"
    assert perry["amount"] == 2200000


def test_clear_fit_with_one_signal_beats_unclear_fit_with_two():
    perry = {"funding": {"amount": 2_200_000}, "hires": [], "fit": "yes"}
    unclear = {"funding": {"amount": 30_000_000}, "hires": [{"title": "x", "senior": True}], "fit": "maybe"}
    assert leads.score(perry) > leads.score(unclear)


def test_why_for_a_news_raise():
    lead = {"fit": "yes", "fit_reason": "legal ai",
            "funding": {"amount": 2_200_000, "date": "2026-11-18", "source": "news",
                        "headline": "Perry AI raises $2.2M seed"}, "hires": []}
    reasons = leads.why(lead, PROFILE, format_usd)
    assert reasons[0] == "Fit (yes): legal ai"
    assert reasons[1].startswith('Raised $2.2M (in the news, 2026-11-18: "Perry AI raises $2.2M seed").')


def test_why_when_news_has_no_amount():
    lead = {"funding": {"amount": None, "date": "2026-11-18", "source": "news", "headline": "h"}, "hires": []}
    assert leads.why(lead, PROFILE, format_usd)[0].startswith("Raised money (amount not disclosed)")
