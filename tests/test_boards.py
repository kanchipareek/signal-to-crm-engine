"""Tests for the lever, workable and careers-page boards, and the checks that a
board really belongs to the company's domain. Network is mocked out.

Run with:  python -m pytest tests/ -v
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.find_boards as fb
import src.fetch as fetch

SHAPE = {"external_id", "title", "source_url", "department", "first_published", "updated_at", "company_name"}


def fake_web(routes):
    def _web(url, data=None, headers=None, limit=0):
        for prefix, (status, body) in routes.items():
            if url.startswith(prefix):
                return status, url, body
        return 404, url, ""
    return _web


def test_lever_postings_shape(monkeypatch):
    body = json.dumps([{"id": "abc", "text": "Loan Processor", "hostedUrl": "https://jobs.lever.co/acme/abc",
                        "createdAt": 1786469891368, "categories": {"team": "Ops", "location": "Austin, TX"}}])
    monkeypatch.setattr(fetch, "_web", fake_web({"https://api.lever.co/v0/postings/acme": (200, body)}))
    [p] = fetch.fetch_lever_board("acme")
    assert SHAPE <= set(p)
    assert (p["title"], p["source_url"], p["department"]) == ("Loan Processor", "https://jobs.lever.co/acme/abc", "Ops")
    assert p["first_published"].startswith("2026-")


def test_workable_postings_shape_and_paging(monkeypatch):
    pages = iter([
        {"results": [{"shortcode": "A1", "title": "Claims Specialist", "location": {"city": "Pune", "country": "India"}}],
         "nextPage": "p2"},
        {"results": [{"shortcode": "B2", "title": "Underwriting Assistant", "location": {}}]},
    ])
    monkeypatch.setattr(fetch, "_web", lambda url, data=None, headers=None, limit=0: (200, url, json.dumps(next(pages))))
    got = fetch.fetch_workable_board("acme")
    assert [p["title"] for p in got] == ["Claims Specialist", "Underwriting Assistant"]
    assert got[0]["source_url"] == "https://apply.workable.com/acme/j/A1/"
    assert got[0]["location"] == "Pune, India"
    assert SHAPE <= set(got[0])


CAREERS = """<html><head><style>.x{}</style></head><body>
<nav><a href="/about">About</a><a href="/careers">Careers</a><a href="/blog">Field notes on B2B marketing</a></nav>
<h2>Open roles</h2>
<div class="role"><h3>Applied AI Engineer</h3><span>Bengaluru</span></div>
<div class="role"><h3>Senior Backend Engineer/ Architect</h3><p>Shillong, Meghalaya, India</p></div>
<div><span class="openPositions__jobTitle">Data Analyst Bengaluru · On-site · Full-time</span></div>
<h3>Head of Supply Chain</h3>
<li><a href="/jobs/ux">UX Designer</a></li>
<h4>Product</h4><h4>Sales</h4>
<p>Stripe recruiters reach out through official channels</p>
<p>The Stripe Young Scientist & Technology Exhibition.</p>
<a href="/apply">View Role</a>
</body></html>"""


def test_careers_page_pulls_job_titles_only():
    titles = [p["title"] for p in fetch.parse_careers_page(CAREERS, "https://acme.ai/careers")]
    assert titles == ["Applied AI Engineer", "Senior Backend Engineer/ Architect", "Data Analyst",
                      "Head of Supply Chain", "UX Designer"]


def test_careers_postings_have_the_page_as_source():
    [p, *_] = fetch.parse_careers_page(CAREERS, "https://acme.ai/careers")
    assert p["source_url"] == "https://acme.ai/careers"
    assert SHAPE <= set(p)


@pytest.mark.parametrize("title, ok", [
    ("Senior Backend Engineer/ Architect", True),
    ("Head of Supply Chain", True),
    ("Engineer, Backend (Remote)", True),
    ("Mortgage Loan Processor", True),
    ("Stripe recruiters reach out through official channels", False),
    ("The Stripe Young Scientist & Technology Exhibition.", False),
    ("Product", False),
])
def test_looks_like_job_title(title, ok):
    assert fetch.looks_like_job_title(title) is ok


def test_careers_page_picks_the_path_with_roles(monkeypatch):
    # shamrockus.ai: /careers is a culture page, the roles are on /jobs
    monkeypatch.setattr(fetch, "_web", fake_web({
        "https://acme.ai/careers": (200, "<h2>Join our mission</h2>"),
        "https://acme.ai/jobs": (200, "<h2>AI Engineer</h2><h2>Senior Product Manager</h2>"),
    }))
    url, _ = fetch.find_careers_page("acme.ai")
    assert url == "https://acme.ai/jobs"


def test_careers_page_that_redirects_off_site_is_ignored(monkeypatch):
    monkeypatch.setattr(fetch, "_web", lambda url, data=None, headers=None, limit=0:
                        (200, "https://jobs.someoneelse.com/x", "<h2>AI Engineer</h2>"))
    assert fetch.find_careers_page("acme.ai") == (None, "")


# --- does the board belong to this domain? -------------------------------------

def test_lever_board_must_link_back_to_the_domain(monkeypatch):
    import src.fetch
    monkeypatch.setattr(src.fetch, "_web", fake_web({
        "https://api.lever.co/v0/postings/acme": (200, '[{"id": "1"}]'),
        "https://jobs.lever.co/acme": (200, '<a class="main-header-logo" href="https://www.acme.com/">'),
    }))
    assert fb.lever_token_ok("acme", "acme.com")
    assert not fb.lever_token_ok("acme", "acme.io")


def test_workable_board_must_list_the_domain(monkeypatch):
    import src.fetch
    monkeypatch.setattr(src.fetch, "_web", fake_web({
        "https://apply.workable.com/api/v1/accounts/acme": (200, '{"url": "https://acme.com/"}'),
        "https://apply.workable.com/api/v3/accounts/acme/jobs": (200, '{"results": [{"shortcode": "A", "title": "x"}]}'),
    }))
    assert fb.workable_token_ok("acme", "acme.com")
    assert not fb.workable_token_ok("acme", "acme.co")


def test_boards_linked_from_the_companys_own_site(monkeypatch):
    import src.fetch
    monkeypatch.setattr(src.fetch, "_web", fake_web({
        "https://acme.com/careers": (200, '<a href="https://jobs.ashbyhq.com/acme-inc/123">x</a>'
                                          '<a href="https://jobs.lever.co/acme">y</a>'),
    }))
    assert fb.boards_linked_from_site("acme.com") == [("ashby", "acme-inc"), ("lever", "acme")]
