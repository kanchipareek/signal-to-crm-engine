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


def _get(url, attempts=3):
    delay = 1.0
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            time.sleep(PAUSE)
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


def parse_form_d(xml_bytes):
    """Pull the fields the rule needs out of a Form D primary_doc.xml."""
    root = ET.fromstring(xml_bytes)
    od = "offeringData/"
    return {
        "entity_name": _text(root, "primaryIssuer/entityName"),
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
