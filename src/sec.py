"""Talks to SEC EDGAR and returns a company's Form D filings.

A Form D is the notice a US company files within 15 days of first selling
securities in a private round. It's public, free and needs no key, which makes
it the "money raised" equivalent of a public Greenhouse board.

One job: given a CIK (SEC's company id), list its Form D filings and parse the
amounts out of each one. No database, no rule - just fetching and parsing.

SEC asks automated clients to send a User-Agent with contact details and to
stay under 10 requests a second, so every call reuses fetch.py's UA and pauses.
"""
import json
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from src.fetch import UA, FetchError

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_DIR = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
TIMEOUT = 10
PAUSE = 0.2  # keeps us well under SEC's 10 requests/second
FORM_D_TYPES = ("D", "D/A")


_lock = threading.Lock()
_last_call = [0.0]


def _wait_turn():
    """Space calls PAUSE apart even when several threads share this module."""
    with _lock:
        wait = _last_call[0] + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _get(url, attempts=3):
    delay = 1.0
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        _wait_turn()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FetchError(f"not found: {url}")
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


def filing_url(cik, accession):
    return FILING_DIR.format(cik=int(cik), acc=accession.replace("-", ""))


def list_form_d(cik):
    """Every Form D / D/A in the company's recent filings, newest first."""
    data = json.loads(_get(SUBMISSIONS.format(cik=int(cik))))
    recent = data.get("filings", {}).get("recent", {})
    out = []
    for form, filed, acc in zip(recent.get("form", []), recent.get("filingDate", []),
                                recent.get("accessionNumber", [])):
        if form in FORM_D_TYPES:
            out.append({"form": form, "filing_date": filed, "accession": acc})
    return out


def _amount(text):
    """'25000000' -> 25000000. 'Indefinite', blank or junk -> None."""
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _text(root, path):
    node = root.find(path)
    return node.text.strip() if node is not None and node.text else None


def _people(root):
    """The executive officers, directors and promoters listed on the filing.

    Form D doesn't have a 'founder' box, but for a startup the executive
    officers are almost always the founders and the CEO.
    """
    people = []
    for person in root.findall("relatedPersonsList/relatedPersonInfo"):
        first = _text(person, "relatedPersonName/firstName") or ""
        last = _text(person, "relatedPersonName/lastName") or ""
        name = " ".join(p for p in (first, last) if p and p != "-").strip()
        roles = [r.text.strip() for r in person.findall("relatedPersonRelationshipList/relationship") if r.text]
        note = _text(person, "relationshipClarification")
        if name:
            people.append({"name": name, "roles": roles, "note": note})
    return people


def parse_form_d(xml_bytes):
    """Pull the fields the rule needs out of a Form D primary_doc.xml."""
    root = ET.fromstring(xml_bytes)
    od = "offeringData/"
    return {
        "entity_name": _text(root, "primaryIssuer/entityName"),
        "entity_type": _text(root, "primaryIssuer/entityType"),
        "city": _text(root, "primaryIssuer/issuerAddress/city"),
        "state": _text(root, "primaryIssuer/issuerAddress/stateOrCountry"),
        "incorporated_within_5_years": (_text(root, "primaryIssuer/yearOfInc/withinFiveYears") or "").lower() == "true",
        "people": _people(root),
        "submission_type": _text(root, "submissionType"),
        "industry_group": _text(root, od + "industryGroup/industryGroupType"),
        "is_amendment": (_text(root, od + "typeOfFiling/newOrAmendment/isAmendment") or "").lower() == "true",
        "date_of_first_sale": _text(root, od + "typeOfFiling/dateOfFirstSale/value"),
        "business_combination": (_text(root, od + "businessCombinationTransaction/isBusinessCombinationTransaction") or "").lower() == "true",
        "total_offering": _amount(_text(root, od + "offeringSalesAmounts/totalOfferingAmount")),
        "total_sold": _amount(_text(root, od + "offeringSalesAmounts/totalAmountSold")) or 0,
    }


def fetch_form_d(cik, accession):
    """Fetch and parse one filing."""
    return parse_form_d(_get(filing_url(cik, accession) + "primary_doc.xml"))


SEARCH = ("https://efts.sec.gov/LATEST/search-index?forms=D&dateRange=custom"
          "&startdt={start}&enddt={end}&from={offset}")
TYPEAHEAD = "https://efts.sec.gov/LATEST/search-index?keysTyped={q}"


def search_recent_form_d(start, end, limit=None):
    """Every Form D filed between two dates (YYYY-MM-DD), from EDGAR's search.

    Returns light rows (cik, accession, name, filing date, location) - the
    filing itself still has to be fetched for amounts and people. Only original
    filings ('D'), not amendments.
    """
    rows, offset = [], 0
    while True:
        data = json.loads(_get(SEARCH.format(start=start, end=end, offset=offset)))
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            src = hit["_source"]
            if src.get("form") != "D":
                continue
            name = (src.get("display_names") or [""])[0]
            name = name.split("  (CIK")[0].strip()
            rows.append({
                "cik": str(int(src["ciks"][0])),
                "accession": src["adsh"],
                "name": name,
                "filing_date": src.get("file_date"),
                "location": (src.get("biz_locations") or [""])[0],
            })
        offset += len(hits)
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        if offset >= total or (limit and len(rows) >= limit):
            break
    return rows[:limit] if limit else rows


def lookup_entities(name):
    """EDGAR's company-name typeahead: [(cik, entity name), ...]."""
    import urllib.parse
    data = json.loads(_get(TYPEAHEAD.format(q=urllib.parse.quote(name))))
    return [(str(int(h["_id"])), h["_source"].get("entity", ""))
            for h in data.get("hits", {}).get("hits", [])]
