"""Fills in the job board (greenhouse or ashby) for each company in config/boards.csv.

Three tries per company:
  1. guess the token from the name/domain and check it against Greenhouse's API
  2. fetch the homepage and /careers and look for a boards.greenhouse.io link
  3. guess the token on Ashby

Every greenhouse token - guessed OR scraped - has to pass the same check against
the company name, so a wrong board can't sneak in. Ashby's job list has no
company name, so an ashby board has to pass a stricter check: the website Ashby
has on file for that board must be the company's domain (that's how "circle"
got rejected - it belongs to circle.so, not circle.com), and it has to have at
least one open job (an empty board is usually an old one they've moved off).
Anything unresolved stays blank.
"""
import csv
import json
import re
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UA = "gtm-signal-detector/0.1 (learning project; contact: kanchipareek32@gmail.com)"
TIMEOUT = 10
GH_LINK = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)")


def fetch(url, limit=2_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(limit)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception:
        return None, b""


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def token_ok(token, name, domain):
    status, data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    if status != 200:
        return False
    try:
        jobs = json.loads(data).get("jobs", [])
    except Exception:
        return False
    if not jobs:
        return True
    api_name = norm(jobs[0].get("company_name", ""))
    label = norm(domain.split(".")[0])
    company = norm(name)
    return bool(api_name) and (api_name in company or company in api_name or label in api_name)


def guess_tokens(name, domain):
    label = domain.split(".")[0].lower()
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    return list(dict.fromkeys([label, slug]))


def scrape_token(domain):
    for path in ("", "/careers", "/jobs"):
        status, data = fetch(f"https://{domain}{path}")
        if data:
            m = GH_LINK.search(data.decode("utf-8", "ignore"))
            if m:
                return m.group(1)
        time.sleep(0.5)
    return None


ASHBY_JOBS = "https://api.ashbyhq.com/posting-api/job-board/{token}"
# Ashby's job page uses this to look up the org behind a board. It's not a
# documented api, so it's only used here, in the one-off board finder, never in
# the detector. If it ever breaks, ashby boards just won't be auto-filled.
ASHBY_ORG = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName"
ASHBY_ORG_QUERY = (
    "query ApiOrganizationFromHostedJobsPageName($organizationHostedJobsPageName: String!, "
    "$searchContext: OrganizationSearchContext) { organization: organizationFromHostedJobsPageName("
    "organizationHostedJobsPageName: $organizationHostedJobsPageName, searchContext: $searchContext) "
    "{ name publicWebsite } }"
)


def bare_domain(url):
    """'https://www.Airwallex.com/' -> 'airwallex.com'."""
    url = (url or "").lower().strip()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0]


def ashby_site(token):
    body = json.dumps({
        "operationName": "ApiOrganizationFromHostedJobsPageName",
        "variables": {"organizationHostedJobsPageName": token, "searchContext": "JobBoard"},
        "query": ASHBY_ORG_QUERY,
    }).encode()
    req = urllib.request.Request(ASHBY_ORG, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            org = (json.loads(resp.read()).get("data") or {}).get("organization") or {}
    except Exception:
        return None
    return org.get("publicWebsite")


def ashby_token_ok(token, domain):
    status, data = fetch(ASHBY_JOBS.format(token=token))
    if status != 200:
        return False
    try:
        jobs = json.loads(data.decode("utf-8")).get("jobs", [])
    except Exception:
        return False
    if not jobs:
        return False
    return bare_domain(ashby_site(token)) == bare_domain(domain)


# --- lever, workable, and boards the company's own site links to -------------------
# Same rule as above: a board only counts if it provably belongs to the domain.
#   lever    - the hosted board page links back to the company's website
#   workable - the account's website field is the company's domain
#   any ats  - the company's own homepage or careers page links to the board

def lever_token_ok(token, domain):
    from src.fetch import _web
    status, _, text = _web(f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1")
    if status != 200 or text.strip() in ("", "[]"):
        return False
    status, _, page = _web(f"https://jobs.lever.co/{token}")
    if status != 200:
        return False
    sites = {bare_domain(u) for u in re.findall(r'href="(https?://[^"]+)"', page)}
    return bare_domain(domain) in sites


def workable_token_ok(token, domain):
    from src.fetch import _web
    status, _, text = _web(f"https://apply.workable.com/api/v1/accounts/{token}")
    if status != 200:
        return False
    try:
        site = json.loads(text).get("url")
    except Exception:
        return False
    if bare_domain(site) != bare_domain(domain):
        return False
    from src.fetch import fetch_workable_board
    try:
        return bool(fetch_workable_board(token))
    except Exception:
        return False


ATS_LINKS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_.-]+)")),
    ("greenhouse", GH_LINK),
]
NOT_TOKENS = {"api", "embed", "j", "jobs", "careers", "static", "assets"}


def boards_linked_from_site(domain):
    """[(source, token)] for job boards the company's own site links to. A link
    from their own homepage or careers page is proof the board is theirs."""
    from src.fetch import _web
    found = []
    for path in ("", "/careers", "/jobs"):
        status, _, page = _web(f"https://{domain}{path}")
        if status != 200:
            continue
        for source, rx in ATS_LINKS:
            for token in rx.findall(page):
                token = urllib.parse.unquote(token).strip("/").lower()
                if token and token not in NOT_TOKENS and (source, token) not in found:
                    found.append((source, token))
    return found


def main():
    path = Path("config/boards.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    filled = 0
    for row in rows:
        if row["board_token"]:
            continue
        token = None
        for cand in guess_tokens(row["name"], row["domain"]):
            if token_ok(cand, row["name"], row["domain"]):
                token = cand
                break
        source = "greenhouse"
        if not token:
            scraped = scrape_token(row["domain"])
            if scraped and token_ok(scraped, row["name"], row["domain"]):
                token = scraped
        if not token:
            for cand in guess_tokens(row["name"], row["domain"]):
                if ashby_token_ok(cand, row["domain"]):
                    token, source = cand, "ashby"
                    break
        if token:
            row["board_token"] = token
            row["source"] = source
            filled += 1
            print(f"  ok    {row['name']:24} -> {source}:{token}")
        else:
            print(f"  gap   {row['name']}")
        time.sleep(0.5)

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["company_id", "name", "domain", "source", "board_token"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nfilled {filled} of {len(rows)} tokens")


if __name__ == "__main__":
    main()
