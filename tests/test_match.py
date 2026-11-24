"""Tests for the match engine.

I edit the rule often, so this is the set of cases I re-run after every change.
If one breaks, I either changed something on purpose or I made a mistake -
either way I want to catch it here and not in the detector.

Runs offline: no network, no database, just the classification logic.

Run with:  python -m pytest tests/ -v
"""
import pytest

from src.match import classify, load_rule

RULE = load_rule()

# (title, department, expected label)
CASES = [
    # --- the core rule -------------------------------------------------------
    ("VP Marketing", "Marketing", "hit"),
    ("Head of Growth", "Growth", "hit"),
    ("Director of Growth", "Growth", "hit"),                 # Growth-only director rule
    ("Director of Marketing", "Marketing", "near_miss"),     # director, non-Growth
    ("CMO", "Marketing", "hit"),                             # abbreviation is senior
    ("VP Sales", "Sales", "hit"),
    ("Director of Sales", "Sales", "near_miss"),
    ("Marketing Manager", "Marketing", "near_miss"),         # right function, below seniority
    ("Account Executive", "Sales", "near_miss"),
    ("Growth Analyst", "Growth", "near_miss"),

    # --- ambiguity and conflict route to review ------------------------------
    ("Director of Growth Engineering", "Engineering", "review"),
    ("Director of Growth Engineering", "Growth", "review"),
    ("Director of Growth Engineering", None, "review"),
    ("Head of Growth Engineering", "Engineering", "review"),
    ("Head of Marketing Operations", "Marketing", "review"),
    ("Director of Marketing Operations", "Marketing", "review"),
    ("VP Marketing", "Engineering", "review"),               # conflict, not missing
    ("Chief of Staff, Sales", "Sales", "review"),            # "chief" here isn't a sales leader
    ("Chief of Staff to the CRO", None, "review"),
    ("Chief Revenue Officer", None, "hit"),                  # real chief titles still fire

    # --- clearly not a signal ------------------------------------------------
    ("VP of Engineering", "Engineering", "not_a_hit"),
    ("Data Scientist", "Engineering", "not_a_hit"),

    # --- a clear title needs no department -----------------------------------
    ("Head of GTM", None, "hit"),

    # --- normalisation / order-independence ----------------------------------
    ("Head of Growth", "Go-To-Market", "hit"),
    ("Head of Growth", "Go To Market", "hit"),
    ("Head of Growth", "go_to_market", "hit"),
    ("VP Marketing", "GTM", "hit"),
    ("VP, Marketing", "Marketing", "hit"),                   # punctuation in title
    ("Director of Growth", "Growth Marketing", "hit"),
    ("Director of Marketing", "Growth Marketing", "near_miss"),
]


@pytest.mark.parametrize("title, department, expected", CASES)
def test_classify(title, department, expected):
    assert classify(title, department, RULE) == expected


def test_department_spelling_is_equivalent():
    # Go-To-Market, Go To Market and go_to_market should all give the same answer.
    variants = ["Go-To-Market", "Go To Market", "go_to_market", "GTM"]
    results = {classify("Head of Growth", v, RULE) for v in variants}
    assert results == {"hit"}


def test_department_order_does_not_matter():
    # "Growth Marketing" hits two functions, so I check it doesn't depend on order.
    assert classify("Director of Growth", "Growth Marketing", RULE) == "hit"
    assert classify("Director of Marketing", "Growth Marketing", RULE) == "near_miss"


def test_short_abbreviation_does_not_fire_inside_a_word():
    # "cro" used to match inside "Crowd", which was a bug.
    assert classify("Crowd Marketing Lead", "Marketing", RULE) != "hit"
