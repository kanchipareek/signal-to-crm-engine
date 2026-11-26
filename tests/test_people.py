"""Tests for finding founders / ceo / coo outside sec filings. The rule is
'never guess': a name only counts with a title right next to it.

Run with:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.people as people

ARTICLE = """<article>
<p>The round was led by Accel, with participation from Peak XV Partners.</p>
<p>Ajeet Singh, founder and CEO of Different, said the money will go into hiring.</p>
<p>Co-founder and CTO Abhinav Yajurvedi added that the team is in Bengaluru.</p>
<p>Oren Michaely is the CEO of a different company here.</p>
<p>Series A led by Endiya Partners. Nazara Technologies as an investor.</p>
</article>"""

TEAM_PAGE = """<section><div><h3>Sarat Addanki</h3><p>Co-founder &amp; CEO</p></div>
<div><h3>Mick Gallagher</h3><p>Co-founder</p></div>
<div><h3>Our Team</h3><p>Founder</p></div>
<div><h3>Jane Smith</h3><p>Head of Design</p></div></section>"""


def names(found):
    return [(p["name"], p["title"]) for p in found]


def test_names_next_to_a_title_in_an_article():
    got = names(people.people_in_text(ARTICLE, "https://inc42.com/x"))
    assert ("Ajeet Singh", "founder and CEO") in got
    assert ("Abhinav Yajurvedi", "co-founder and CTO") in got


def test_no_guessing_from_loose_text():
    got = [n for n, _ in names(people.people_in_text(ARTICLE, "u"))]
    assert not any(n.endswith((" is", " as")) for n in got)       # "Oren Michaely is" isn't a name
    assert "Peak XV Partners" not in got and "Endiya Partners" not in got
    assert "Nazara Technologies" not in got


def test_team_cards_name_then_title():
    got = names(people.people_in_text(TEAM_PAGE, "https://acme.ai/about"))
    assert got == [("Sarat Addanki", "co-founder & CEO"), ("Mick Gallagher", "co-founder")]   # not Jane (no founder title)


def test_every_person_keeps_their_source():
    [first, *_] = people.people_in_text(TEAM_PAGE, "https://acme.ai/about")
    assert first["source"] == "https://acme.ai/about"


def test_find_people_skips_google_redirect_links_and_reads_company_pages(monkeypatch):
    asked = []

    def fake_web(url, data=None, headers=None, limit=0):
        asked.append(url)
        if url == "https://acme.ai/about":
            return 200, url, TEAM_PAGE
        return 404, url, ""
    monkeypatch.setattr(people, "_web", fake_web)
    got = people.find_people("acme.ai", ["https://news.google.com/rss/articles/abc", "https://inc42.com/a"])
    assert "https://news.google.com/rss/articles/abc" not in asked
    assert asked[0] == "https://inc42.com/a"
    assert [p["name"] for p in got] == ["Sarat Addanki", "Mick Gallagher"]


def test_title_words_are_not_names():
    # seen on a live run: "Before Conveo, co-founder and CTO", "Global Sales president"
    text = ("<p>Before Conveo, co-founder and CTO Maarten Claes built search tools.</p>"
            "<p>Thijn Lamers, Global Sales president, said so. Managing Partner founder of the fund.</p>"
            "<p>Senior Partner managing director Jane Roe. Former Vice president.</p>")
    got = [n for n, _ in names(people.people_in_text(text, "u"))]
    for bad in ("Before Conveo", "Global Sales", "Managing Partner", "Senior Partner", "Former Vice"):
        assert bad not in got, got
    assert "Maarten Claes" in got


def test_verbs_joiners_and_company_words_are_not_names():
    # also from live runs: "CEO Eric Glyman explains", "founder of Clinics On Cloud"
    for bad in ("Eric Glyman Explains", "Clinics On Cloud", "Fractal Analytics", "With Bernardo Seixas",
                "Post Payments Bank"):
        assert not people._good_name(bad), bad
    for good in ("Eric Glyman", "Bernardo Seixas", "Sarah Iglesias Hiller", "Noah Spaulding"):
        assert people._good_name(good), good
