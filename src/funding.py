"""Funding rule: classify a parsed Form D as a money-raised signal.

The thresholds live in config/funding_rule.json (data); this is the logic.

Decision order (precision-first: anything that isn't clearly a raise is held
back, never a silent hit):
  1. Amendment (D/A)                         -> not_a_hit (the original filing carried the news)
  2. Pooled investment fund (SPV, fund)       -> not_a_hit (money into a fund, not the company)
  3. Filed before the lookback window          -> not_a_hit (old news)
  4. Nothing sold and no real offering amount  -> not_a_hit (e.g. an internal restructuring)
  5. Business combination (merger/restructure) -> review
  6. Amount sold >= min_amount_usd             -> hit
  7. Amount sold > 0 but below the bar         -> near_miss
  8. Nothing sold yet, offering >= the bar      -> review (round announced, money not in yet)
  9. Otherwise                                  -> not_a_hit
"""
import json
from datetime import date, timedelta
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "funding_rule.json"

HIT, NEAR_MISS, REVIEW, NOT_A_HIT = "hit", "near_miss", "review", "not_a_hit"


def load_rule(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def format_usd(amount):
    """25000000 -> '$25M', 7500000 -> '$7.5M', 400000 -> '$400K'."""
    if amount >= 1_000_000_000:
        value, unit = amount / 1_000_000_000, "B"
    elif amount >= 1_000_000:
        value, unit = amount / 1_000_000, "M"
    else:
        value, unit = amount / 1_000, "K"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"${text}{unit}"


def in_window(filing_date, rule, today=None):
    today = today or date.today()
    cutoff = today - timedelta(days=rule["lookback_days"])
    return date.fromisoformat(filing_date[:10]) >= cutoff


def classify(filing, rule, today=None):
    """Return (label, reason). `filing` is parse_form_d() output plus 'filing_date'."""
    minimum = rule["min_amount_usd"]
    sold = filing.get("total_sold") or 0
    offering = filing.get("total_offering")

    if filing.get("is_amendment") or filing.get("submission_type") == "D/A":
        return NOT_A_HIT, "amendment"
    if filing.get("industry_group") in rule["exclude_industry_groups"]:
        return NOT_A_HIT, f"industry group: {filing.get('industry_group')}"
    if not in_window(filing["filing_date"], rule, today):
        return NOT_A_HIT, f"filed {filing['filing_date']}, outside {rule['lookback_days']}-day window"
    if sold == 0 and not (offering and offering >= minimum):
        return NOT_A_HIT, "nothing sold and no offering amount"
    if filing.get("business_combination"):
        return REVIEW, "business combination or restructuring, not clearly a raise"
    if sold >= minimum:
        return HIT, f"sold {format_usd(sold)}"
    if sold > 0:
        return NEAR_MISS, f"sold {format_usd(sold)}, below {format_usd(minimum)}"
    return REVIEW, f"offering {format_usd(offering)} filed, nothing sold yet"


def signal_title(filing):
    """The human-readable line that stands in for a job title on a funding signal."""
    return f"Raised {format_usd(filing['total_sold'])} (SEC Form D)"
