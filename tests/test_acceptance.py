"""Acceptance tests: the real lead finder, run on recorded real data.

The fixtures in tests/fixtures/acceptance/ were captured from live feeds, job
boards and company pages with scripts/capture_acceptance.py (see that file for
what's kept). These tests replay them, so they run offline and are repeatable.
Anything the pipeline asks for that wasn't recorded is a 404.

No gemini key here: every step is the rule-based one.

  rightfit.so (proof-of-work hiring, india) should surface at least 3 of
    different.ai, firstwork.com, shamrockus.ai, humynlabs.ai
  unsiloed.ai (document parsing) should surface at least 3 of
    elio.mortgage, trovy.com, tomo.com, aurenity.com, alpine-specialty.com

Run with:  python -m pytest tests/test_acceptance.py -v
"""
import gzip
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.find_leads as fl
import src.news as news
import src.sec as sec
from src.evidence import evidence_rows
from src.seller import load_or_build

FIXTURES = ROOT / "tests" / "fixtures" / "acceptance"


class Replay(io.BytesIO):
    def __init__(self, status, url, body):
        super().__init__(body.encode("utf-8"))
        self.status = status
        self._url = url
        self.headers = {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def replayer(responses):
    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        data = getattr(req, "data", None)
        key = ("POST " if data else "GET ") + url + (" " + data.decode("utf-8", "ignore") if data else "")
        r = responses.get(key)
        if r is None:
            raise urllib.error.HTTPError(url, 404, "not recorded", {}, io.BytesIO(b""))
        if r["status"] is None:
            raise urllib.error.URLError("recorded as a network failure")
        if r["status"] >= 400:
            raise urllib.error.HTTPError(url, r["status"], "recorded", {}, io.BytesIO(b""))
        return Replay(r["status"], r["url"], r["body"])
    return urlopen


def run_offline(seller, monkeypatch):
    path = FIXTURES / f"{seller.replace('.', '-')}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fx = json.load(fh)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", replayer(fx["responses"]))
    monkeypatch.setattr(fl, "search_headlines", lambda profile, days: fx["headlines"])  # the saved headlines only
    monkeypatch.setattr(news, "GOOGLE_GAP", 0)
    monkeypatch.setattr(sec, "PAUSE", 0)
    profile, _ = load_or_build(seller)
    cands = [c for c in fl.from_news(profile, fx["days"]) if c["name"] in fx["candidates"]] + fx["listed"]
    leads, rejected, _ = fl.process(cands, profile, fx["days"], workers=1)
    return fx, leads, rejected


def found(leads, needle):
    return next((l for l in leads if needle in l["name"].lower() or needle in (l.get("domain") or "")), None)


def assert_real_evidence(lead):
    rows = evidence_rows(lead)
    assert rows, f"{lead['name']} has no evidence"
    for kind, day, url in rows:
        assert kind and day and url.startswith("http"), (lead["name"], kind, day, url)
    for p in lead.get("people", []):
        assert p["source"].startswith("http"), p


# --- rightfit.so -----------------------------------------------------------------

@pytest.fixture(scope="module")
def rightfit():
    mp = pytest.MonkeyPatch()
    try:
        yield run_offline("rightfit.so", mp)
    finally:
        mp.undo()


def test_rightfit_surfaces_at_least_three_of_the_four(rightfit):
    _, leads, _ = rightfit
    hits = [n for n in ("different", "firstwork", "shamrock", "humyn") if found(leads, n)]
    assert len(hits) >= 3, f"only found {hits} in {[l['name'] for l in leads]}"


def test_rightfit_leads_carry_the_real_raise_and_roles(rightfit):
    _, leads, _ = rightfit
    checks = {"different": 48_000_000, "firstwork": 15_000_000, "shamrock": 5_000_000, "humyn": 47_000_000}
    with_roles = []
    for needle, amount in checks.items():
        lead = found(leads, needle)
        if not lead:
            continue
        assert lead["funding"]["amount"] == amount, (needle, lead["funding"])
        if lead["hires"]:
            with_roles.append(needle)
    # shamrock ai's site (shamrockus.ai) can't be found from its name or its raise
    # articles, so its roles can't be read - the other three come with real roles
    assert len(with_roles) >= 3, with_roles


def test_different_resolves_to_different_ai_not_different_com(rightfit):
    _, leads, _ = rightfit
    lead = found(leads, "different")
    assert lead and lead["domain"] == "different.ai"


def test_firstwork_roles_come_from_its_ashby_board(rightfit):
    _, leads, _ = rightfit
    lead = found(leads, "firstwork")
    if lead:
        assert lead["board"] == ("ashby", "firstwork")
        assert all(h["source_url"].startswith("https://jobs.ashbyhq.com/firstwork/") for h in lead["hires"])


def test_rightfit_every_lead_has_real_sources(rightfit):
    _, leads, _ = rightfit
    for lead in leads:
        assert_real_evidence(lead)


# --- unsiloed.ai -----------------------------------------------------------------

@pytest.fixture(scope="module")
def unsiloed():
    mp = pytest.MonkeyPatch()
    try:
        yield run_offline("unsiloed.ai", mp)
    finally:
        mp.undo()


def test_unsiloed_surfaces_at_least_three_of_the_five(unsiloed):
    _, leads, _ = unsiloed
    hits = [n for n in ("elio", "trovy", "tomo", "aurenity", "alpine") if found(leads, n)]
    assert len(hits) >= 3, f"only found {hits} in {[l['name'] for l in leads]}"


def test_unsiloed_news_alone_finds_three(unsiloed):
    # elio, aurenity and alpine specialty come from the funding news, not a list
    _, leads, _ = unsiloed
    from_news = [n for n in ("elio", "aurenity", "alpine") if (l := found(leads, n)) and l["found_via"].startswith("raised (in the news")]
    assert len(from_news) >= 3, from_news


def test_aurenity_is_kept_with_the_amount_not_disclosed(unsiloed):
    _, leads, _ = unsiloed
    lead = found(leads, "aurenity")
    assert lead and lead["funding"]["amount"] is None
    assert lead["funding"]["amount_text"] == "not disclosed"
    assert lead["funding"]["round"] == "strategic"


def test_lenders_hiring_loan_processors_are_ops_pain_leads(unsiloed):
    _, leads, _ = unsiloed
    for needle in ("tomo", "trovy"):
        lead = found(leads, needle)
        assert lead, f"{needle} missing"
        pains = [h for h in lead["hires"] if h["kind"] == "ops_pain"]
        assert any("loan processor" in h["title"].lower() for h in pains), [h["title"] for h in lead["hires"]]
        assert lead["fit"] == "yes" and lead["fit_by"] == "rule-based"


def test_unsiloed_every_lead_has_real_sources(unsiloed):
    _, leads, _ = unsiloed
    for lead in leads:
        assert_real_evidence(lead)
