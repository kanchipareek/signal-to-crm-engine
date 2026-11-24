"""Sends seven test signals to the n8n webhook and checks what comes back.

These are TEST records with fake .example domains, not real companies.
They prove the routing rule before any real signal touches the CRM:

  1. revenue leader at a new company   -> created (updated on re-runs), HIGH task
  2. marketing leader at a company that already has an owner -> owner kept
  3. regional head                     -> review queue (triage owner)
  4. the same signal as #1 again       -> duplicate, no second task
  5. tampered signature                -> 401
  6. missing company_domain            -> 400
  7. money raised (SEC Form D)         -> funding_round, MEDIUM task

Run:  python scripts/send_test_signals.py
Needs WEBHOOK_URL and WEBHOOK_SECRET in .env (WEBHOOK_URL = the n8n URL).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.deliver import load_env, sign  # same signing code the real sender uses


RUN = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # new ids every run, so you can re-run safely


def event(n, company, domain, title, kind="gtm_leadership_hire", amount=None):
    return {
        "event": kind,
        "amount_usd": amount,
        "signal_id": 9000 + n,
        "company_id": 9000 + n,
        "company_name": company,
        "company_domain": domain,
        "source": "greenhouse",
        "board_token": "test-board",
        "external_id": f"test-{n}-{RUN}",
        "source_url": f"https://example.com/jobs/test-{n}",
        "title": title,
        "department": None,
        "first_published": datetime.now(timezone.utc).date().isoformat(),
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def send(url, secret, payload, tamper=False):
    body = json.dumps(payload).encode()
    sig = sign(secret, body)
    if tamper:
        sig = "0" * len(sig)
    key = f"{payload['source']}:{payload['board_token']}:{payload['external_id']}:{payload['event']}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Signature": sig, "X-Idempotency-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"raw": text[:200]}


def main():
    load_env()
    url = os.environ.get("WEBHOOK_URL")
    secret = os.environ.get("WEBHOOK_SECRET")
    if not url or not secret:
        print("Set WEBHOOK_URL and WEBHOOK_SECRET in .env first.")
        raise SystemExit(1)

    t1 = event(1, "TEST Revenue Leader Co", "test-revenue-leader.example", "VP of Sales")
    t2 = event(2, "TEST Existing Co", "test-existing-co.example", "Head of Growth")
    t3 = event(3, "TEST Regional Co", "test-regional.example", "Head of Sales, ANZ")
    t6 = event(6, "TEST No Domain Co", "", "CMO")
    t7 = event(7, "TEST Funded Co", "test-funded.example", "Raised $25M (SEC Form D)",
               kind="money_raised", amount=25_000_000)

    cases = [
        ("1 revenue leader, new company", t1, False, lambda s, r: s == 200 and r.get("status") in ("created", "updated") and r.get("signal_type") == "revenue_leader"),
        ("2 marketing leader, existing owner", t2, False, lambda s, r: s == 200 and r.get("signal_type") == "marketing_leader" and "Kept the owner" in r.get("routing_note", "")),
        ("3 regional head -> review", t3, False, lambda s, r: s == 200 and r.get("signal_type") == "review"),
        ("4 replay of #1 -> duplicate", t1, False, lambda s, r: s == 200 and r.get("status") == "duplicate"),
        ("5 tampered signature -> 401", t1, True, lambda s, r: s == 401),
        ("6 missing domain -> 400", t6, False, lambda s, r: s == 400),
        ("7 money raised -> funding_round", t7, False, lambda s, r: s == 200 and r.get("signal_type") == "funding_round"),
    ]
    # HubSpot's search index takes a few seconds to see a new company.
    # Without a pause, the replay test could create a second copy.
    pause = float(os.environ.get("TEST_PAUSE_SECONDS", "12"))
    passed = 0
    for i, (name, payload, tamper, check) in enumerate(cases):
        if i:
            time.sleep(pause)
        status, resp = send(url, secret, payload, tamper)
        ok = check(status, resp)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {status} {json.dumps(resp)}")
    print(f"\n{passed}/{len(cases)} passed")
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
