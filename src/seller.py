"""The seller profile: who a company sells to, and what would make a company a
good lead for them right now.

It's built once per seller and saved to config/sellers/<seller>.json, so I can
read it, fix it, and re-run without asking the model again. Two ways to make one:
  1. let Gemini read the seller's website (needs GEMINI_API_KEY)
  2. write the json by hand (no key needed) - same fields, see config/sellers/
"""
import html
import json
import re
import urllib.request
from pathlib import Path

from src.llm import ask_json

ROOT = Path(__file__).resolve().parent.parent
SELLERS_DIR = ROOT / "config" / "sellers"
UA = "Mozilla/5.0 (compatible; signal-to-crm-engine)"
PAGES = ("", "/pricing", "/customers", "/about")
MAX_SITE_CHARS = 12000

# The fixed list of industry groups a Form D can have. The model has to pick
# from these so the filter actually matches filings.
FORM_D_INDUSTRY_GROUPS = [
    "Agriculture", "Airlines and Airports", "Biotechnology", "Business Services", "Coal Mining",
    "Commercial", "Commercial Banking", "Computers", "Construction", "Electric Utilities",
    "Energy Conservation", "Environmental Services", "Health Insurance", "Hospitals and Physicians",
    "Insurance", "Investing", "Investment Banking", "Lodging and Conventions", "Manufacturing",
    "Oil and Gas", "Other", "Other Banking and Financial Services", "Other Energy",
    "Other Health Care", "Other Real Estate", "Other Technology", "Other Travel", "Pharmaceuticals",
    "REITS and Finance", "Residential", "Restaurants", "Retailing", "Telecommunications",
    "Tourism and Travel Services",
]

REQUIRED = ("seller", "seller_name", "sells", "buyers", "form_d_industry_groups",
            "min_raise_usd", "hire_keywords", "hire_reason", "funding_reason")


def slug(domain):
    return re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")


def profile_path(domain):
    return SELLERS_DIR / f"{slug(domain)}.json"


def _page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(1_500_000).decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()


def site_text(domain):
    """Readable text from the seller's homepage and a few likely pages."""
    chunks = []
    for path in PAGES:
        text = _page_text(f"https://{domain}{path}")
        if text:
            chunks.append(f"--- {path or '/'} ---\n{text[:4000]}")
    return "\n\n".join(chunks)[:MAX_SITE_CHARS]


PROMPT = """You are a B2B go-to-market analyst. Below is text from the website of {domain}.
Work out who this company sells to, and which public signals would show that a
company is likely to buy from them soon. Signals come from two places only:
  - job postings (we only see job TITLES), and
  - SEC Form D filings (amount raised, industry group, state, company age).

Return ONLY a JSON object with exactly these fields:
{{
  "seller": "{domain}",
  "seller_name": "the company's name",
  "sells": "one sentence: what they sell",
  "buyers": "two or three sentences: which companies buy this and why",
  "buyer_roles": ["job titles of the people who would buy or use it, e.g. 'Head of SEO'"],
  "competitors": ["domains of companies that sell something similar - they are never leads"],
  "news_queries": ["3 to 6 short phrases describing the BUYER companies' own businesses, used to search funding news, e.g. for a document-parsing api: 'legal ai', 'insurance claims automation', 'fintech document processing'. Describe what the buyers do, not what the seller does."],
  "hire_keywords": ["8 to 15 short lowercase phrases that, if they appear in a JOB TITLE at a company, mean that company is building the team that would buy this. Be specific to this product: for an SEO tool, 'seo' and 'organic search', not 'growth marketing' or 'digital marketing', which match almost every marketing job. Never use words like 'manager', 'lead' or 'engineer' on their own. If the product is a developer or AI tool, don't use titles every tech company hires ('machine learning', 'llm', 'ai engineer') - pick titles that show the specific need, e.g. 'document processing', 'data extraction', 'intelligent automation'."],
  "exclude_title_words": ["lowercase words that mean a matching title is NOT a buying signal: 'intern', 'assistant', 'recruiter', and words for neighbouring jobs this product doesn't serve (for an SEO tool: 'paid', 'ppc', 'social', 'design')"],
  "hire_reason": "one sentence, starting with 'Hiring for', on why such a hire makes them a good lead",
  "form_d_industry_groups": ["pick ONLY from this list: {groups}"],
  "min_raise_usd": a whole number, the smallest raise that suggests budget for this product,
  "max_raise_usd": a whole number, the largest raise that still fits (big enterprises may be out of reach),
  "startups_only": true if they mainly sell to startups and scaleups, else false,
  "funding_reason": "one sentence on why a fresh raise makes a company a good lead for them",
  "example_buyers": [{{"name": "company", "domain": "company.com"}}, ... 20 to 30 real startups or scaleups that would PAY for this product as customers. Not famous companies in the same space, not peers, not competitors, not big AI labs or platforms that would build it themselves. Think of who is in the seller's customer logos and case studies, and companies like them. Only include companies you are confident exist, with their real domain.]
}}

Website text:
{text}
"""


NO_SITE = """(The website couldn't be read - it blocks automated visitors. Use what you
already know about {domain} instead. If you don't know this company well enough
to say what it sells, return exactly {{"unknown": true}} and nothing else. Don't guess.)"""


def build_profile(domain, model=None):
    text = site_text(domain)
    blocked = len(text) < 200
    if blocked:
        text = NO_SITE.format(domain=domain)
    profile = ask_json(PROMPT.format(domain=domain, text=text,
                                     groups=", ".join(FORM_D_INDUSTRY_GROUPS)), model=model)
    if profile.get("unknown"):
        raise ValueError(f"https://{domain} blocks automated visitors and gemini doesn't know the company")
    profile["made_by"] = "gemini, from what it knows (site blocked us) - check it" if blocked else "gemini, from their website"
    return clean(profile)


def clean(profile):
    """Check the fields and drop anything the filters can't use."""
    missing = [f for f in REQUIRED if f not in profile]
    if missing:
        raise ValueError(f"seller profile is missing: {', '.join(missing)}")
    profile["form_d_industry_groups"] = [g for g in profile["form_d_industry_groups"]
                                         if g in FORM_D_INDUSTRY_GROUPS]
    profile["hire_keywords"] = sorted({k.strip().lower() for k in profile["hire_keywords"] if k.strip()})
    profile["exclude_title_words"] = sorted({k.strip().lower() for k in profile.get("exclude_title_words", []) if k.strip()})
    profile["min_raise_usd"] = int(profile["min_raise_usd"])
    profile["max_raise_usd"] = int(profile.get("max_raise_usd") or 0) or None
    profile.setdefault("startups_only", True)
    profile.setdefault("example_buyers", [])
    profile.setdefault("buyer_roles", [])
    profile.setdefault("competitors", [])
    profile.setdefault("news_queries", [])
    competitors = {c.lower().removeprefix("www.") for c in profile["competitors"]}
    profile["example_buyers"] = [b for b in profile["example_buyers"]
                                 if (b.get("domain") or "").lower().removeprefix("www.") not in competitors]
    return profile


def load_or_build(domain, refresh=False, model=None):
    """Use the saved profile if there is one, otherwise build and save it."""
    path = profile_path(domain)
    if path.exists() and not refresh:
        return clean(json.loads(path.read_text(encoding="utf-8"))), False
    profile = build_profile(domain, model=model)
    SELLERS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return profile, True
