import json
import os
import urllib.request
import urllib.error
from pathlib import Path

# Load existing .env without displaying secrets
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

url = os.environ["WEBHOOK_URL"]
secret = os.environ["WEBHOOK_SECRET"]

# Deliberately missing signal_title
payload = {
    "company_name": "Test Company",
    "domain": "example.com",
    "signal_type": "revenue_leader",
    "signal_url": "https://example.com/jobs/test",
    "signal_key": "manual-invalid-test-001"
}

request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
        "Content-Type": "application/json",
        "x-gtm-secret": secret,
        "X-Idempotency-Key": "manual-invalid-test-001"
    }
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("HTTP:", response.status)
        print("Response:", response.read().decode("utf-8"))
except urllib.error.HTTPError as error:
    print("HTTP:", error.code)
    print("Response:", error.read().decode("utf-8"))