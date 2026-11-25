"""Records the real feeds, job boards and company pages for the acceptance tests.

    python scripts/capture_acceptance.py

For each acceptance seller it runs the real lead finder once, live, with no
gemini key (so every step is the rule-based one and the tests are repeatable),
and records every http response it gets. The tests then replay those
responses, so they run offline and give the same answer every time.

What's kept, per seller, in tests/fixtures/acceptance/<seller>.json.gz:
  - the funding headlines from the live news search (google news us + india,
    inc42, entrackr, yourstory) - the parsed items, not the raw feeds
  - the candidate companies the test runs on: the expected leads plus a few
    other real raises from the same headlines, so there's noise to filter
  - every response the pipeline fetched for them: job boards, careers pages,
    about pages, raise articles, sec lookups. html loses its <script>/<style>
    and job json loses its long descriptions, to keep the files small.
"""
import gzip
import io
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "tests" / "fixtures" / "acceptance"
DAYS = 90
NOISE = 6  # other real raises per seller, besides the expected ones

CASES = {
    "rightfit.so": ["different", "firstwork", "shamrock", "humyn"],
    "unsiloed.ai": ["elio", "trovy", "tomo", "aurenity", "alpine specialty"],
}
ATS = re.compile(r"ashbyhq\.com|lever\.co|workable\.com|greenhouse\.io")
LONG_FIELDS = {"descriptionHtml", "descriptionPlain", "description", "descriptionBody", "descriptionBodyPlain",
               "lists", "additional", "additionalPlain", "opening", "openingPlain", "compensation"}


def request_key(req):
    url = req.full_url if hasattr(req, "full_url") else req
    data = getattr(req, "data", None)
    return ("POST " if data else "GET ") + url + (" " + data.decode("utf-8", "ignore") if data else "")


def trim(body, content_type):
    text = body.decode("utf-8", "ignore")
    if "json" in content_type or text.lstrip()[:1] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            return text
        def strip(x):
            if isinstance(x, dict):
                return {k: strip(v) for k, v in x.items() if k not in LONG_FIELDS}
            if isinstance(x, list):
                return [strip(v) for v in x]
            return x
        return json.dumps(strip(data))
    # drop styles and svgs, and scripts - except scripts that link a job board,
    # because some careers pages only link their ashby/lever board from a script
    text = re.sub(r"(?is)<(style|noscript|svg)\b[^>]*>.*?</\1>", "", text)
    return re.sub(r"(?is)<script\b[^>]*>.*?</script>",
                  lambda m: m.group(0) if ATS.search(m.group(0)) else "", text)


class Recorder:
    def __init__(self):
        self.real = urllib.request.urlopen
        self.responses = {}
        self.lock = threading.Lock()
        self.on = True

    def __call__(self, req, *a, **kw):
        key = request_key(req)
        try:
            resp = self.real(req, *a, **kw)
        except urllib.error.HTTPError as exc:
            if self.on:
                with self.lock:
                    self.responses[key] = {"status": exc.code, "url": exc.geturl() or key.split(" ")[1], "body": ""}
            raise
        except Exception:
            if self.on:
                with self.lock:
                    self.responses[key] = {"status": None, "url": key.split(" ")[1], "body": ""}
            raise
        body = resp.read()
        if self.on:
            with self.lock:
                self.responses[key] = {"status": resp.status, "url": resp.geturl(),
                                       "body": trim(body, resp.headers.get("Content-Type", ""))}
        return FakeResponse(resp.status, resp.geturl(), body)


class FakeResponse(io.BytesIO):
    def __init__(self, status, url, body):
        super().__init__(body)
        self.status = status
        self._url = url
        self.headers = {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main():
    os.environ.pop("GEMINI_API_KEY", None)   # rule-based everywhere, so replays are deterministic
    import scripts.find_leads as fl
    import src.news as news
    from src.seller import load_or_build

    OUT.mkdir(parents=True, exist_ok=True)
    rec = Recorder()
    urllib.request.urlopen = rec
    for seller, expected in CASES.items():
        rec.responses = {}
        profile, _ = load_or_build(seller)
        print(f"== {seller}: news search (not recorded, the parsed headlines are saved instead)")
        rec.on = False
        headlines = news.search_headlines(profile, DAYS)
        rec.on = True
        real_search = fl.search_headlines   # find_leads imported it by name, so swap it there
        fl.search_headlines = lambda p, d: headlines
        try:
            cands = fl.from_news(profile, DAYS)
        finally:
            fl.search_headlines = real_search
        def is_expected(name, e):   # whole words: "tomo" isn't "RentoMojo", "elio" isn't "Chamelio"
            return re.search(rf"\b{re.escape(e)}\b", name.lower()) is not None
        picked = [c for c in cands if any(is_expected(c["name"], e) for e in expected)]
        others = [c for c in cands if c not in picked][:NOISE]
        # expected leads that the news doesn't find (no raise in the window) go in
        # the way a real run would see them: from an account list, by domain
        missing = [e for e in expected if not any(is_expected(c["name"], e) for c in picked)]
        listed = {"trovy": ("Trovy", "trovy.com"), "tomo": ("Tomo", "tomo.com")}
        extra = [{"name": listed[m][0], "domain": listed[m][1], "found_via": "account list"} for m in missing if m in listed]
        subset = picked + others + extra
        names = [c["name"] for c in subset]
        print(f"   {len(headlines)} headlines, {len(cands)} raises in range; running on {names}")
        import copy
        fl.process(copy.deepcopy(subset), profile, DAYS, workers=4)
        fixture = {"seller": seller, "captured": date.today().isoformat(), "days": DAYS,
                   "headlines": headlines, "candidates": names, "listed": extra,
                   "responses": rec.responses}
        path = OUT / f"{seller.replace('.', '-')}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(fixture, fh)
        print(f"   saved {len(rec.responses)} responses -> {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")
    urllib.request.urlopen = rec.real


if __name__ == "__main__":
    main()
