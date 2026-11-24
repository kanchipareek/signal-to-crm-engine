"""A tiny Gemini client: send a prompt, get JSON back. Standard library only.

I only use an LLM for one step: reading a seller's website and writing down
who they sell to. Everything else in the project is rules and public data.

Needs GEMINI_API_KEY (free from Google AI Studio). The model can be changed
with GEMINI_MODEL. On the free tier Google may use what you send to improve
its products - here that's only text from a public website, so I'm fine with it.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-3.5-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 90
BUSY_CODES = {429, 500, 503, 504}
RETRY_WAIT = 5


class LLMError(Exception):
    pass


def _extract_json(text):
    """The reply should be pure JSON, but strip ``` fences just in case."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model didn't return valid json: {exc}; got: {text[:300]}")


class Busy(LLMError):
    """The model is overloaded or rate-limited. Worth retrying, or trying another model."""


def _call(prompt, model, api_key):
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(ENDPOINT.format(model=model), data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in BUSY_CODES:
            raise Busy(f"{model} is busy (http {exc.code})")
        raise LLMError(f"gemini http {exc.code}: {detail}")
    except TimeoutError:
        raise Busy(f"{model} timed out")
    except Exception as exc:
        if "timed out" in str(exc):
            raise Busy(f"{model} timed out")
        raise LLMError(f"gemini request failed: {type(exc).__name__}")


def ask_json(prompt, model=None, api_key=None):
    """Send one prompt and return the parsed JSON reply.

    On the free tier the bigger model is often overloaded (503) or slow, so a
    busy model gets one retry, then the request falls back to flash-lite.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("GEMINI_API_KEY isn't set. Add it to .env, or write the seller profile by hand.")
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    models = list(dict.fromkeys([model, FALLBACK_MODEL]))
    data, last = None, None
    for m in models:
        for attempt in range(2):
            try:
                data = _call(prompt, m, api_key)
                break
            except Busy as exc:
                last = exc
                if attempt == 0:
                    time.sleep(RETRY_WAIT)
        if data is not None:
            break
    if data is None:
        raise LLMError(f"gemini is busy, tried {', '.join(models)}: {last}. try again in a few minutes.")
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected gemini reply: {json.dumps(data)[:400]}")
    return _extract_json(text)
