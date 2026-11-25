"""Lead finding: given a seller profile, turn companies into scored leads.

Pieces, each testable on its own:
  - name_core / guess_domains / find_domain : Form D gives a legal name, not a
    website, so I guess the domain and confirm it on the homepage
  - find_board      : the company's greenhouse or ashby board, if it has one
  - match_hires     : job titles that mean "building the team that buys this"
  - sec_by_name     : the company's SEC filings, matched by exact legal name
  - score / why     : rank leads and say in plain words why each one fits
"""
import re
import urllib.error
import urllib.parse
import urllib.request

from src.match import _has, _norm_text

UA = "Mozilla/5.0 (compatible; signal-to-crm-engine)"
TLDS = ("com", "ai", "io", "co")

SUFFIX_WORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd",
                "limited", "holdings", "holding", "pbc", "plc", "the", "lp"}
GENERIC_TAIL = {"labs", "technologies", "technology", "software", "systems", "health",
                "therapeutics", "robotics", "bio", "ai", "group", "solutions"}
FUND_NAME = re.compile(r"\b(llc|l l c|lp|l p|fund|funds|trust|partners|capital|portfolio|"
                       r"investors|ventures|reit|dst|spv|series)\b")
PARKED = ("domain is for sale", "buy this domain", "domain for sale", "this domain may be for sale",
          "hugedomains", "sedo", "afternic", "dan.com", "godaddy", "parked", "namecheap")
SENIOR = ("chief", "vp", "vice president", "head", "director", "lead", "principal")


# --- names and websites --------------------------------------------------------

def name_core(entity):
    """'Deep Cogito Inc.' -> ['deep', 'cogito']. Drops legal suffixes."""
    words = _norm_text(entity).split()
    return [w for w in words if w not in SUFFIX_WORDS]


def looks_like_fund(entity):
    return FUND_NAME.search(_norm_text(entity)) is not None


def guess_domains(entity):
    """Likely domains for a company name, most likely first."""
    core = name_core(entity)
    if not core:
        return []
    bases = ["".join(core)]
    if len(core) > 1 and core[-1] in GENERIC_TAIL:
        bases.append("".join(core[:-1]))
    if len(core) > 1:
        bases.append("-".join(core))
    out = []
    if len(core) > 1 and core[-1] in TLDS:
        out.append(f"{''.join(core[:-1])}.{core[-1]}")  # "Perry AI" -> perry.ai
    for base in dict.fromkeys(bases):
        for tld in TLDS:
            out.append(f"{base}.{tld}")
    return list(dict.fromkeys(out))


def fetch_home(domain):
    """(final host, page text) for https://domain, or (None, '')."""
    req = urllib.request.Request(f"https://{domain}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            final = urllib.parse.urlparse(resp.geturl()).hostname or ""
            raw = resp.read(400_000).decode("utf-8", "ignore")
    except Exception:
        return None, ""
    return final.lower().removeprefix("www."), raw


def site_exists(domain):
    """True if the domain answers at all. A 403 from a bot blocker still means it's real."""
    req = urllib.request.Request(f"https://{domain}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def homepage_matches(domain, entity, final_host, page):
    """Does this homepage belong to this company (and isn't a parked domain)?"""
    if not final_host or not page:
        return False
    text = page.lower()
    if any(p in text[:20000] for p in PARKED):
        return False
    base = domain.split(".")[0].replace("-", "")
    if base not in final_host.replace("-", "").replace(".", ""):
        return False  # redirected somewhere else entirely
    if final_host.rsplit(".", 1)[-1] not in TLDS:
        return False  # e.g. alcar.com -> alcar.se: a different (foreign) company with the same name
    core = name_core(entity)
    flat = re.sub(r"[^a-z0-9]", "", text[:60000])
    return "".join(core) in flat or all(_has(_norm_text(text[:60000]), w) for w in core)


def find_domain(entity):
    """Guess the company's website from its legal name and confirm it."""
    for domain in guess_domains(entity):
        host, page = fetch_home(domain)
        if homepage_matches(domain, entity, host, page):
            return host or domain
    return None


# --- job boards ----------------------------------------------------------------

def find_board(name, domain):
    """(source, token) for the company's job board, or None.

    Uses the same checks as scripts/find_boards.py, in this order:
      1. a board the company's own site links to (that link is the proof)
      2. greenhouse - the board has to name the company
      3. ashby      - ashby has to list the company's domain
      4. lever      - the hosted board has to link back to the domain
      5. workable   - the account's website has to be the domain
      6. their own careers page (/careers, /jobs) if it lists any roles
    """
    from scripts.find_boards import (ashby_token_ok, boards_linked_from_site, guess_tokens,
                                     lever_token_ok, token_ok, workable_token_ok)
    from src.fetch import FetchError, fetch_careers_page, fetch_postings
    for source, token in boards_linked_from_site(domain):
        try:
            if fetch_postings(source, token):
                return source, token
        except (FetchError, ValueError):
            continue
    tokens = guess_tokens(name, domain)
    for check, source in ((lambda t: token_ok(t, name, domain), "greenhouse"),
                          (lambda t: ashby_token_ok(t, domain), "ashby"),
                          (lambda t: lever_token_ok(t, domain), "lever"),
                          (lambda t: workable_token_ok(t, domain), "workable")):
        for token in tokens:
            if check(token):
                return source, token
    try:
        if fetch_careers_page(domain):
            return "careers", domain
    except FetchError:
        pass
    return None


def match_hires(postings, profile):
    """Postings whose title is a buying signal for the seller. Two kinds:

      team     - matches hire_keywords: they're building the team that would
                 buy or use the product ("Head of SEO" for an SEO tool)
      ops_pain - matches ops_pain_keywords: they're hiring people to do by hand
                 what the product automates ("Loan Processor" for a document
                 parsing api). Often the stronger signal of the two.

    Each match says which kind hit, and which keyword.
    """
    kinds = [("ops_pain", profile.get("ops_pain_keywords", [])), ("team", profile.get("hire_keywords", []))]
    exclude = profile.get("exclude_title_words", [])
    found = []
    for p in postings:
        title = _norm_text(p["title"])
        if any(_has(title, _norm_text(x)) for x in exclude):
            continue
        for kind, keywords in kinds:
            hit = next((k for k in keywords if _has(title, _norm_text(k))), None)
            if hit:
                found.append({**p, "keyword": hit, "kind": kind,
                              "senior": any(_has(title, s) for s in SENIOR)})
                break
    found.sort(key=lambda p: (p["kind"] != "ops_pain", not p["senior"], p["title"]))
    return found


# --- sec -----------------------------------------------------------------------

def same_company(entity_a, entity_b):
    return name_core(entity_a) == name_core(entity_b) and bool(name_core(entity_a))


def sec_by_name(name):
    """The one SEC filer whose legal name matches exactly, or None.

    Exact match on the name without suffixes ('Vanta' = 'Vanta Inc.'), funds
    and SPVs dropped, and only if exactly one filer matches - two matches means
    I can't tell which is the company, so I don't guess.
    """
    from src.sec import lookup_entities
    matches = [(cik, ent) for cik, ent in lookup_entities(name)
               if same_company(ent, name) and not looks_like_fund(ent)]
    return matches[0] if len(matches) == 1 else None


# --- scoring and wording -------------------------------------------------------

FIT_POINTS = {"yes": 5, "maybe": 0}  # a clear fit with one signal beats an unclear fit with two


def score(lead):
    s = 0
    if lead.get("funding"):
        s += 3
    hires = lead.get("hires", [])
    # an ops-pain hire (doing by hand what the seller automates) counts like a senior team hire
    s += min(4, sum(2 if (h["senior"] or h.get("kind") == "ops_pain") else 1 for h in hires))
    if lead.get("funding") and hires:
        s += 2
    s += FIT_POINTS.get(lead.get("fit"), 0)
    return s


def why(lead, profile, money):
    """Plain-words reasons this company is a lead for the seller."""
    reasons = []
    if lead.get("fit_reason"):
        reasons.append(f"Fit ({lead['fit']}): {lead['fit_reason']}")
    f = lead.get("funding")
    if f:
        if f.get("amount"):
            amount = money(f["amount"])
            if f.get("amount_text") and not f["amount_text"].startswith("$"):
                amount += f" ({f['amount_text']})"  # "₹40 crore" stays visible next to the dollar guess
        else:
            amount = "money (amount not disclosed)"
        rnd = f" in a {f['round']} round" if f.get("round") else ""
        if f.get("source") == "news":
            where = f"in the news, {f['date']}: \"{f['headline']}\""
        else:
            where = f"SEC Form D, filed {f['date']}"
        reasons.append(f"Raised {amount}{rnd} ({where}). {profile['funding_reason']}")
    hires = lead.get("hires", [])
    for kind, reason in (("ops_pain", profile.get("ops_pain_reason") or
                          f"They're hiring people to do by hand what {profile['seller_name']} automates."),
                         ("team", profile["hire_reason"])):
        group = [h for h in hires if h.get("kind", "team") == kind]
        if not group:
            continue
        titles = ", ".join(f"\"{h['title']}\"" for h in group[:3])
        more = f" and {len(group) - 3} more" if len(group) > 3 else ""
        label = "ops-pain hire" if kind == "ops_pain" else "team hire"
        reasons.append(f"Hiring {titles}{more} ({label}). {reason}")
    return reasons
