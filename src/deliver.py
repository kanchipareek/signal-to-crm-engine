"""Delivers pending signals to the webhook.

Picks up signals that haven't been delivered, signs each one, POSTs it with
bounded retries, and records the result.

Retries only make sense for TRANSIENT failures (network errors, 5xx, 429). A
permanent 4xx stops immediately and the signal is dead-lettered, so it isn't
retried forever. Delivery state lives on the signal row, so a crash mid-run
just means the next run picks up where it left off.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIMEOUT = 10
EVENT = "gtm_leadership_hire"
TRANSIENT_CODES = {429, 500, 502, 503, 504}


def load_env(path=".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_transient(status_code, error):
    """A failure worth retrying: no response, a 5xx, or a rate-limit."""
    if status_code is None:
        return True
    return status_code in TRANSIENT_CODES or 500 <= status_code < 600


def build_event(row):
    """Turn a signals row into the webhook payload."""
    (signal_id, company_id, company_name, company_domain, source, board_token,
     external_id, source_url, title, department, first_published) = row
    return {
        "event": EVENT,
        "signal_id": signal_id,
        "company_id": company_id,
        "company_name": company_name,
        "company_domain": company_domain,
        "source": source,
        "board_token": board_token,
        "external_id": external_id,
        "source_url": source_url,
        "title": title,
        "department": department,
        "first_published": first_published,
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_request(url, secret, body, idempotency_key):
    """The signed HTTP request - kept separate so it can be inspected in tests."""
    return urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Signature": sign(secret, body),
            "X-Idempotency-Key": idempotency_key,
            "User-Agent": "gtm-signal-detector/0.1",
        },
    )


def post(url, secret, body, idempotency_key):
    req = build_request(url, secret, body, idempotency_key)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, f"http {exc.code}"
    except Exception as exc:
        return None, type(exc).__name__


def deliver_pending(conn, url, secret, max_attempts=3, backoff=1.0, pause=0.0):
    rows = conn.execute("""
        SELECT s.id, s.company_id, c.name, c.domain, s.source, s.board_token, s.external_id,
               s.source_url, s.title, s.department, s.first_published
        FROM signals s JOIN companies c ON c.id = s.company_id
        WHERE s.delivery_status IN ('pending', 'failed')
        ORDER BY s.id
    """).fetchall()

    delivered = failed = 0
    for i, row in enumerate(rows):
        if pause and i:
            time.sleep(pause)  # give the CRM's search index a moment between signals
        event = build_event(row)
        body = json.dumps(event).encode()
        key = f"{event['source']}:{event['board_token']}:{event['external_id']}:{event['event']}"
        signal_id = event["signal_id"]

        ok = False
        permanent = False
        attempt = 0
        status_code = None
        error = None
        while attempt < max_attempts:
            attempt += 1
            status_code, error = post(url, secret, body, key)
            ok = bool(status_code and 200 <= status_code < 300)
            conn.execute(
                "INSERT INTO signal_deliveries (signal_id, attempt_no, attempted_at, ok, status_code, error) "
                "VALUES (?,?,?,?,?,?)",
                (signal_id, attempt, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 1 if ok else 0, status_code, error),
            )
            conn.commit()
            if ok:
                break
            if not is_transient(status_code, error):
                permanent = True
                break
            if attempt < max_attempts:
                time.sleep(backoff * attempt)

        if ok:
            conn.execute(
                "UPDATE signals SET delivery_status='delivered', attempts=?, last_error=NULL, next_retry_at=NULL WHERE id=?",
                (attempt, signal_id),
            )
            delivered += 1
            print(f"  delivered signal {signal_id}")
        elif permanent:
            conn.execute(
                "UPDATE signals SET delivery_status='dead', attempts=?, last_error=?, next_retry_at=NULL WHERE id=?",
                (attempt, error, signal_id),
            )
            failed += 1
            print(f"  dead-lettered signal {signal_id}: {error} (permanent, not retried)")
        else:
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE signals SET delivery_status='failed', attempts=?, last_error=?, next_retry_at=? WHERE id=?",
                (attempt, error, retry_at, signal_id),
            )
            failed += 1
            print(f"  failed signal {signal_id}: {error} (will retry)")
        conn.commit()

    return delivered, failed


def main():
    load_env()
    url = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:8000/webhook")
    secret = os.environ.get("WEBHOOK_SECRET")
    db_path = os.environ.get("LEAD_DB_PATH")
    if not secret or not db_path:
        print("Set WEBHOOK_SECRET and LEAD_DB_PATH in .env first.")
        raise SystemExit(1)
    conn = sqlite3.connect(db_path)
    pause = float(os.environ.get("DELIVER_PAUSE_SECONDS", "0"))
    delivered, failed = deliver_pending(conn, url, secret, pause=pause)
    conn.close()
    print(f"\ndelivered {delivered} | failed {failed}")


if __name__ == "__main__":
    main()
