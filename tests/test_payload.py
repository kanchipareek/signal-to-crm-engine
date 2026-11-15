"""The webhook payload carries the company domain, so the CRM side can dedupe on it."""
from src.deliver import build_event


def test_event_includes_company_domain():
    row = (1, 1001, "UserGems", "usergems.com", "greenhouse", "usergems",
           "6123486004", "https://job-boards.greenhouse.io/usergems/jobs/6123486004",
           "VP of Sales", None, "2026-07-28T14:57:37-04:00")
    event = build_event(row)
    assert event["company_domain"] == "usergems.com"
    assert event["company_name"] == "UserGems"
    assert event["title"] == "VP of Sales"
