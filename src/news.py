"""Funding news: raises announced in the press, worldwide.

Form D only covers US companies, and plenty of small rounds never get filed.
Perry AI (London, $2.2m seed, nov 2026) was the case that showed me this: no
form d, but six outlets covered the raise on the same day.

So I search Google News for recent funding headlines - some general ones, and
some aimed at the seller's buyers (e.g. "legal ai raises") - and let Gemini
pull out the company, the amount, and what the company does. Headlines only,
so it's cheap: one Gemini call per batch of headlines.

Google News RSS has no key and no documented limits, so I keep the number of
searches small.
"""
import html
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src.llm import ask_json

RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
UA = "Mozilla/5.0 (compatible; signal-to-crm-engine)"
GENERAL_QUERIES = ["startup raises seed round", "startup raises series a", "raises pre-seed funding"]
BATCH = 60


def _rss(query, days):
    q = urllib.parse.quote(f"{query} when:{days}d")
    req = urllib.request.Request(RSS.format(q=q), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "ignore")
    except Exception:
        return []
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", text, re.S):
        title = re.search(r"<title>(.*?)</title>", raw, re.S)
        link = re.search(r"<link>(.*?)</link>", raw, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)
        if not title:
            continue
        items.append({
            "title": html.unescape(title.group(1)).strip(),
            "link": (link.group(1).strip() if link else ""),
            "date": date.group(1).strip() if date else "",
        })
    return items


def _iso(rfc_date):
    try:
        return parsedate_to_datetime(rfc_date).date().isoformat()
    except Exception:
        return ""


def search_headlines(profile, days):
    """Recent funding headlines: general ones plus ones aimed at the seller's buyers."""
    queries = list(GENERAL_QUERIES)
    for term in profile.get("news_queries", [])[:6]:
        queries.append(f"{term} raises")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    seen, out = set(), []
    for q in queries:
        for item in _rss(q, days):
            key = re.sub(r"[^a-z0-9]", "", item["title"].lower())[:80]
            day = _iso(item["date"])
            if key in seen or (day and day < cutoff):
                continue
            seen.add(key)
            out.append({**item, "date": day, "query": q})
    return out


EXTRACT = """Below are news headlines, numbered. Find the ones that announce a company
raising money (seed, series a/b/c, pre-seed, a funding round). Ignore guides,
opinion pieces, fund launches by investors, acquisitions and anything that
isn't a company announcing its own raise.

For each real raise return one object. If several headlines are about the same
raise, return it once.

Return ONLY a JSON object: {{"raises": [
  {{"headline": <number>,
    "company": "the company that raised",
    "amount_usd": whole number in US dollars (convert euros/pounds roughly), or null if not stated,
    "round": "seed / series a / ... or null",
    "what_they_do": "a few words from the headline, e.g. 'AI legal software for private capital funds'",
    "country": "country or city if the headline says, else null"}}
]}}

Headlines:
{lines}
"""


def extract_raises(headlines, model=None):
    """Ask Gemini which headlines are raises, and pull out the details."""
    raises = []
    for start in range(0, len(headlines), BATCH):
        chunk = headlines[start:start + BATCH]
        lines = "\n".join(f"{i}. {h['title']}" for i, h in enumerate(chunk))
        reply = ask_json(EXTRACT.format(lines=lines), model=model)
        for r in reply.get("raises", []):
            try:
                h = chunk[int(r["headline"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if not r.get("company"):
                continue
            raises.append({
                "company": r["company"].strip(),
                "amount": int(r["amount_usd"]) if r.get("amount_usd") else None,
                "round": r.get("round"),
                "what_they_do": r.get("what_they_do") or "",
                "country": r.get("country"),
                "headline": h["title"],
                "url": h["link"],
                "date": h["date"],
            })
    # one row per company. different outlets describe the same raise differently
    # ("investment fund management startup" vs "ai legal software for private
    # capital"), so I keep every description - the fit check needs the best one.
    by_name = {}
    for r in raises:
        key = re.sub(r"[^a-z0-9]", "", r["company"].lower())
        if key not in by_name:
            by_name[key] = r
            continue
        kept = by_name[key]
        if r["what_they_do"] and r["what_they_do"].lower() not in kept["what_they_do"].lower():
            kept["what_they_do"] = f"{kept['what_they_do']}; {r['what_they_do']}".strip("; ")
        kept["amount"] = kept["amount"] or r["amount"]
        kept["country"] = kept["country"] or r["country"]
    return list(by_name.values())
