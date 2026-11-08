
"""Deliver pending GTM leadership signals to n8n."""
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


def classify_signal(title):
    """Classify leadership hires using conservative title-based rules."""
    title = " ".join((title or "").lower().split())

    if any(term in title for term in (
        "regional", "area vice president", "area vp", "regional head"
    )):
        return "review"

    if "marketing" in title:
        return "marketing_leader"

    if "sales" in title or "revenue" in title:
        return "revenue_leader"

    return "review"


def is_transient(status_code, error):
    if status_code is None:
        return True
    return status_code in TRANSIENT_CODES or 500 <= status_code < 600


def build_event(row):
    (
        signal_id, company_id, company_name, domain, source,
        board_token, external_id, source_url, title,
        department, first_published
    ) = row

    return {
        "event": EVENT,
        "signal_id": signal_id,
        "company_id": company_id,
        "company_name": company_name,
        "domain": domain,
        "signal_type": classify_signal(title),
        "signal_title": title,
        "signal_url": source_url,
        "signal_key": (
            f"{source}:{board_token}:{external_id}:{EVENT}"
        ),
        "source": source,
        "department": department,
        "first_published": first_published,
        "detected_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def build_request(url, secret, body, idempotency_key):
    return urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-gtm-secret": secret,
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


def deliver_pending(conn, url, secret, max_attempts=3, backoff=1.0):
    rows = conn.execute("""
        SELECT
            s.id,
            s.company_id,
            c.name,
            c.domain,
            s.source,
            s.board_token,
            s.external_id,
            s.source_url,
            s.title,
            s.department,
            s.first_published
        FROM signals s
        JOIN companies c ON c.id = s.company_id
        WHERE s.delivery_status IN ('pending', 'failed')
        ORDER BY s.id
    """).fetchall()

    delivered = failed = 0

    for row in rows:
        event = build_event(row)
        body = json.dumps(event).encode("utf-8")
        key = event["signal_key"]
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
                "INSERT INTO signal_deliveries "
                "(signal_id, attempt_no, attempted_at, ok, status_code, error) "
                "VALUES (?,?,?,?,?,?)",
                (
                    signal_id,
                    attempt,
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    1 if ok else 0,
                    status_code,
                    error,
                ),
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
                "UPDATE signals SET delivery_status='delivered', "
                "attempts=?, last_error=NULL, next_retry_at=NULL WHERE id=?",
                (attempt, signal_id),
            )
            delivered += 1
            print(f"  delivered signal {signal_id}")

        elif permanent:
            conn.execute(
                "UPDATE signals SET delivery_status='dead', "
                "attempts=?, last_error=?, next_retry_at=NULL WHERE id=?",
                (attempt, error, signal_id),
            )
            failed += 1
            print(f"  dead-lettered signal {signal_id}: {error}")

        else:
            retry_at = (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(timespec="seconds")

            conn.execute(
                "UPDATE signals SET delivery_status='failed', "
                "attempts=?, last_error=?, next_retry_at=? WHERE id=?",
                (attempt, error, retry_at, signal_id),
            )
            failed += 1
            print(f"  failed signal {signal_id}: {error}")

        conn.commit()

    return delivered, failed


def main():
    load_env()

    url = os.environ.get("WEBHOOK_URL")
    secret = os.environ.get("WEBHOOK_SECRET")
    db_path = os.environ.get("LEAD_DB_PATH")

    if not url or not secret or not db_path:
        print("Set WEBHOOK_URL, WEBHOOK_SECRET and LEAD_DB_PATH in .env.")
        raise SystemExit(1)

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        raise SystemExit(1)

    conn = sqlite3.connect(db_path)

    try:
        delivered, failed = deliver_pending(conn, url, secret)
    finally:
        conn.close()

    print(f"\ndelivered {delivered} | failed {failed}")


if __name__ == "__main__":
    main()
