"""Tests for the ashby job board: parsing, the source dispatcher, and the board
finder's domain check.

Run with:  python -m pytest tests/ -v
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.fetch as fetch
import scripts.find_boards as find_boards

ASHBY_RESPONSE = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "34413f8d-26bf-4bbc-8ade-eb309a0e2245",
            "title": " VP of Marketing ",
            "department": "Go To Market",
            "team": "Marketing",
            "publishedAt": "2026-11-01T17:12:35.753+00:00",
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
        },
        {
            "id": "hidden-1",
            "title": "Head of Sales",
            "department": "Sales",
            "isListed": False,
            "jobUrl": "https://jobs.ashbyhq.com/ramp/hidden-1",
        },
        {
            "id": "no-dept",
            "title": "Account Executive",
            "jobUrl": "https://jobs.ashbyhq.com/ramp/no-dept",
        },
    ],
}


@pytest.fixture
def ashby(monkeypatch):
    calls = []

    def fake_get(url, attempts=3):
        calls.append(url)
        return json.dumps(ASHBY_RESPONSE).encode()
    monkeypatch.setattr(fetch, "_get", fake_get)
    return calls


def test_ashby_posting_shape_matches_greenhouse(ashby):
    postings = fetch.fetch_ashby_board("ramp")
    first = postings[0]
    assert set(first) == {"external_id", "title", "source_url", "department",
                          "first_published", "updated_at", "company_name"}
    assert first["external_id"] == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first["title"] == "VP of Marketing"
    assert first["department"] == "Go To Market Marketing"
    assert first["first_published"] == "2026-11-01T17:12:35.753+00:00"
    assert first["source_url"].startswith("https://jobs.ashbyhq.com/ramp/")
    assert ashby == ["https://api.ashbyhq.com/posting-api/job-board/ramp"]


def test_unlisted_ashby_jobs_are_skipped(ashby):
    ids = [p["external_id"] for p in fetch.fetch_ashby_board("ramp")]
    assert "hidden-1" not in ids
    assert len(ids) == 2


def test_same_department_and_team_said_once():
    assert fetch._ashby_department({"department": "Sales", "team": "sales"}) == "Sales"


def test_missing_department_is_none(ashby):
    postings = fetch.fetch_ashby_board("ramp")
    assert postings[1]["department"] is None


def test_dispatcher_picks_the_right_fetcher(monkeypatch):
    monkeypatch.setitem(fetch.FETCHERS, "greenhouse", lambda t: ["gh", t])
    monkeypatch.setitem(fetch.FETCHERS, "ashby", lambda t: ["ashby", t])
    assert fetch.fetch_postings("greenhouse", "x") == ["gh", "x"]
    assert fetch.fetch_postings("ashby", "y") == ["ashby", "y"]


def test_unknown_source_fails_loudly():
    with pytest.raises(fetch.FetchError):
        fetch.fetch_postings("lever", "x")


# --- board finder ------------------------------------------------------------

@pytest.mark.parametrize("url, bare", [
    ("https://www.Airwallex.com/", "airwallex.com"),
    ("http://ramp.com", "ramp.com"),
    ("circle.so/about", "circle.so"),
    (None, ""),
])
def test_bare_domain(url, bare):
    assert find_boards.bare_domain(url) == bare


def _fake_board(monkeypatch, jobs, site):
    body = json.dumps({"jobs": jobs}).encode()
    monkeypatch.setattr(find_boards, "fetch", lambda url, limit=0: (200, body))
    monkeypatch.setattr(find_boards, "ashby_site", lambda token: site)


def test_ashby_board_accepted_when_site_matches(monkeypatch):
    _fake_board(monkeypatch, [{"id": "1"}], "https://ramp.com")
    assert find_boards.ashby_token_ok("ramp", "ramp.com")


def test_ashby_board_rejected_when_site_is_another_company(monkeypatch):
    # the real case: ashby's "circle" board is circle.so, not circle.com
    _fake_board(monkeypatch, [{"id": "1"}], "https://circle.so/")
    assert not find_boards.ashby_token_ok("circle", "circle.com")


def test_ashby_board_rejected_when_empty(monkeypatch):
    _fake_board(monkeypatch, [], "https://www.deel.com/")
    assert not find_boards.ashby_token_ok("deel", "deel.com")


def test_ashby_board_rejected_when_site_unknown(monkeypatch):
    _fake_board(monkeypatch, [{"id": "1"}], None)
    assert not find_boards.ashby_token_ok("everbridge", "everbridge.com")
