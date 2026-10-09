"""Rank Agent web app: mobile-friendly UI + small JSON API with background jobs."""
import hmac
import os
import threading
import time
import uuid
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request

from agent_core import run_agent
from tools import assert_public_url
from tracker import run_prompt

import anthropic

app = Flask(__name__)

ACCESS_CODE = os.environ.get("APP_PASSWORD", "")
MAX_RUNNING = 3            # concurrent jobs
MAX_JOBS_PER_HOUR = 15     # per IP
JOB_TTL = 60 * 60          # keep finished jobs for an hour

JOBS, LOCK = {}, threading.Lock()
RECENT = defaultdict(deque)  # ip -> timestamps


def clean_jobs():
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
        JOBS.pop(jid, None)


def authorized():
    sent = request.headers.get("X-Access-Code", "")
    return bool(ACCESS_CODE) and hmac.compare_digest(sent.encode(), ACCESS_CODE.encode())


def norm_url(u):
    u = (u or "").strip()
    if not u:
        raise ValueError("Please enter a website URL")
    if "://" not in u:
        u = "https://" + u
    if len(u) > 300:
        raise ValueError("URL is too long")
    assert_public_url(u)
    return u


def tracking_markdown(brand, domain, results):
    n = len(results)
    mention = sum(r["brand_mentioned"] for r in results)
    cited = sum(r["domain_cited"] for r in results)
    lines = [f"# AI visibility: {brand}", "",
             f"- Brand mentioned in **{mention}/{n}** answers ({mention / n:.0%})",
             f"- {domain} cited as a source in **{cited}/{n}** answers ({cited / n:.0%})", "",
             "| Question | Mentioned | Cited |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r['prompt']} | {'yes' if r['brand_mentioned'] else 'no'} | {'yes' if r['domain_cited'] else 'no'} |")
    lines += ["", "## Sources the AI used", ""]
    seen = []
    for r in results:
        for s in r["sources"]:
            if s not in seen:
                seen.append(s)
    lines += [f"- {s}" for s in seen[:15]] or ["- (none)"]
    lines += ["", "_Re-run weekly to see the trend. AI answers vary, so treat single runs as a snapshot._"]
    return "\n".join(lines)


def worker(jid, mode, p):
    job = JOBS[jid]
    note = lambda m: job["progress"].append(m)
    try:
        if mode == "track":
            results = []
            client = anthropic.Anthropic()
            for i, q in enumerate(p["prompts"], 1):
                note(f"Asking AI question {i} of {len(p['prompts'])}...")
                results.append(run_prompt(client, q, p["brand"], p["domain"]))
            job["report"] = tracking_markdown(p["brand"], p["domain"], results)
        else:
            job["report"] = run_agent(mode, progress=note, **p["fmt"])
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {str(e)[:200]}"


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return {"ok": True, "configured": bool(ACCESS_CODE and os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/api/start")
def start():
    if not ACCESS_CODE:
        return jsonify(error="Server not configured: set APP_PASSWORD."), 503
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify(error="Server not configured: set ANTHROPIC_API_KEY."), 503
    if not authorized():
        return jsonify(error="Wrong access code."), 401

    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()
    now = time.time()
    with LOCK:
        clean_jobs()
        q = RECENT[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= MAX_JOBS_PER_HOUR:
            return jsonify(error="Hourly limit reached. Try again later."), 429
        if sum(1 for j in JOBS.values() if j["status"] == "running") >= MAX_RUNNING:
            return jsonify(error="Server is busy. Try again in a minute."), 429

        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        try:
            if mode in ("audit", "rewrite"):
                p = {"fmt": {"url": norm_url(data.get("url"))}}
            elif mode == "research":
                business = (data.get("business") or "").strip()[:300]
                if len(business) < 5:
                    raise ValueError("Describe the business (what it does and where)")
                url = norm_url(data["url"]) if (data.get("url") or "").strip() else "(none provided)"
                p = {"fmt": {"business": business, "url": url}}
            elif mode == "track":
                brand = (data.get("brand") or "").strip()[:100]
                domain = (data.get("domain") or "").strip().lower().replace("https://", "").replace("http://", "").split("/")[0][:100]
                prompts = [l.strip()[:200] for l in (data.get("prompts") or "").splitlines() if l.strip()][:8]
                if not brand or not domain or not prompts:
                    raise ValueError("Enter brand name, domain, and at least one question")
                p = {"brand": brand, "domain": domain, "prompts": prompts}
            else:
                raise ValueError("Unknown mode")
        except ValueError as e:
            return jsonify(error=str(e)), 400

        jid = uuid.uuid4().hex
        JOBS[jid] = {"status": "running", "progress": [], "report": None, "error": None, "created": now}
        q.append(now)

    threading.Thread(target=worker, args=(jid, mode, p), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/status/<jid>")
def status(jid):
    if not authorized():
        return jsonify(error="Wrong access code."), 401
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Job not found or expired."), 404
    return jsonify(status=job["status"], progress=job["progress"][-6:], report=job["report"], error=job["error"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
