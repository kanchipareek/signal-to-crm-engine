"""Fit check: would this company actually buy from the seller?

A signal on its own isn't a lead. Perplexity hiring eight ML engineers is a
real signal, but Perplexity isn't going to buy a document-parsing api from
Unsiloed - it's a peer, not a buyer. And a biotech that raised $3m isn't an
seo agency's customer just because it raised.

So every company that has a signal gets a short description (its homepage
title and meta description, or the news headline), and Gemini judges them in
batches against the seller profile: yes, maybe or no, with a one-line reason.
'no' gets dropped from the list. Batching keeps it to a few calls per run.
"""
import html
import re

from src.leads import fetch_home
from src.llm import LLMError, ask_json

BATCH = 15


def describe(lead):
    """A short 'what this company does' line for the fit check."""
    parts = []
    if lead.get("what_they_do"):
        parts.append(lead["what_they_do"])
    if lead.get("domain"):
        _, page = fetch_home(lead["domain"])
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page or "")
        desc = (re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', page or "")
                or re.search(r'(?is)<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', page or ""))
        for m in (title, desc):
            if m:
                parts.append(re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:200])
    if lead.get("industry_group"):
        parts.append(f"SEC industry group: {lead['industry_group']}")
    if lead.get("hires"):
        parts.append("hiring: " + ", ".join(h["title"] for h in lead["hires"][:4]))
    return " | ".join(p for p in parts if p) or "no description found"


PROMPT = """You are qualifying sales leads for {seller_name} ({seller}).

What {seller_name} sells: {sells}
Who buys it: {buyers}
Competitors (never a lead): {competitors}

For each company below, decide if it is likely to BUY {seller_name}'s product.
- "yes": the description itself shows its business matches the buyers above.
  Don't infer it from the industry or the funding alone.
- "maybe": could fit, but the description is too thin to tell. Use this, not
  "no", when you simply don't have enough information.
- "no": a peer or competitor, a different market, too big to buy from a startup
  like this, or a company whose business doesn't need this product.
Judge the company's business, not just its signal. A company hiring AI
engineers is not a buyer of an AI tool if it builds AI models itself.

Return ONLY a JSON object: {{"leads": [{{"id": <number>, "fit": "yes|maybe|no", "reason": "one short sentence"}}]}}

Companies:
{lines}
"""


def check_fit(leads, profile, model=None):
    """Adds 'fit' and 'fit_reason' to each lead. Leads it can't judge stay 'maybe'."""
    for lead in leads:
        lead.setdefault("description", describe(lead))
    failed = 0
    for start in range(0, len(leads), BATCH):
        chunk = leads[start:start + BATCH]
        lines = "\n".join(f"{i}. {l['name']} ({l.get('domain') or 'no website'}): {l['description']}"
                          for i, l in enumerate(chunk))
        prompt = PROMPT.format(
            seller_name=profile["seller_name"], seller=profile["seller"], sells=profile["sells"],
            buyers=profile["buyers"], competitors=", ".join(profile.get("competitors", [])) or "none listed",
            lines=lines)
        reply = None
        for _ in range(2):  # the model sometimes returns broken json; one bad batch shouldn't sink the run
            try:
                reply = ask_json(prompt, model=model)
                break
            except LLMError as exc:
                last = exc
        if reply is None:
            failed += 1
            if failed == len(range(0, len(leads), BATCH)):
                raise last  # every batch failed: say so instead of pretending it ran
            continue
        for r in reply.get("leads", []):
            try:
                lead = chunk[int(r["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            fit = str(r.get("fit", "")).lower()
            if fit in ("yes", "maybe", "no"):
                lead["fit"] = fit
                lead["fit_reason"] = r.get("reason", "")
    for lead in leads:
        lead.setdefault("fit", "maybe")
        lead.setdefault("fit_reason", "")
    return failed
