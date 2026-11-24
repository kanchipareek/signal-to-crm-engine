"""Talks to the job boards and returns the open postings for a board.

One job: given a source ('greenhouse' or 'ashby') and a board token, ask that
board for the company's jobs and return them as a clean list of dicts, in the
same shape whatever the source. No database, no matching - just fetching.
"""
import json
import time
import urllib.error
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


FETCHERS = {"greenhouse": fetch_board, "ashby": fetch_ashby_board}


def fetch_postings(source, token):
    """Fetch one board from whichever platform it's on."""
    if source not in FETCHERS:
        raise FetchError(f"unknown source: {source}")
    return FETCHERS[source](token)
