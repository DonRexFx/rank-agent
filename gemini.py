"""Minimal Google Gemini client over REST (no SDK needed). Free tier works with a key from
https://aistudio.google.com/apikey. Supports Google Search grounding."""
import os
import time

import requests

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(Exception):
    pass


def generate(prompt, system=None, search=False, max_tokens=8192, temperature=0.4, retries=3):
    """Return {"text": str, "sources": [{"title","uri"}]}. Retries on rate limits."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set on the server.")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if search:
        body["tools"] = [{"google_search": {}}]

    r = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(ENDPOINT.format(model=MODEL), json=body, timeout=120,
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(3)
                continue
            raise GeminiError(f"Could not reach Gemini: {type(e).__name__}")
        if r.status_code in (429, 500, 502, 503) and attempt < retries:
            time.sleep(min(6 * 2 ** attempt, 40))
            continue
        break

    if r.status_code == 429:
        raise GeminiError("Free Gemini limit reached for now. Wait a few minutes (or until tomorrow) and try again.")
    if r.status_code in (400, 403) and "API key" in r.text:
        raise GeminiError("The Gemini API key was rejected. Check GEMINI_API_KEY on the server.")
    if r.status_code != 200:
        raise GeminiError(f"Gemini error {r.status_code}: {r.text[:200]}")

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise GeminiError(f"Gemini returned no answer ({data.get('promptFeedback') or 'empty'}).")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    sources = []
    for ch in (cands[0].get("groundingMetadata") or {}).get("groundingChunks") or []:
        w = ch.get("web") or {}
        if w.get("uri") or w.get("title"):
            sources.append({"title": w.get("title", ""), "uri": w.get("uri", "")})
    if not text:
        raise GeminiError("Gemini returned an empty answer. Try again.")
    return {"text": text, "sources": sources}
