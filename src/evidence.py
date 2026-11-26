"""Evidence: every lead has to show its work, checked at run time.

For each lead, every signal needs a date and a source url:
  - funding: the filing or the news story (with every outlet's url)
  - jobs: the posting url, fetched again at the end of the run. It has to
    return 200 and still show the role's title, or the job is dropped. A
    posting that was taken down between the board fetch and now is not evidence.

A lead with nothing left after this is dropped.
"""
import html
import re
from datetime import datetime, timezone

from src.fetch import _web


def _norm(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def page_lists(title, page):
    """Does the page still show this role? Compared without punctuation or case."""
    want = _norm(title)
    return bool(want) and want in _norm(page)


class PageCache:
    """One fetch per url - a careers page lists many roles."""
    def __init__(self, web=None):
        self.web = web or _web
        self.pages = {}

    def get(self, url):
        if url not in self.pages:
            status, _, page = self.web(url)
            self.pages[url] = (status, page)
        return self.pages[url]


def verify_jobs(hires, cache):
    """(kept, dropped). Kept jobs get 'checked_at'; dropped ones get 'drop_reason'."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kept, dropped = [], []
    for h in hires:
        url = h.get("source_url")
        if not url:
            dropped.append({**h, "drop_reason": "no url"})
            continue
        status, page = cache.get(url)
        if status != 200:
            dropped.append({**h, "drop_reason": f"url returned {status}"})
        elif not page_lists(h["title"], page):
            dropped.append({**h, "drop_reason": "role no longer listed on the page"})
        else:
            kept.append({**h, "checked_at": now})
    return kept, dropped


def funding_evidence_ok(funding):
    return bool(funding and funding.get("date") and (funding.get("url") or funding.get("sources")))


def verify_lead(lead, cache):
    """Recheck a lead's evidence in place. Returns False if nothing is left."""
    lead["hires"], lead["dropped_jobs"] = verify_jobs(lead.get("hires", []), cache)
    if lead.get("funding") and not funding_evidence_ok(lead["funding"]):
        lead["dropped_funding"] = lead.pop("funding")
    lead["people"] = [p for p in lead.get("people", []) if p.get("source")]
    return bool(lead.get("funding") or lead["hires"])


def evidence_rows(lead):
    """[(signal, date, source url)] for the report."""
    rows = []
    for f in [lead.get("funding")] + lead.get("other_raises", []):
        if not f:
            continue
        kind = "raise (sec form d)" if f.get("source") != "news" else f"raise ({f.get('round') or 'round'}, news)"
        for url in (f.get("sources") or [f.get("url")])[:4]:
            rows.append((kind, f.get("date", ""), url))
    for h in lead.get("hires", []):
        label = "ops-pain hire" if h.get("kind") == "ops_pain" else "team hire"
        date = (h.get("first_published") or "")[:10] or f"open on {h.get('checked_at', '')[:10]}"
        rows.append((f"{label}: {h['title']}", date, h["source_url"]))
    return rows
