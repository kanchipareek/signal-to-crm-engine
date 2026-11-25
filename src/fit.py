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
from src.llm import LLMError, ask_json, has_key
from src.match import _has, _norm_text

BATCH = 15
STOP = set("""a an the and or of for to in on with by at from into that this these those their its it is are be
who which what why how they them we our you your as such like need needs want wants use uses using buy buys
companies company business businesses teams team startups startup scaleups growing large small mid market
raises raise funding round platform platforms software tool tools product products service services data
help helps more most also any all new than other within across based""".split())


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


def _phrases(profile):
    out = []
    for p in profile.get("buyer_industries", []) + profile.get("news_queries", []):
        p = re.sub(r"\b(raises?|funding|startups?|companies|investment)\b", "", _norm_text(p)).strip()
        if p and p not in out:
            out.append(p)
    return out


def _words(profile):
    text = " ".join([profile.get("buyers", "")] + _phrases(profile))
    return sorted({w for w in _norm_text(text).split() if len(w) > 3 and w not in STOP})


def rule_fit(lead, profile):
    """Keyword fit check, no llm: does the description name the seller's buyers?

    yes   - it names one of the buyer industries / news phrases ('mortgage lender'),
            or the company is hiring people to do by hand what the seller automates
    maybe - it shares one or two words with the buyer description, or there's
            nothing to judge by
    no    - it's a competitor, or its description shares nothing with the buyers
    """
    domain = (lead.get("domain") or "").lower()
    if domain and domain in {c.lower().removeprefix("www.") for c in profile.get("competitors", [])}:
        return "no", "a competitor"
    if any(h.get("kind") == "ops_pain" for h in lead.get("hires", [])):
        titles = ", ".join(h["title"] for h in lead["hires"] if h.get("kind") == "ops_pain")[:120]
        return "yes", f"hiring people to do by hand what {profile['seller_name']} automates ({titles})"
    desc = _norm_text(lead.get("description", ""))
    if not desc or desc == "no description found":
        return "maybe", "no description to judge by"
    for phrase in _phrases(profile):
        words = phrase.split()
        if _has(desc, phrase) or (len(words) > 1 and all(_has(desc, w) for w in words)):
            return "yes", f"its description mentions '{phrase}'"
    hits = [w for w in _words(profile) if _has(desc, w)]
    if hits:
        return "maybe", f"shares words with the buyer description: {', '.join(hits[:4])}"
    return "no", f"nothing in its description matches who {profile['seller_name']} sells to"


def check_fit(leads, profile, model=None, use_llm=None):
    """Adds 'fit', 'fit_reason' and 'fit_by' to each lead.

    With a gemini key, gemini judges in batches; a batch that fails twice falls
    back to the keyword check. Without a key, everything uses the keyword check.
    Returns how many batches had to fall back.
    """
    use_llm = has_key() if use_llm is None else use_llm
    for lead in leads:
        lead.setdefault("description", describe(lead))
    if not use_llm:
        for lead in leads:
            lead["fit"], lead["fit_reason"] = rule_fit(lead, profile)
            lead["fit_by"] = "rule-based"
        return 0
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
            for lead in chunk:  # gemini failed twice on this batch: use the keyword check
                lead["fit"], lead["fit_reason"] = rule_fit(lead, profile)
                lead["fit_by"] = "rule-based"
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
                lead["fit_by"] = "gemini"
    for lead in leads:
        if "fit" not in lead:  # gemini skipped it in its reply
            lead["fit"], lead["fit_reason"] = rule_fit(lead, profile)
            lead["fit_by"] = "rule-based"
    return failed
