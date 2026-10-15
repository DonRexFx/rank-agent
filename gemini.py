"""Minimal Google Gemini client over REST (no SDK needed). Free tier works with a key from
https://aistudio.google.com/apikey. Supports Google Search grounding.

Error handling: Google's own status code and message are kept (with secrets removed). Temporary
rate limits are retried, honouring Retry-After / RetryInfo, only when a short wait can help.
Exhausted daily/monthly quotas, bad models and bad keys are never retried. Every HTTP attempt
reserves a slot in budget.py first, so retries cannot exceed the configured call budget."""
import os
import re
import time
from email.utils import parsedate_to_datetime

import requests

import budget

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _env_num(name, default, cast):
    try:
        return max(0, cast(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_RETRIES = _env_num("GEMINI_MAX_RETRIES", 2, int)
MAX_WAIT = _env_num("GEMINI_MAX_RETRY_WAIT_SECONDS", 30, float)

_SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{10,}|(?i:key=)[^&\s\"']+|(?i:bearer)\s+[0-9A-Za-z._\-]+)")


def _clean(text):
    """Remove anything that looks like an API key from text that may be shown to users or logged."""
    t = str(text)
    key = os.environ.get("GEMINI_API_KEY", "")
    if len(key) >= 6:
        t = t.replace(key, "[hidden]")
    return _SECRET.sub("[hidden]", t)


class GeminiError(Exception):
    """kind: rate_limit | quota_exhausted | model_unavailable | auth | bad_request | server |
    network | blocked | empty | budget | error"""

    def __init__(self, message, status=None, kind="error", retry_after=None, google_status=None):
        super().__init__(message)
        self.status, self.kind, self.retry_after, self.google_status = status, kind, retry_after, google_status

    @property
    def retryable(self):
        return self.kind in ("rate_limit", "server", "network")

    @property
    def abort_run(self):
        """True when sending more questions in the same run is pointless right now."""
        return self.kind in ("quota_exhausted", "budget", "auth", "model_unavailable", "rate_limit")


def _seconds(value):
    if value is None:
        return None
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*s?\s*$", str(value))
    return float(m.group(1)) if m else None


def _retry_after_header(resp):
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if raw is None:
        return None
    secs = _seconds(raw)
    if secs is not None:
        return secs
    try:
        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _parse_error_body(resp):
    try:
        body = resp.json()
        err = body.get("error", {}) if isinstance(body, dict) else {}
    except ValueError:
        err = {}
    message = err.get("message") or (getattr(resp, "text", "") or "")[:300]
    quota, delay = [], None
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        kind = str(d.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                quota.append(f"{v.get('quotaMetric', '')} {v.get('quotaId', '')}")
        elif kind.endswith("RetryInfo"):
            delay = _seconds(d.get("retryDelay"))
    return str(message), err.get("status"), " ".join(quota), delay


def _compose(status, gstatus, message, hint=""):
    head = f"Gemini error {status}" + (f" ({gstatus})" if gstatus else "")
    return _clean(f"{head}: {message[:300]}") + (f" {hint}" if hint else "")


def _classify(resp, attempt):
    """Return (GeminiError, wait_seconds_or_None). wait is None when waiting cannot help."""
    status = resp.status_code
    message, gstatus, quota, delay = _parse_error_body(resp)
    low = f"{message} {quota}".lower()

    if status == 429:
        ids = quota.lower()
        if ids:
            exhausted = any(s in ids for s in ("perday", "per_day", "permonth", "per_month")) or "limit: 0" in low
        else:
            exhausted = any(s in low for s in ("per day", "daily", "per month", "monthly", "limit: 0"))
        if exhausted:
            hint = ("This is a daily or monthly quota (or this model has no free allowance), so waiting a few "
                    "minutes will not fix it. Try again after it resets, or check usage in Google AI Studio.")
            return GeminiError(_compose(status, gstatus, message, hint), status, "quota_exhausted", None, gstatus), None
        wait = _retry_after_header(resp)
        wait = delay if wait is None else wait
        known = wait is not None
        wait = min(5.0 * 2 ** attempt, MAX_WAIT) if wait is None else wait
        hint = f"Temporary rate limit. Try again in about {int(wait + 0.999)} seconds." if known else "Temporary rate limit. Try again shortly."
        err = GeminiError(_compose(status, gstatus, message, hint), status, "rate_limit", wait if known else None, gstatus)
        return err, wait
    if status in (500, 502, 503, 504):
        wait = _retry_after_header(resp)
        wait = min(5.0 * 2 ** attempt, MAX_WAIT) if wait is None else wait
        return GeminiError(_compose(status, gstatus, message, "Google's service had a temporary problem."), status, "server", None, gstatus), wait
    if status == 404:
        return GeminiError(_compose(status, gstatus, message, "Check the GEMINI_MODEL setting on the server."), status, "model_unavailable", None, gstatus), None
    if status in (401, 403) or (status == 400 and "api key" in low):
        return GeminiError(_compose(status, gstatus, message, "Check GEMINI_API_KEY on the server."), status, "auth", None, gstatus), None
    if status == 400:
        return GeminiError(_compose(status, gstatus, message), status, "bad_request", None, gstatus), None
    return GeminiError(_compose(status, gstatus, message), status, "error", None, gstatus), None


def _parse_success(resp):
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini returned no answer ({block or 'empty response'}).", 200, "blocked" if block else "empty")
    cand = cands[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or []).strip()
    meta = cand.get("groundingMetadata") or {}
    queries = meta.get("webSearchQueries") or []
    sources = []
    for ch in meta.get("groundingChunks") or []:
        w = ch.get("web") or {}
        if w.get("uri") or w.get("title"):
            sources.append({"title": w.get("title", ""), "uri": w.get("uri", "")})
    if not text:
        reason = str(cand.get("finishReason") or "")
        kind = "blocked" if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION") else "empty"
        raise GeminiError(f"Gemini returned an empty answer ({reason or 'no reason given'}).", 200, kind)
    return {"text": text, "sources": sources, "grounded": bool(queries or sources),
            "search_queries": queries, "model": MODEL}


def generate(prompt, system=None, search=False, max_tokens=8192, temperature=0.4, retries=None):
    """Return {"text", "sources", "grounded", "search_queries", "model"} or raise GeminiError."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set on the server.", kind="auth")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    max_retries = MAX_RETRIES if retries is None else max(0, retries)

    for attempt in range(max_retries + 1):
        try:
            budget.reserve()
        except budget.BudgetExceeded as e:
            raise GeminiError(str(e), kind="budget")
        try:
            r = requests.post(ENDPOINT.format(model=MODEL), json=body, timeout=120,
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            err = GeminiError(f"Could not reach Gemini ({type(e).__name__}).", kind="network")
            if attempt < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise err
        if r.status_code == 200:
            return _parse_success(r)
        err, wait = _classify(r, attempt)
        if wait is not None and attempt < max_retries and wait <= MAX_WAIT:
            time.sleep(wait)
            continue
        raise err
    raise GeminiError("Gemini request failed.", kind="error")  # pragma: no cover
