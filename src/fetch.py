"""Talks to the job boards and returns the open postings for a board.

One job: given a source ('greenhouse', 'ashby', 'lever', 'workable', or
'careers' for the company's own careers page) and a board token (the domain,
for 'careers'), ask that board for the company's jobs and return them as a
clean list of dicts, in the same shape whatever the source. No database, no
matching - just fetching.
"""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{token}"
UA = "gtm-signal-detector/0.1 (learning project; contact: kanchipareek32@gmail.com)"
TIMEOUT = 10


class FetchError(Exception):
    pass


def _get(url, attempts=3):
    delay = 1.0
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FetchError(f"board not found: {url}")
            if exc.code in (429, 500, 502, 503) and attempt < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise FetchError(f"http {exc.code} for {url}")
        except Exception as exc:
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise FetchError(f"{type(exc).__name__} for {url}")
    raise FetchError(f"gave up on {url}")


def _department(job):
    """Greenhouse has no departments field - the category lives in metadata.

    A metadata value is usually a string, but multi-select fields hand back a
    LIST, so I flatten both into one space-separated string.
    """
    parts = []
    for field in (job.get("metadata") or []):
        value = field.get("value")
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
        elif value:
            parts.append(str(value))
    return " ".join(parts) if parts else None


def fetch_board(token):
    """Return a list of postings for one board, newest info included."""
    data = json.loads(_get(API.format(token=token)))
    postings = []
    for job in data.get("jobs", []):
        postings.append({
            "external_id": str(job["id"]),
            "title": job.get("title", ""),
            "source_url": job.get("absolute_url", ""),
            "department": _department(job),
            "first_published": job.get("first_published"),
            "updated_at": job.get("updated_at"),
            "company_name": job.get("company_name", ""),
        })
    return postings


def _ashby_department(job):
    """Ashby splits it into department and team (e.g. 'Go To Market' / 'Marketing').

    I join both into one string, same as the flattened Greenhouse metadata, so
    the rule sees everything the board says about where the role sits.
    """
    parts = []
    for value in (job.get("department"), job.get("team")):
        value = (value or "").strip()
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)  # boards often set team == department; say it once
    return " ".join(parts) or None


def fetch_ashby_board(token):
    """Return the listed postings on one Ashby board, same shape as fetch_board."""
    data = json.loads(_get(ASHBY_API.format(token=token)).decode("utf-8"))
    postings = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:
            continue
        postings.append({
            "external_id": str(job["id"]),
            "title": (job.get("title") or "").strip(),
            "source_url": job.get("jobUrl", ""),
            "department": _ashby_department(job),
            "first_published": job.get("publishedAt"),
            "updated_at": None,
            "company_name": "",  # ashby's posting api doesn't include it
        })
    return postings


# --- lever, workable, careers pages -----------------------------------------------
# These talk to lots of different sites (every company's own careers page), so
# they send a plain browser user-agent, not the contact one above.

LEVER_API = "https://api.lever.co/v0/postings/{token}?mode=json"
WORKABLE_JOBS = "https://apply.workable.com/api/v3/accounts/{token}/jobs"
WORKABLE_ACCOUNT = "https://apply.workable.com/api/v1/accounts/{token}"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/join-us", "/company/careers")


def _web(url, data=None, headers=None, limit=3_000_000):
    """(status, final url, text) with a browser user-agent. Never raises."""
    hdrs = {"User-Agent": BROWSER_UA}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.geturl(), resp.read(limit).decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, url, ""
    except Exception:
        return None, url, ""


def _iso_ms(ms):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def fetch_lever_board(token):
    """Postings on jobs.lever.co/<token>, same shape as fetch_board."""
    status, _, text = _web(LEVER_API.format(token=token), limit=50_000_000)  # big boards are many MB
    if status != 200:
        raise FetchError(f"lever board {token}: http {status}")
    postings = []
    for job in json.loads(text or "[]"):
        cats = job.get("categories") or {}
        dept = " ".join(dict.fromkeys(v for v in (cats.get("department"), cats.get("team")) if v)) or None
        postings.append({
            "external_id": str(job["id"]),
            "title": (job.get("text") or "").strip(),
            "source_url": job.get("hostedUrl", ""),
            "department": dept,
            "location": cats.get("location"),
            "first_published": _iso_ms(job["createdAt"]) if job.get("createdAt") else None,
            "updated_at": None,
            "company_name": "",
        })
    return postings


def fetch_workable_board(token):
    """Postings on apply.workable.com/<token>. The job list is a POST, not a GET."""
    postings, page = [], None
    for _ in range(10):  # 10 pages is plenty for a startup
        body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if page:
            body["token"] = page
        status, _, text = _web(WORKABLE_JOBS.format(token=token), data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
        if status != 200:
            raise FetchError(f"workable board {token}: http {status}")
        data = json.loads(text or "{}")
        for job in data.get("results", []):
            loc = job.get("location") or {}
            postings.append({
                "external_id": str(job.get("shortcode") or job.get("id")),
                "title": (job.get("title") or "").strip(),
                "source_url": f"https://apply.workable.com/{token}/j/{job.get('shortcode')}/",
                "department": job.get("department") or None,
                "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
                "first_published": job.get("published"),
                "updated_at": None,
                "company_name": "",
            })
        page = data.get("nextPage")
        if not page:
            break
    return postings


ROLE_NOUN = re.compile(
    r"\b(engineer|developer|designer|manager|scientist|analyst|architect|researcher|specialist|executive|"
    r"strategist|lead|head|director|officer|recruiter|copywriter|writer|consultant|associate|intern|"
    r"coordinator|accountant|processor|underwriter|representative|administrator|marketer|advocate|"
    r"operator|technician|counsel|paralegal|controller|editor|producer|vp|president|cto|cfo|coo)s?\b", re.I)
NOT_A_ROLE = re.compile(r"view role|apply|careers?|blog|field notes|our team|meet the|join us|life at|"
                        r"benefits|perks|culture|privacy|cookie|terms|log ?in|sign ?up|contact|about|"
                        r"read more|learn more|see all|open positions|openings|all jobs|\bteam\b$", re.I)
CITY_TAIL = re.compile(r"\s+(bengaluru|bangalore|hyderabad|mumbai|delhi|gurugram|gurgaon|noida|pune|chennai|"
                       r"shillong|kolkata|ahmedabad|remote|hybrid|on-site|onsite|san francisco|new york|nyc|"
                       r"london|austin|seattle|boston|toronto|singapore|dubai|berlin|india|usa|us|uk)\b.*$", re.I)


def _clean_title(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip(" -–—|·•:")
    text = re.split(r"\s+[·•|–—]\s+|\s+-\s+", text)[0].strip()   # "Data Analyst Bengaluru · On-site" -> first part
    text = CITY_TAIL.sub("", text).strip(" ,-/")
    text = re.sub(r"^\d+[.)]?\s+", "", text)  # "05 Research Engineer" -> numbered lists
    return text


HEAD_FIRST = re.compile(r"^(?:senior |sr\.? |junior |founding |principal )?(?:head|vp|vice president|director|chief|lead|manager)\b", re.I)


def looks_like_job_title(title):
    """'Senior Backend Engineer/ Architect' and 'Head of Supply Chain' yes;
    'Stripe recruiters reach out through official channels' no. The role word
    has to be how the title ends (or starts, for 'Head of ...'), and it can't
    read like a sentence."""
    if title.endswith((".", "!", "?")):
        return False
    core = re.split(r"\s*[,(]\s*", title)[0]             # "Engineer, Backend (Remote)" -> "Engineer"
    last_two = " ".join(re.split(r"[\s/&]+", core)[-2:])
    return bool(ROLE_NOUN.search(last_two) or HEAD_FIRST.match(core))


def parse_careers_page(page, url):
    """Job titles from a company's own careers page. Works on plain HTML, Webflow,
    Wix and Next.js pages: role-like text in headings, links, list items and
    anything marked up as a job title."""
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page or "")
    chunks = re.findall(r"(?is)<(h[1-6]|a|li|button|p|span|div)\b[^>]*>(.{2,300}?)</\1>", page)
    chunks += [("x", m) for m in re.findall(
        r'(?is)<[a-z]+[^>]+class="[^"]*(?:job-?title|jobTitle|position-?title|role-?title|opening-?title|job-name)[^"]*"[^>]*>(.{2,300}?)</', page)]
    found, seen = [], set()
    for _, raw in chunks:
        title = _clean_title(raw)
        words = title.split()
        if not (1 < len(words) <= 9) or len(title) > 80:
            continue
        if not looks_like_job_title(title) or NOT_A_ROLE.search(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "external_id": re.sub(r"[^a-z0-9]+", "-", key).strip("-"),
            "title": title,
            "source_url": url,
            "department": None,
            "first_published": None,
            "updated_at": None,
            "company_name": "",
        })
    return found


def find_careers_page(domain):
    """(url, html) of the company's careers page, or (None, '').

    Tries /careers, /jobs and a few others, and keeps the one that lists the
    most roles - some sites have a /careers page about culture and the actual
    roles on /jobs (shamrockus.ai does)."""
    best = (None, "", -1)
    seen = set()
    for path in CAREERS_PATHS:
        status, final, text = _web(f"https://{domain}{path}")
        if status != 200 or not text or final in seen:
            continue
        seen.add(final)
        host = (urllib.parse.urlparse(final).hostname or "").lower().removeprefix("www.")
        if not host.endswith(domain.lower().removeprefix("www.")):
            continue  # redirected off their own site
        n = len(parse_careers_page(text, final))
        if n > best[2]:
            best = (final, text, n)
    return best[0], best[1]


def fetch_careers_page(domain):
    """Postings read off <domain>/careers (or /jobs). The page is on the company's
    own domain, so it can't belong to anyone else."""
    url, text = find_careers_page(domain)
    if not url:
        raise FetchError(f"no careers page on {domain}")
    return parse_careers_page(text, url)


FETCHERS = {"greenhouse": fetch_board, "ashby": fetch_ashby_board, "lever": fetch_lever_board,
            "workable": fetch_workable_board, "careers": fetch_careers_page}


def fetch_postings(source, token):
    """Fetch one board from whichever platform it's on."""
    if source not in FETCHERS:
        raise FetchError(f"unknown source: {source}")
    return FETCHERS[source](token)
