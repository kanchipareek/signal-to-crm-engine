"""Find leads for a seller, based on signals.

    python scripts/find_leads.py <seller domain>     e.g. python scripts/find_leads.py acme.com

1. works out who the seller sells to (config/sellers/<seller>.json - made by
   Gemini from their website the first time, or written by hand)
2. gathers companies that could be buyers:
     - every company that filed a Form D in the last --days days, filtered
       to the seller's buyers (industry, raise size, company type)
     - the companies in config/boards.csv
     - the example buyers in the seller profile
     - companies whose raise was in the news (worldwide - form d is us only)
3. checks each one for signals:
     - funding: a Form D or a news story in the window, with the amount
     - hiring: open jobs on greenhouse or ashby whose title matches the
       seller's buyer roles (e.g. "Head of SEO" for an SEO tool)
   then a fit check: gemini reads what each company does and says if it would
   actually buy from the seller. 'no' gets dropped (peers, competitors, wrong market).
4. writes a ranked lead list with the reasons and the people on the company's
   SEC filing (usually the founders and CEO) to leads/<seller>-<date>.md / .csv
"""
import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.run_detector import load_env
from src.fetch import FetchError, fetch_postings
from src.funding import classify, format_usd, load_rule
from src.evidence import PageCache, evidence_rows, verify_lead
from src.fit import check_fit
from src.leads import (find_board, find_domain, looks_like_fund, match_hires, name_core,
                       sec_by_name, score, site_exists, why)
from src.news import extract_raises, search_headlines
from src.people import find_people
from src.llm import LLMError
from src.sec import fetch_form_d, filing_url, list_form_d, search_recent_form_d
from src.seller import load_or_build, profile_path, slug

OUT_DIR = ROOT / "leads"


def log(msg):
    print(msg, flush=True)


def sec_people(people, url):
    """Form D's related persons in the same shape as everyone else: name, title, source."""
    return [{"name": p["name"], "title": ", ".join(p["roles"]) or "listed on the filing", "source": url}
            for p in people or []]


def funding_rule(profile, days):
    return {**load_rule(), "min_amount_usd": profile["min_raise_usd"], "lookback_days": days}


def fits_raise(amount, profile):
    return not profile.get("max_raise_usd") or amount <= profile["max_raise_usd"]


def funding_from(filing, cik, rule, profile):
    """A funding signal dict if this filing counts for this seller, else None."""
    label, _ = classify(filing, rule)
    if label != "hit" or not fits_raise(filing["total_sold"], profile):
        return None
    return {"amount": filing["total_sold"], "date": filing["filing_date"],
            "url": filing_url(cik, filing["accession"])}


# --- step 2: gather companies ------------------------------------------------------

def scan_form_d(profile, days, max_filings):
    """Companies that raised in the window and look like the seller's buyers."""
    end = date.today()
    start = end - timedelta(days=days)
    rows = search_recent_form_d(start.isoformat(), end.isoformat())
    log(f"  {len(rows)} form d filings from {start} to {end}")
    rows = [r for r in rows if not looks_like_fund(r["name"])]
    rows.sort(key=lambda r: r["filing_date"], reverse=True)
    if len(rows) > max_filings:
        log(f"  {len(rows)} after dropping funds/llcs, checking the newest {max_filings} (--max-filings)")
        rows = rows[:max_filings]
    else:
        log(f"  {len(rows)} after dropping funds/llcs")

    rule = funding_rule(profile, days)
    groups = set(profile["form_d_industry_groups"])
    by_cik = {}
    for i, r in enumerate(rows, 1):
        if i % 100 == 0:
            log(f"  ...read {i}/{len(rows)} filings")
        try:
            filing = {**fetch_form_d(r["cik"], r["accession"]), **r}
        except FetchError:
            continue
        if groups and filing["industry_group"] not in groups:
            continue
        if profile.get("startups_only") and filing.get("entity_type") not in (None, "Corporation"):
            continue
        funding = funding_from(filing, r["cik"], rule, profile)
        if not funding:
            continue
        old = by_cik.get(r["cik"])
        if old and old["funding"]["amount"] >= funding["amount"]:
            continue  # same company, two filings: keep the bigger one
        by_cik[r["cik"]] = {
            "name": filing["entity_name"] or r["name"], "cik": r["cik"], "domain": None,
            "location": r["location"], "funding": funding, "people": sec_people(filing["people"], funding["url"]),
            "industry_group": filing["industry_group"],
            "people_from": f"form d filed {r['filing_date']}", "found_via": "raised (sec form d)",
        }
    log(f"  {len(by_cik)} companies raised {format_usd(profile['min_raise_usd'])}+ in the seller's industries")
    return list(by_cik.values())


def from_news(profile, days):
    """Companies whose raise was reported in the news in the window."""
    headlines = search_headlines(profile, days)
    by_source = {}
    for h in headlines:
        by_source[h["source"]] = by_source.get(h["source"], 0) + 1
    log(f"  {len(headlines)} funding headlines ({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))})")
    if not headlines:
        return []
    raises = extract_raises(headlines)
    how = "gemini" if any(r["extracted_by"] == "gemini" for r in raises) else "rule-based"
    out = []
    for r in raises:
        # undisclosed amounts stay in (strategic and pe deals often have none);
        # stated amounts have to be in the seller's range
        if r["amount"] and (r["amount"] < profile["min_raise_usd"] or not fits_raise(r["amount"], profile)):
            continue
        out.append({
            "name": r["company"], "domain": None, "location": r["country"] or "",
            "what_they_do": r["what_they_do"],
            "funding": {"amount": r["amount"], "amount_text": r.get("amount_text"), "round": r.get("round"),
                        "date": r["date"], "url": r["url"], "sources": r.get("sources", [r["url"]]),
                        "source": "news", "headline": r["headline"], "extracted_by": r["extracted_by"]},
            "found_via": f"raised (in the news, {r.get('news_source') or 'news'})",
        })
    undisclosed = sum(1 for o in out if not o["funding"]["amount"])
    log(f"  {len(raises)} raises in the headlines ({how}), {len(out)} in the seller's range "
        f"({undisclosed} with the amount not disclosed)")
    return out


def from_lists(profile):
    out = []
    with (ROOT / "config" / "boards.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            board = (r["source"].strip(), r["board_token"].strip()) if r["board_token"].strip() else None
            out.append({"name": r["name"], "domain": r["domain"], "board": board,
                        "found_via": "your account list (boards.csv)"})
    for b in profile.get("example_buyers", []):
        if b.get("name") and b.get("domain"):
            out.append({"name": b["name"], "domain": b["domain"].lower().removeprefix("www."),
                        "found_via": "likely buyer from the seller profile", "verify_domain": True})
    return out


# --- step 3: signals -----------------------------------------------------------------

def enrich(c, profile, rule):
    """Fill in website, board, hires, and (if not already known) sec funding + people."""
    try:
        if c.get("verify_domain"):
            if not site_exists(c["domain"]):
                c["dropped"] = "website doesn't load (the suggestion may be wrong)"
                return c
        if not c.get("domain"):
            c["domain"] = find_domain(c["name"])
            c["domain_guessed"] = bool(c["domain"])
        if c.get("domain") and "board" not in c:
            c["board"] = find_board(c["name"], c["domain"])
        c["hires"] = []
        if c.get("board"):
            source, token = c["board"]
            try:
                c["hires"] = match_hires(fetch_postings(source, token), profile)
            except FetchError:
                pass
        if not c.get("cik"):
            match = sec_by_name(c["name"])
            if match:
                c["cik"], c["sec_name"] = match
                filings = list_form_d(c["cik"])
                originals = [f for f in filings if f["form"] == "D"]
                if originals:
                    latest = {**fetch_form_d(c["cik"], originals[0]["accession"]), **originals[0]}
                    c["people"] = sec_people(latest["people"], filing_url(c["cik"], latest["accession"]))
                    c["people_from"] = f"form d filed {latest['filing_date']} (matched by exact name: {c['sec_name']})"
                    if not c.get("funding"):  # don't overwrite a raise we already know from the news
                        c["funding"] = funding_from(latest, c["cik"], rule, profile)
    except Exception as exc:  # one bad company shouldn't stop the run
        c["error"] = f"{type(exc).__name__}: {exc}"
    return c


def add_people(lead):
    """No sec filing? Look for founders / ceo / coo in the raise article and the
    company's own about and team pages. Only names with a title next to them."""
    if lead.get("people"):
        return lead
    articles = (lead.get("funding") or {}).get("sources", [])
    try:
        lead["people"] = find_people(lead.get("domain"), articles)
    except Exception:
        lead["people"] = []
    if lead["people"]:
        lead["people_from"] = "their own site or the raise article"
    return lead


# --- step 4: report ------------------------------------------------------------------

def people_text(people):
    return "; ".join(f"{p['name']} ({p['title']})" for p in people or [])


def people_md(people):
    return "; ".join(f"{p['name']} ({p['title']}, [source]({p['source']}))" for p in people or [])


def write_report(profile, leads, days, rejected=()):
    OUT_DIR.mkdir(exist_ok=True)
    stem = OUT_DIR / f"{slug(profile['seller'])}-{date.today().isoformat()}"
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "fit", "company", "website", "location", "raised", "raised_on", "hiring",
                    "people", "why", "evidence", "found_via"])
        for l in leads:
            f = l.get("funding") or {}
            w.writerow([l["score"], l.get("fit", ""), l["name"], l.get("domain") or "", l.get("location") or "",
                        f.get("amount") or "", f.get("date") or "",
                        " | ".join(h["title"] for h in l["hires"]), people_text(l.get("people")),
                        " ".join(l["why"]),
                        " ; ".join(f"{k} | {d} | {u}" for k, d, u in evidence_rows(l)),
                        l["found_via"]])

    lines = [f"# leads for {profile['seller_name']} ({profile['seller']})", "",
             f"{date.today().isoformat()} · signals from the last {days} days · {len(leads)} leads", "",
             f"**what {profile['seller_name']} sells:** {profile['sells']}", "",
             f"**who buys it:** {profile['buyers']}", "",
             f"**hiring signals:** job titles with {', '.join(profile['hire_keywords'])}", "",
             f"**funding signals:** raised {format_usd(profile['min_raise_usd'])}"
             + (f" to {format_usd(profile['max_raise_usd'])}" if profile.get("max_raise_usd") else "+")
             + f" in {', '.join(profile['form_d_industry_groups']) or 'any industry'}", ""]
    for i, l in enumerate(leads, 1):
        lines.append(f"## {i}. {l['name']} · score {l['score']}")
        lines.append("")
        site = l.get("domain") or "website not found (search the name)"
        if l.get("domain_guessed"):
            site += " (guessed from the company name, worth a quick check)"
        lines.append(f"- **website:** {site}" + (f" · {l['location']}" if l.get("location") else ""))
        for reason in l["why"]:
            lines.append(f"- **why:** {reason}")
        if l.get("people"):
            lines.append(f"- **people:** {people_md(l['people'])}")
        else:
            lines.append("- **people:** not found on an sec filing, the raise article or their site. check linkedin")
        lines.append("- **evidence:**")
        for kind, day, url in evidence_rows(l)[:8]:
            lines.append(f"  - {kind} · {day} · {url}")
        lines.append(f"- **found via:** {l['found_via']}")
        lines.append("")
    if rejected:
        lines.append(f"## dropped by the fit check ({len(rejected)})")
        lines.append("")
        lines.append("these had a signal, but gemini didn't think they'd buy. in case it got one wrong:")
        lines.append("")
        for l in rejected:
            lines.append(f"- {l['name']} ({l.get('domain') or 'no website'}): {l.get('fit_reason') or 'no reason given'}")
        lines.append("")
    stem.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return stem


def main():
    ap = argparse.ArgumentParser(description="find signal-based leads for a seller")
    ap.add_argument("seller", help="the domain of the company you want leads for, e.g. acme.com")
    ap.add_argument("--days", type=int, default=90, help="how far back to look for funding (default 90)")
    ap.add_argument("--max-filings", type=int, default=1000, help="cap on form d filings to read, newest first (default 1000)")
    ap.add_argument("--no-scan", action="store_true", help="skip the form d scan, only check the lists")
    ap.add_argument("--refresh-profile", action="store_true", help="rebuild the seller profile from their site")
    ap.add_argument("--no-news", action="store_true", help="skip the google news funding search")
    ap.add_argument("--no-fit-check", action="store_true", help="keep every lead with a signal, don't ask gemini about fit")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    load_env()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # windows consoles choke on company names

    seller = args.seller.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
    log(f"1. seller profile for {seller}")
    try:
        profile, built = load_or_build(seller, refresh=args.refresh_profile)
    except (LLMError, ValueError) as exc:
        log(f"  ! {exc}")
        log(f"  write the profile by hand at {profile_path(seller).relative_to(ROOT)} "
            "(copy config/sellers/_template.json as a start) and run again.")
        raise SystemExit(1)
    log(f"  {'built with gemini and saved' if built else 'using saved'}: {profile_path(seller).relative_to(ROOT)}")
    log(f"  sells: {profile['sells']}")
    if "check it" in profile.get("made_by", ""):
        log("  ! their site blocked us, so gemini wrote this from what it already knows. give the profile a quick read.")

    log("2. gathering companies")
    cands = [] if args.no_scan else scan_form_d(profile, args.days, args.max_filings)
    if not args.no_news:
        try:
            have = {tuple(name_core(c["name"])) for c in cands}
            cands += [c for c in from_news(profile, args.days) if tuple(name_core(c["name"])) not in have]
        except LLMError as exc:
            log(f"  ! skipped the news search: {exc}")
    known = {c["name"].lower() for c in cands}
    for c in from_lists(profile):  # one entry per company, even if it's on both lists
        keys = {c["name"].lower(), (c.get("domain") or "").lower()} - {""}
        if not keys & known:
            cands.append(c)
            known |= keys
    cands = [c for c in cands if (c.get("domain") or "") != seller]
    log(f"  {len(cands)} companies to check")

    log("3. checking signals (websites, job boards, sec)")
    rule = funding_rule(profile, args.days)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = list(pool.map(lambda c: enrich(c, profile, rule), cands))

    with_signal = [c for c in done if not c.get("dropped") and (c.get("funding") or c.get("hires"))]
    rejected = []
    if with_signal and not args.no_fit_check:
        log(f"  fit check on {len(with_signal)} companies with a signal")
        try:
            failed = check_fit(with_signal, profile)
            if failed:
                log(f"  ! {failed} fit-check batches failed twice, those companies stay as 'maybe'")
            rejected = [c for c in with_signal if c["fit"] == "no"]
            with_signal = [c for c in with_signal if c["fit"] != "no"]
            log(f"  {len(rejected)} dropped as not a fit, {sum(c['fit'] == 'yes' for c in with_signal)} clear fits")
        except LLMError as exc:
            log(f"  ! fit check skipped: {exc}")
    log(f"  looking up founders / ceo for {sum(1 for c in with_signal if not c.get('people'))} leads without an sec filing")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        with_signal = list(pool.map(add_people, with_signal))

    # every job gets fetched again now: still 200, still listing the role, or it's not evidence
    cache = PageCache()
    jobs_before = sum(len(c.get("hires", [])) for c in with_signal)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        ok = list(pool.map(lambda c: verify_lead(c, cache), with_signal))
    no_evidence = [c for c, keep in zip(with_signal, ok) if not keep]
    with_signal = [c for c, keep in zip(with_signal, ok) if keep]
    jobs_dropped = sum(len(c.get("dropped_jobs", [])) for c in with_signal + no_evidence)
    log(f"  rechecked {jobs_before} job urls: {jobs_dropped} gone or no longer listing the role, "
        f"{len(no_evidence)} leads dropped for having no evidence left")
    leads = []
    for c in with_signal:
        c["score"] = score(c)
        c["why"] = why(c, profile, format_usd)
        leads.append(c)
    leads.sort(key=lambda l: (-l["score"], l["name"]))
    errors = [c for c in done if c.get("error")]
    dropped = [c for c in done if c.get("dropped")]

    stem = write_report(profile, leads, args.days, rejected)
    log(f"4. {len(leads)} leads -> {stem.relative_to(ROOT)}.md / .csv")
    if dropped:
        log(f"  dropped {len(dropped)} suggested buyers whose website didn't load")
    if errors:
        log(f"  {len(errors)} companies hit an error and were skipped")
    for l in leads[:15]:
        bits = []
        if l.get("funding"):
            amt = l["funding"].get("amount")
            bits.append(f"raised {format_usd(amt) if amt else '?'}" + (" (news)" if l["funding"].get("source") == "news" else ""))
        if l["hires"]:
            bits.append(f"hiring {l['hires'][0]['title']}" + (f" +{len(l['hires']) - 1}" if len(l["hires"]) > 1 else ""))
        who = l["people"][0]["name"] if l.get("people") else "-"
        log(f"  {l['score']:2} {l.get('fit', '-'):5} {l['name'][:26]:26} {(l.get('domain') or '?')[:22]:22} {', '.join(bits)[:60]:60} {who}")


if __name__ == "__main__":
    main()
