"""AI-visibility tracker (Gemini with Google Search grounding).

Each customer question is sent as its own request. For every question the result is one of:
  cited        the client's domain appears among the sources the AI used
  mentioned    the brand is named in the answer (and not only in a negative statement)
  not_found    verified absent: the AI used live search and neither the brand nor the domain appeared
  unverified   could not be checked (request failed, no live search was used, the question already
               names the brand, or a source domain was unreadable). Never counted as a "No".
Answers are cached (see cache.py); only complete, search-grounded answers are ever saved."""
import re
import time
import unicodedata
from collections import Counter
from urllib.parse import urlparse

import cache as cache_mod
import gemini
from gemini import GeminiError, generate

TRACK_PAUSE = gemini._env_num("TRACK_PAUSE_SECONDS", 4, float)
CACHE_VERSION = "t1"

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_NEGATIVE = ("couldn't find", "could not find", "cannot find", "can't find", "unable to find", "not able to find",
             "no information", "no specific information", "no results", "no record", "no evidence", "no mention",
             "not aware of", "don't have information", "do not have information", "did not find", "didn't find",
             "not listed", "isn't listed", "is not listed", "not found", "no business", "no company")


# ---------- matching ----------
def normalize_text(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"[‘’ʼ`´']", "", s)  # apostrophes vanish: Becky's -> beckys
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def brand_regex(brand):
    """Whole-word, accent/punctuation-insensitive pattern. The last word may carry a possessive 's'."""
    tokens = normalize_text(brand).split()
    if not tokens:
        return None
    body = r"\s+".join(re.escape(t) for t in tokens)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:s)?(?![a-z0-9])")


def split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


def _snippet(sentence, limit=240):
    s = re.sub(r"[*#`_>]+", "", sentence).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def find_brand(text, brand):
    """Return (mentioned, evidence_snippet, negative_only).
    A sentence that names the brand only to say nothing was found does not count as a mention."""
    rx = brand_regex(brand)
    if not rx:
        return False, "", False
    positive, negative = "", ""
    for sentence in split_sentences(text):
        if rx.search(normalize_text(sentence)):
            neg = any(p in sentence.lower().replace("’", "'") for p in _NEGATIVE)
            if neg and not negative:
                negative = sentence
            if not neg and not positive:
                positive = sentence
    if positive:
        return True, _snippet(positive), False
    return False, _snippet(negative) if negative else "", bool(negative)


def normalize_domain(value):
    """Return a bare lowercase domain ('' if none given). Raises ValueError if it is not a valid domain."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    v = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", v)
    v = re.split(r"[/?#]", v, maxsplit=1)[0].rsplit("@", 1)[-1]
    v = re.sub(r":\d+$", "", v).rstrip(".")
    if v.startswith("www."):
        v = v[4:]
    if not _DOMAIN_RE.match(v):
        raise ValueError("Enter a valid domain such as example.com")
    return v


def host_matches(host, domain):
    """Exact domain or a real subdomain (blog.acme.com) but never notacme.com or acme.com.evil.net."""
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def source_host(src):
    """Real publisher domain for one grounding source, or None when it cannot be read reliably.
    Google often returns a redirect link, in which case the title holds the publisher's domain."""
    uri_host = (urlparse(src.get("uri") or "").hostname or "").lower()
    if uri_host.startswith("www."):
        uri_host = uri_host[4:]
    if uri_host and not uri_host.endswith("vertexaisearch.cloud.google.com") and _DOMAIN_RE.match(uri_host):
        return uri_host
    title = (src.get("title") or "").strip().lower()
    if title.startswith("www."):
        title = title[4:]
    return title if _DOMAIN_RE.match(title) else None


# ---------- analysis ----------
def analyze(question, brand, domain, raw):
    """Turn one raw answer into a result. Pure function: no network, no randomness."""
    text = raw.get("text") or ""
    grounded = bool(raw.get("grounded"))
    sources = [{"host": source_host(s), "title": s.get("title", ""), "uri": s.get("uri", "")} for s in raw.get("sources") or []]

    rx = brand_regex(brand)
    brand_in_question = bool(rx and rx.search(normalize_text(question)))
    said, snippet, negative_only = find_brand(text, brand)

    if brand_in_question:
        mentioned = None  # the answer repeating the question's own wording proves nothing
    elif said:
        mentioned = True
    else:
        mentioned = False if grounded else None  # absence is only meaningful if live search ran

    cited, hit = None, None
    if domain:
        hits = [s for s in sources if host_matches(s["host"], domain)]
        if hits:
            cited, hit = True, hits[0]
        elif not grounded:
            cited = None
        elif any(s["host"] is None for s in sources):
            cited = None
        else:
            cited = False

    if cited is True:
        status = "cited"
    elif mentioned is True:
        status = "mentioned"
    else:
        checks = [mentioned] + ([cited] if domain else [])
        status = "unverified" if any(c is None for c in checks) else "not_found"

    reason, evidence = "", []
    if hit:
        evidence.append(f"Source used by the AI: {hit['host']}" + (f" ({hit['title']})" if hit["title"] and hit["title"] != hit["host"] else ""))
    if mentioned is True:
        evidence.append(f"“{snippet}”")
    if status == "not_found":
        evidence.append(f"The AI used live search ({len(sources)} sources) and did not name the brand" + (" or cite the domain." if domain else "."))
        if negative_only:
            evidence.append(f"Brand appeared only in a negative statement: “{snippet}”")
    if status == "unverified":
        why = []
        if brand_in_question:
            why.append("the question itself names the brand")
        if not grounded:
            why.append("the AI did not use live web search for this answer")
        elif domain and cited is None:
            why.append("some sources had no readable domain")
        reason = "; ".join(why) or "result could not be confirmed"
    return {"prompt": question, "status": status, "mentioned": mentioned, "cited": cited, "evidence": " ".join(evidence),
            "reason": reason, "sources": sources, "cited_hosts": sorted({s["host"] for s in sources if s["host"]}),
            "grounded": grounded, "model": raw.get("model", ""), "cached": False, "cached_age": None}


def unverified_result(question, error):
    return {"prompt": question, "status": "unverified", "mentioned": None, "cited": None, "evidence": "",
            "reason": str(error), "sources": [], "cited_hosts": [], "grounded": False, "model": "", "cached": False,
            "cached_age": None}


# ---------- running ----------
def _dedupe(questions):
    seen, out = set(), []
    for q in questions:
        k = normalize_text(q)
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out


def _cache_key(question, brand, domain):
    return cache_mod.make_key(v=CACHE_VERSION, q=normalize_text(question), brand=normalize_text(brand), domain=domain,
                              model=gemini.MODEL, search=True, temperature=0.2, max_tokens=1500)


def _ask(question, brand, domain, cache):
    """Return (raw_answer, from_cache, stored_at). Raises GeminiError on failure (nothing is cached then)."""
    key = _cache_key(question, brand, domain)
    entry = cache.get_entry(key) if cache else None
    if entry:
        return entry["value"], True, entry["stored_at"]
    res = generate(question, search=True, max_tokens=1500, temperature=0.2)
    raw = {"text": res["text"], "sources": res.get("sources", []), "grounded": bool(res.get("grounded")),
           "search_queries": res.get("search_queries", []), "model": res.get("model", gemini.MODEL)}
    if cache and raw["text"] and raw["grounded"]:  # never save ungrounded or empty answers as if they were good
        cache.set(key, raw)
    return raw, False, time.time()


def track(questions, brand, domain, progress=lambda m: None, pause=None, cache=None):
    """Return (results, info). One request per question; a failed question never discards the others.
    info = {'cached': n, 'first_error': GeminiError|None, 'aborted_at': index|None}"""
    cache = cache if cache is not None else cache_mod.default_cache()
    pause = TRACK_PAUSE if pause is None else pause
    questions = _dedupe(questions)
    results, info, abort_error, called_api = [], {"cached": 0, "first_error": None, "aborted_at": None}, None, False
    for i, q in enumerate(questions, 1):
        if abort_error is not None:
            results.append(unverified_result(q, f"not sent: {abort_error}"))
            continue
        progress(f"Asking AI question {i} of {len(questions)}...")
        try:
            if called_api and pause:
                time.sleep(pause)  # gentle pacing between live requests (cached answers skip this)
            raw, from_cache, stored_at = _ask(q, brand, domain, cache)
        except GeminiError as e:
            info["first_error"] = info["first_error"] or e
            results.append(unverified_result(q, e))
            if e.abort_run:
                abort_error, info["aborted_at"] = e, i
            called_api = True
            continue
        if not from_cache:
            called_api = True
        r = analyze(q, brand, domain, raw)
        if from_cache:
            r["cached"], r["cached_age"] = True, max(0, time.time() - stored_at)
            info["cached"] += 1
        results.append(r)
    return results, info


# ---------- report ----------
def _yn(v):
    return "Yes" if v is True else "No" if v is False else "Couldn't verify"


_LABEL = {"cited": "Cited", "mentioned": "Mentioned", "not_found": "Not found", "unverified": "Couldn't verify"}


def _age(seconds):
    s = int(seconds or 0)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{max(1, s // 60)}m"


def tracking_markdown(brand, domain, results):
    n = len(results)
    unverified = [r for r in results if r["status"] == "unverified"]
    m_known = [r for r in results if r["mentioned"] is not None]
    c_known = [r for r in results if r["cited"] is not None]
    m_yes = sum(1 for r in m_known if r["mentioned"] or r["cited"])
    lines = [f"## AI visibility snapshot: {brand}", "",
             f"- **Brand mentioned** in {m_yes} of {len(m_known)} verified AI answers"
             + (f" ({m_yes / len(m_known):.0%})" if m_known else "")]
    if domain:
        c_yes = sum(1 for r in c_known if r["cited"])
        lines.append(f"- **{domain} used as a source** in {c_yes} of {len(c_known)} verified answers"
                     + (f" ({c_yes / len(c_known):.0%})" if c_known else ""))
    if unverified:
        lines.append(f"- **Couldn't verify:** {len(unverified)} of {n} questions. These are not counted as \"No\" and are excluded from the percentages.")
    lines += ["", "| Customer question | Mentioned | Cited | Result |", "|---|---|---|---|"]
    for r in results:
        men = _yn(True if r["cited"] and r["mentioned"] is None else r["mentioned"])
        lines.append(f"| {r['prompt']} | {men} | {_yn(r['cited']) if domain else 'n/a'} | {_LABEL[r['status']]} |")

    shown = [r for r in results if r["evidence"]]
    if shown:
        lines += ["", "### Evidence", ""]
        lines += [f"- **{r['prompt']}**: {r['evidence']}" for r in shown]
    if unverified:
        lines += ["", "### Questions that could not be verified", ""]
        lines += [f"- **{r['prompt']}**: {r['reason'] or 'unknown reason'}" for r in unverified]
        lines += ["", "_Run again later to check these. Nothing above treats them as a negative result._"]

    rivals = Counter()
    for r in results:
        if r["status"] != "unverified":
            for h in r["cited_hosts"]:
                if not host_matches(h, domain):
                    rivals[h] += 1
    lines += ["", "### Who the AI relies on instead", ""]
    lines += [f"- {h} (in {c} answers)" for h, c in rivals.most_common(10)] or ["- No sources were reported."]

    cached = [r for r in results if r["cached"]]
    if cached:
        lines += ["", f"_{len(cached)} answer(s) were reused from a saved result up to {_age(max(r['cached_age'] for r in cached))} old, "
                      "to save free-tier quota._"]
    lines += ["", "_AI answers vary between runs and by AI tool. Treat this as a snapshot and re-run monthly to track progress._"]
    return "\n".join(lines)
