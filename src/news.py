"""Funding news: raises announced in the press, worldwide.

Form D only covers US companies, and plenty of small rounds never get filed.
Perry AI (London, $2.2m seed, nov 2026) was the case that showed me this: no
form d, but six outlets covered the raise on the same day.

Where the headlines come from:
  - Google News, every query run on the US and the India editions
  - Indian funding feeds: Inc42 (funding tag), Entrackr, YourStory (funding)

How a headline becomes a raise:
  - with GEMINI_API_KEY: Gemini reads the headlines in batches and pulls out
    the company, the amount, the round and what the company does
  - without a key (or if Gemini fails): rule-based patterns like
    "<company> raises/secures/bags $X <round>", "<investor> invests in <company>"
    and "launches <company> with $X". Labelled "rule-based".

Amounts in rupees ("Rs 40 crore", "₹10 cr"), euros and pounds are converted to
rough US dollars, and the original wording is kept. A raise with no amount
stays in as "amount not disclosed" - a strategic or PE investment often has none.
"""
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src.llm import LLMError, ask_json, has_key

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
GOOGLE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
EDITIONS = {"us": ("en-US", "US", "US:en"), "in": ("en-IN", "IN", "IN:en")}
GENERAL_QUERIES = ["startup raises seed round", "startup raises series a", "raises pre-seed funding",
                   "strategic investment in startup"]
# Indian funding feeds. Inc42's tag feed pages back with ?paged=N.
FEEDS = {
    "inc42": ["https://inc42.com/tag/funding/feed/"] + [f"https://inc42.com/tag/funding/feed/?paged={n}" for n in range(2, 7)],
    "entrackr": ["https://entrackr.com/rss"],
    "yourstory": ["https://yourstory.com/category/funding/feed"],
}
BATCH = 60

# very rough fx, fine for "is this raise in range"
USD_PER = {"USD": 1.0, "INR": 1 / 84, "EUR": 1.08, "GBP": 1.27}
UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mln": 1e6, "million": 1e6,
        "b": 1e9, "bn": 1e9, "billion": 1e9, "cr": 1e7, "crore": 1e7, "crores": 1e7,
        "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5}


# --- fetching ----------------------------------------------------------------------

GOOGLE_GAP = 1.5      # seconds between google news requests; it returns 429 if you go faster
_last_google = [0.0]


def _get(url):
    """Page text, or '' on failure. Google News gets spaced out, and a 429 is retried with backoff."""
    is_google = "news.google.com" in url
    for attempt in range(3):
        if is_google:
            wait = _last_google[0] + GOOGLE_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_google[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            return ""
        except Exception:
            return ""
    return ""


def _clean(text):
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def parse_rss(text, source):
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", text or "", re.S):
        title = re.search(r"<title>(.*?)</title>", raw, re.S)
        if not title:
            continue
        link = re.search(r"<link>(.*?)</link>", raw, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)
        desc = re.search(r"<description>(.*?)</description>", raw, re.S)
        items.append({
            "title": re.sub(r"\s+", " ", _clean(title.group(1))),
            "link": _clean(link.group(1)) if link else "",
            "date": date.group(1).strip() if date else "",
            "summary": re.sub(r"\s+", " ", _clean(desc.group(1)))[:500] if desc else "",
            "source": source,
        })
    return items


def _rss(query, days, edition="us"):
    hl, gl, ceid = EDITIONS[edition]
    url = GOOGLE.format(q=urllib.parse.quote(f"{query} when:{days}d"), hl=hl, gl=gl, ceid=ceid)
    return parse_rss(_get(url), f"google news ({edition})")


def _feed(name):
    items = []
    for url in FEEDS[name]:
        items += parse_rss(_get(url), name)
    return items


def _iso(rfc_date):
    try:
        return parsedate_to_datetime(rfc_date).date().isoformat()
    except Exception:
        return ""


def search_headlines(profile, days):
    """Recent funding headlines from google news (us + india) and the indian feeds."""
    queries = list(GENERAL_QUERIES)
    for term in profile.get("news_queries", [])[:8]:
        queries.append(term if re.search(r"\b(raises?|funding|invest)", term) else f"{term} raises")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    batches = [(q, _rss(q, days, ed)) for q in queries for ed in EDITIONS]
    batches += [(name, _feed(name)) for name in FEEDS]
    seen, out = set(), []
    for q, items in batches:
        for item in items:
            title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])  # drop " - Dealroom" style source suffix
            key = re.sub(r"[^a-z0-9]", "", title.lower())[:80]
            day = _iso(item["date"])
            if key in seen or (day and day < cutoff):
                continue
            seen.add(key)
            out.append({**item, "date": day, "query": q})
    return out


# --- amounts and rounds ----------------------------------------------------------------

MONEY = re.compile(
    r"(?P<cur>\$|us\$|usd|₹|rs\.?|inr|€|eur|£|gbp)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>k|thousand|mn|mln|million|m|bn|billion|b|cr|crores?|lakhs?|lacs?)?\b",
    re.I)
MONEY_AFTER = re.compile(  # "40 crore rupees", "5 million dollars"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>million|mn|billion|bn|crores?|cr|lakhs?)\s*(?P<cur>dollars|rupees|euros|pounds)",
    re.I)
CUR = {"$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD", "₹": "INR", "rs": "INR", "rs.": "INR",
       "inr": "INR", "rupees": "INR", "€": "EUR", "eur": "EUR", "euros": "EUR", "£": "GBP",
       "gbp": "GBP", "pounds": "GBP"}


def parse_amount(text):
    """'Rs 40 crore' -> (476190, 'Rs 40 crore'). Returns (usd or None, original or None)."""
    for rx in (MONEY, MONEY_AFTER):
        for m in rx.finditer(text or ""):
            cur = CUR.get(m.group("cur").lower().rstrip("."), None) or CUR.get(m.group("cur").lower())
            if not cur:
                continue
            num = float(m.group("num").replace(",", ""))
            unit = (m.group("unit") or "").lower()
            if not unit and num < 1000:
                continue  # "$5" on its own is almost never a round
            usd = int(num * UNIT.get(unit, 1) * USD_PER[cur])
            if usd < 10_000:
                continue
            return usd, m.group(0).strip()
    return None, None


ROUND = re.compile(r"\b(pre-seed|pre seed|seed|pre-series [a-f]|series [a-f]\+?|bridge|growth|"
                   r"strategic|private equity|pe investment|debt|angel)\b", re.I)


def parse_round(text):
    m = ROUND.search(text or "")
    return m.group(1).lower().replace("pre seed", "pre-seed") if m else None


# --- rule-based extraction ---------------------------------------------------------------

VERBS = r"(?:raises|raised|secures|secured|bags|bagged|nets|netted|lands|closes|closed|gets|receives|" \
        r"grabs|snags|picks up|scores|announces|completes|attracts)"
DESCRIPTOR = re.compile(r"(?:.*\b(?:startup|start-up|brand|platform|company|firm|maker|provider|unicorn|"
                        r"-backed|backed|based|'s|’s)\s+)", re.I)
RAISE = re.compile(rf"^(?P<co>.+?)\s+{VERBS}\s+(?P<rest>.+)$", re.I)
INVEST = re.compile(r"^(?P<inv>.+?)\s+(?:invests|makes (?:a )?(?:strategic |growth |minority |majority )?investment|"
                    r"announces (?:a )?(?:strategic |growth )?investment|backs|leads [\w$₹€£.\s-]+? round)\s+in\s+"
                    r"(?P<co>.+?)(?:,|\s+to\s+|\s+for\s+|\s+as\s+|$)(?P<rest>.*)", re.I)
LAUNCH = re.compile(r"(?:launches|introduces|unveils|debuts)\s+(?P<desc>.*?)\b(?:startup|platform|company)\s+"
                    r"['‘’\"]?(?P<co>[A-Z][\w&.\-]*(?:\s[A-Z][\w&.\-]*){0,2})['‘’\"]?\s+with\s+(?P<rest>.+)", re.I)
EMERGE = re.compile(r"^(?P<co>.+?)\s+(?:launches|emerges from stealth|comes out of stealth|debuts|exits stealth)"
                    r"\s+(?:\w+\s+){0,3}?with\s+(?P<rest>.+)$", re.I)
MONEY_WORDS = re.compile(r"funding|round|investment|capital|seed|series|\$|₹|rs\.?\s*\d|€|£|crore|million|mn\b", re.I)


def _company(raw):
    """'Y Combinator-Backed Firstwork' -> 'Firstwork', "India’s Humyn Labs" -> 'Humyn Labs'."""
    raw = raw.strip(" '\"‘’:")
    m = DESCRIPTOR.match(raw)
    name = raw[m.end():] if m and m.end() < len(raw) else raw
    desc = raw[:m.end()].strip() if m and m.end() < len(raw) else ""
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip(" ,'\"‘’")
    words = name.split()
    # "E&S underwriter Aurenity": a lowercase word in the middle means everything
    # up to it is a description, and the company is the capitalised words after it
    lower = [i for i, w in enumerate(words) if w[:1].islower()]
    if lower and lower[-1] < len(words) - 1:
        desc = " ".join(filter(None, [desc, " ".join(words[:lower[-1] + 1])]))
        words = words[lower[-1] + 1:]
        name = " ".join(words)
    desc = re.sub(r"\S+['’]s$", "", desc).strip()  # "India's" says where, not what
    if not words or len(words) > 5 or not words[0][:1].isalnum():
        return None, desc
    return name, desc


def _purpose(rest):
    m = re.search(r"\b(?:to|for)\s+(.+)$", rest)
    return m.group(1).strip() if m else ""


def rule_extract(headline):
    """A raise dict from one headline, or None. No llm."""
    title = headline["title"]
    for rx, kind in ((LAUNCH, "launch"), (INVEST, "invest"), (EMERGE, "raise"), (RAISE, "raise")):
        m = rx.search(title)
        if not m:
            continue
        rest = m.group("rest") or ""
        if kind == "raise" and not MONEY_WORDS.search(rest):
            continue  # "X secures contract with Y" isn't a raise
        name, desc = _company(m.group("co"))
        if not name:
            continue
        usd, original = parse_amount(rest if kind != "invest" else title)
        what = " ".join(x for x in (desc, m.groupdict().get("desc") or "", _purpose(rest)) if x).strip()
        return {
            "company": name,
            "amount": usd,
            "amount_text": original or "not disclosed",
            "round": parse_round(title) or ("strategic" if kind == "invest" else None),
            "what_they_do": what,
            "country": None,
            "extracted_by": "rule-based",
        }
    return None


# --- llm extraction ------------------------------------------------------------------------

EXTRACT = """Below are news headlines, numbered. Find the ones that announce a company
raising money: pre-seed, seed, series a/b/c..., a funding round, or a strategic,
growth or private-equity investment into the company. Ignore guides, opinion
pieces, funds launched by investors, acquisitions of a whole company, and
anything that isn't money going into one specific company.

For each real raise return one object. If several headlines are about the same
raise, return it once.

Return ONLY a JSON object: {{"raises": [
  {{"headline": <number>,
    "company": "the company that raised",
    "amount_text": "the amount exactly as written, e.g. 'Rs 40 crore' or '$5.1M', or null",
    "amount_usd": whole number in US dollars (convert rupees, euros, pounds roughly; 1 crore = 10 million rupees), or null if not stated,
    "round": "pre-seed / seed / series a / strategic / private equity / ... or null",
    "what_they_do": "a few words from the headline, e.g. 'AI legal software for private capital funds'",
    "country": "country or city if the headline says, else null"}}
]}}

Headlines:
{lines}
"""


def _llm_extract(chunk, model):
    lines = "\n".join(f"{i}. {h['title']}" for i, h in enumerate(chunk))
    reply = ask_json(EXTRACT.format(lines=lines), model=model)
    out = []
    for r in reply.get("raises", []):
        try:
            h = chunk[int(r["headline"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if not r.get("company"):
            continue
        usd = int(r["amount_usd"]) if r.get("amount_usd") else None
        text = r.get("amount_text")
        if usd is None and text:
            usd, _ = parse_amount(text)
        out.append((h, {
            "company": r["company"].strip(),
            "amount": usd,
            "amount_text": text or "not disclosed",
            "round": (r.get("round") or parse_round(h["title"]) or None),
            "what_they_do": r.get("what_they_do") or "",
            "country": r.get("country"),
            "extracted_by": "gemini",
        }))
    return out


def extract_raises(headlines, model=None, use_llm=None):
    """Which headlines are raises, with the details. Gemini if there's a key, else rules."""
    use_llm = has_key() if use_llm is None else use_llm
    found = []
    for start in range(0, len(headlines), BATCH):
        chunk = headlines[start:start + BATCH]
        pairs = None
        if use_llm:
            try:
                pairs = _llm_extract(chunk, model)
            except LLMError:
                pairs = None  # fall back to rules for this batch
        if pairs is None:
            pairs = [(h, r) for h in chunk for r in [rule_extract(h)] if r]
        for h, r in pairs:
            found.append({**r, "headline": h["title"], "url": h["link"], "date": h["date"],
                          "news_source": h.get("source", "")})
    return dedupe(found)


def _company_key(name):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(inc|ltd|pvt|private|limited|technologies|labs|ai)\b", "",
                                           name.lower()))


def _same_amount(a, b):
    return a is None or b is None or abs(a - b) <= 0.3 * max(a, b)


def dedupe(raises):
    """One row per company + amount + round. Different outlets describe the same
    raise differently ('investment fund management startup' vs 'ai legal software
    for private capital'), so every description is kept for the fit check, and
    every source url is kept as evidence."""
    kept = []
    for r in raises:
        key = _company_key(r["company"]) or r["company"].lower()
        match = next((k for k in kept if k["_key"] == key and _same_amount(k["amount"], r["amount"])
                      and (not k["round"] or not r["round"] or k["round"] == r["round"])), None)
        if not match:
            kept.append({**r, "_key": key, "sources": [r["url"]] if r["url"] else []})
            continue
        if r["what_they_do"] and r["what_they_do"].lower() not in match["what_they_do"].lower():
            match["what_they_do"] = f"{match['what_they_do']}; {r['what_they_do']}".strip("; ")
        if match["amount"] is None and r["amount"] is not None:
            match["amount"], match["amount_text"] = r["amount"], r["amount_text"]
        match["round"] = match["round"] or r["round"]
        match["country"] = match["country"] or r["country"]
        if r["url"] and r["url"] not in match["sources"]:
            match["sources"].append(r["url"])
        if r["date"] and (not match["date"] or r["date"] < match["date"]):
            match["date"] = r["date"]  # earliest report is the announcement date
    for k in kept:
        k.pop("_key", None)
    return kept
