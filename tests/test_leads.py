"""Tests for the lead finder: website guessing, hire matching, sec name
matching, scoring, founder names from form d, the seller profile, and the
gemini client (with the network mocked out).

Run with:  python -m pytest tests/ -v
"""
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.leads as leads
import src.llm as llm
from src.funding import format_usd
from src.sec import parse_form_d
from src.seller import clean, load_or_build

PROFILE = clean(json.loads((ROOT / "config" / "sellers" / "nozzle-io.json").read_text(encoding="utf-8")))


# --- names and websites --------------------------------------------------------

@pytest.mark.parametrize("entity, core", [
    ("Deep Cogito Inc.", ["deep", "cogito"]),
    ("Alljoined, Inc.", ["alljoined"]),
    ("Engineered Arts Holding Company, Inc.", ["engineered", "arts"]),
    ("next robot inc", ["next", "robot"]),
])
def test_name_core(entity, core):
    assert leads.name_core(entity) == core


def test_guess_domains_order():
    got = leads.guess_domains("Kaliber Labs Inc.")
    assert got[:2] == ["kaliberlabs.com", "kaliberlabs.ai"]
    assert "kaliber.com" in got          # without the generic "labs"
    assert "kaliber-labs.com" in got


@pytest.mark.parametrize("entity, fund", [
    ("PI Harbor District Portfolio, LLC", True),
    ("Alterra IOS Venture III Co-Invest, LP", True),
    ("Twin Capital Income Fund, LLC", True),
    ("Deep Cogito Inc.", False),
    ("Obsidian Sensors, Inc.", False),
])
def test_looks_like_fund(entity, fund):
    assert leads.looks_like_fund(entity) is fund


def test_homepage_matches_real_site():
    page = "<title>Deep Cogito</title> Deep Cogito builds open models"
    assert leads.homepage_matches("deepcogito.com", "Deep Cogito Inc.", "deepcogito.com", page)


def test_homepage_rejects_parked_domain():
    page = "deepcogito.com - this domain is for sale! Buy this domain on Sedo"
    assert not leads.homepage_matches("deepcogito.com", "Deep Cogito Inc.", "deepcogito.com", page)


def test_homepage_rejects_redirect_elsewhere():
    page = "Deep Cogito is mentioned here"
    assert not leads.homepage_matches("deepcogito.com", "Deep Cogito Inc.", "someothersite.com", page)


def test_homepage_rejects_redirect_to_a_country_domain():
    # the real case: Alcar Inc. (Darien, CT) -> alcar.com redirects to a swedish alcar.se
    assert not leads.homepage_matches("alcar.com", "Alcar Inc.", "alcar.se", "Alcar - bildelar")


def test_homepage_rejects_page_without_the_name():
    assert not leads.homepage_matches("acme.ai", "Acme Inc.", "acme.ai", "<title>Welcome</title> hello")


# --- hires ---------------------------------------------------------------------

def postings(*titles):
    return [{"title": t, "source_url": f"https://x/{i}", "external_id": str(i)} for i, t in enumerate(titles)]


def test_match_hires_keeps_seo_roles_and_ranks_senior_first():
    got = leads.match_hires(postings("SEO Manager", "Head of SEO", "Backend Engineer"), PROFILE)
    assert [h["title"] for h in got] == ["Head of SEO", "SEO Manager"]
    assert got[0]["senior"] and not got[1]["senior"]


@pytest.mark.parametrize("title", [
    "Global Paid Search Marketing Manager",       # paid search is ads, not nozzle
    "Executive Assistant, Content Marketing",
    "Head of Content Design",
    "SEO Intern",
    "Senior Product Designer",
])
def test_match_hires_drops_neighbouring_jobs(title):
    assert leads.match_hires(postings(title), PROFILE) == []


# --- sec name matching ---------------------------------------------------------

def test_same_company_ignores_legal_suffix():
    assert leads.same_company("Vanta Inc.", "Vanta")
    assert not leads.same_company("Vantaca, LLC", "Vanta")


def test_sec_by_name_needs_exactly_one_match(monkeypatch):
    import src.sec as sec
    monkeypatch.setattr(sec, "lookup_entities", lambda n: [
        ("1934145", "Vanta Inc."), ("1111111", "Vanta Corp"), ("1669560", "Vantaca, LLC")])
    # two corporations both called Vanta -> can't tell which, so no guess
    assert leads.sec_by_name("Vanta") is None
    # an llc of the same name is treated as a fund/vehicle and ignored
    monkeypatch.setattr(sec, "lookup_entities", lambda n: [
        ("1934145", "Vanta Inc."), ("1953933", "Vanta LLC")])
    assert leads.sec_by_name("Vanta") == ("1934145", "Vanta Inc.")
    monkeypatch.setattr(sec, "lookup_entities", lambda n: [
        ("1934145", "Vanta Inc."), ("1669560", "Vantaca, LLC")])
    assert leads.sec_by_name("Vanta") == ("1934145", "Vanta Inc.")


def test_sec_by_name_skips_spvs(monkeypatch):
    import src.sec as sec
    monkeypatch.setattr(sec, "lookup_entities", lambda n: [("1", "Airtable SPV LLC")])
    assert leads.sec_by_name("Airtable SPV") is None


# --- founders from form d ------------------------------------------------------

FORM_D = b"""<?xml version="1.0"?>
<edgarSubmission>
  <submissionType>D</submissionType>
  <primaryIssuer>
    <entityName>Alljoined, Inc.</entityName>
    <issuerAddress><city>SAN FRANCISCO</city><stateOrCountry>CA</stateOrCountry></issuerAddress>
    <entityType>Corporation</entityType>
    <yearOfInc><withinFiveYears>true</withinFiveYears><value>2023</value></yearOfInc>
  </primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Jane</firstName><middleName>-</middleName><lastName>Doe</lastName></relatedPersonName>
      <relatedPersonRelationshipList><relationship>Executive Officer</relationship><relationship>Director</relationship></relatedPersonRelationshipList>
    </relatedPersonInfo>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Sam</firstName><lastName>Lee</lastName></relatedPersonName>
      <relatedPersonRelationshipList><relationship>Director</relationship></relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <offeringSalesAmounts><totalOfferingAmount>20000000</totalOfferingAmount><totalAmountSold>16180000</totalAmountSold></offeringSalesAmounts>
  </offeringData>
</edgarSubmission>"""


def test_form_d_people_and_company_fields():
    f = parse_form_d(FORM_D)
    assert f["people"] == [
        {"name": "Jane Doe", "roles": ["Executive Officer", "Director"], "note": None},
        {"name": "Sam Lee", "roles": ["Director"], "note": None},
    ]
    assert (f["city"], f["state"], f["entity_type"]) == ("SAN FRANCISCO", "CA", "Corporation")
    assert f["incorporated_within_5_years"] is True


# --- scoring -------------------------------------------------------------------

def test_raised_and_hiring_beats_either_alone():
    funding = {"amount": 16_180_000, "date": "2026-11-18", "url": "u"}
    senior = {"title": "Head of SEO", "senior": True}
    both = leads.score({"funding": funding, "hires": [senior]})
    raised = leads.score({"funding": funding, "hires": []})
    hiring = leads.score({"hires": [senior]})
    assert both > raised > hiring > 0


def test_hire_points_are_capped():
    many = [{"title": f"SEO {i}", "senior": True} for i in range(10)]
    assert leads.score({"hires": many}) == 4


def test_why_reads_like_a_sentence():
    lead = {"funding": {"amount": 16_180_000, "date": "2026-11-18"},
            "hires": [{"title": "Head of SEO"}]}
    reasons = leads.why(lead, PROFILE, format_usd)
    assert reasons[0].startswith("Raised $16.18M (SEC Form D, filed 2026-11-18).")
    assert reasons[1].startswith('Hiring "Head of SEO".')


# --- seller profile ------------------------------------------------------------

def test_clean_drops_made_up_industry_groups():
    p = clean({**PROFILE, "form_d_industry_groups": ["Other Technology", "SaaS"]})
    assert p["form_d_industry_groups"] == ["Other Technology"]


def test_clean_needs_the_key_fields():
    broken = {k: v for k, v in PROFILE.items() if k != "hire_keywords"}
    with pytest.raises(ValueError):
        clean(broken)


def test_saved_profile_is_used_without_calling_the_model(monkeypatch):
    import src.seller as seller
    monkeypatch.setattr(seller, "build_profile", lambda *a, **k: pytest.fail("should not build"))
    profile, built = load_or_build("nozzle.io")
    assert built is False
    assert profile["seller_name"] == "Nozzle"


# --- gemini client -------------------------------------------------------------

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_ask_json_parses_reply(monkeypatch):
    reply = {"candidates": [{"content": {"parts": [{"text": '```json\n{"a": 1}\n```'}]}}]}
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-goog-api-key")
        seen["body"] = json.loads(req.data)
        return FakeResp(json.dumps(reply).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm.ask_json("hi", model="gemini-test", api_key="k") == {"a": 1}
    assert seen["url"].endswith("/models/gemini-test:generateContent")
    assert seen["key"] == "k"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_busy_model_falls_back_to_flash_lite(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_WAIT", 0)
    tried = []
    ok = {"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]}

    def fake_urlopen(req, timeout=0):
        model = req.full_url.split("/models/")[1].split(":")[0]
        tried.append(model)
        if model == "gemini-big":
            raise llm.urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"high demand"))
        return FakeResp(json.dumps(ok).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm.ask_json("hi", model="gemini-big", api_key="k") == {"a": 1}
    assert tried == ["gemini-big", "gemini-big", llm.FALLBACK_MODEL]


def test_permanent_error_does_not_retry(monkeypatch):
    tried = []

    def fake_urlopen(req, timeout=0):
        tried.append(1)
        raise llm.urllib.error.HTTPError(req.full_url, 402, "no credits", {}, io.BytesIO(b"credits depleted"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMError, match="402"):
        llm.ask_json("hi", model="gemini-big", api_key="k")
    assert len(tried) == 1


def test_ask_json_without_key_says_what_to_do(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="by hand"):
        llm.ask_json("hi")


def test_ask_json_bad_reply_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=0: FakeResp(b'{"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}'))
    with pytest.raises(llm.LLMError, match="valid json"):
        llm.ask_json("hi", api_key="k")
