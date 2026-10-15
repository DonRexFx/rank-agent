"""Rank Agent web app: mobile-friendly UI + small JSON API with background jobs (Gemini free tier)."""
import hmac
import os
import threading
import time
import uuid
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request

import agent_core
import budget
from gemini import GeminiError
from tools import assert_public_url
from tracker import normalize_domain

app = Flask(__name__)

ACCESS_CODE = os.environ.get("APP_PASSWORD", "")
MAX_RUNNING = 2            # concurrent jobs (free Gemini tier is rate limited)
MAX_JOBS_PER_HOUR = 10     # per IP
JOB_TTL = 60 * 60

JOBS, LOCK = {}, threading.Lock()
RECENT = defaultdict(deque)


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


def worker(jid, mode, p):
    job = JOBS[jid]
    note = lambda m: job["progress"].append(m)
    budget.BUDGET.begin_run()  # per-run cap on AI calls, retries included
    try:
        client = p.get("client", "")
        if mode == "audit":
            out = agent_core.run_audit(p["url"], client, note)
        elif mode == "rewrite":
            out = agent_core.run_rewrite(p["url"], client, note)
        elif mode == "research":
            out = agent_core.run_research(p["business"], p["url"], client, note)
        elif mode == "launch":
            out = agent_core.run_launch(p["business"], client, note)
        else:
            out = agent_core.run_track(p["brand"], p["domain"], p["prompts"], client, note)
        job["partial"], job["warnings"] = bool(out.get("partial")), list(out.get("warnings") or [])
        job["report"], job["scores"], job["status"] = out["report"], out["scores"], "done"
    except (GeminiError, ValueError) as e:
        job["status"], job["error"] = "error", str(e)[:500]
    except Exception as e:
        job["status"], job["error"] = "error", f"Something went wrong ({type(e).__name__}). Please try again."
    finally:
        budget.BUDGET.end_run()


@app.get("/")
def index():
    return render_template("index.html", brand=agent_core.BRAND)


@app.get("/health")
def health():
    return {"ok": True, "configured": bool(ACCESS_CODE and os.environ.get("GEMINI_API_KEY"))}


@app.post("/api/start")
def start():
    if not ACCESS_CODE:
        return jsonify(error="Server not configured: set APP_PASSWORD."), 503
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify(error="Server not configured: set GEMINI_API_KEY."), 503
    if not authorized():
        return jsonify(error="Wrong access code."), 401

    if budget.BUDGET.remaining() == 0:
        return jsonify(error="This server's daily AI call budget is used up. It resets at 00:00 UTC."), 429

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
        client = (data.get("client") or "").strip()[:100]
        try:
            if mode in ("audit", "rewrite"):
                p = {"url": norm_url(data.get("url"))}
            elif mode in ("research", "launch"):
                business = (data.get("business") or "").strip()[:400]
                if len(business) < 8:
                    raise ValueError("Describe the business: what it does and where")
                url = norm_url(data["url"]) if (data.get("url") or "").strip() and mode == "research" else "(none yet)"
                p = {"business": business, "url": url}
            elif mode == "track":
                brand = (data.get("brand") or "").strip()[:100]
                domain = normalize_domain((data.get("domain") or "")[:300])  # raises ValueError if invalid; blank is allowed
                prompts = [l.strip()[:200] for l in (data.get("prompts") or "").splitlines() if l.strip()][:8]
                if not brand or not prompts:
                    raise ValueError("Enter the brand name and at least one question")
                if len(brand) < 3:
                    raise ValueError("The brand name is too short to match reliably in AI answers")
                p = {"brand": brand, "domain": domain, "prompts": prompts}
            else:
                raise ValueError("Unknown mode")
        except ValueError as e:
            return jsonify(error=str(e)), 400
        p["client"] = client

        jid = uuid.uuid4().hex
        JOBS[jid] = {"status": "running", "progress": [], "report": None, "scores": None, "error": None,
                       "partial": False, "warnings": [], "created": now}
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
    return jsonify(status=job["status"], progress=job["progress"][-6:], report=job["report"],
                   scores=job["scores"], error=job["error"], partial=job["partial"], warnings=job["warnings"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
