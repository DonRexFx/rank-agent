"""Offline tests: a fake website exercises the real crawler + scoring; Gemini is stubbed.
Run: python test_app.py"""
import datetime
import os
import sys
import time

os.environ["APP_PASSWORD"] = "secret123"
os.environ["GEMINI_API_KEY"] = "test-key"

import agent_core  # noqa: E402
import app as webapp  # noqa: E402
import gemini  # noqa: E402
import scoring  # noqa: E402
import tools  # noqa: E402
import tracker  # noqa: E402

GOOD = """<html lang="en"><head><title>Sweet Crumbs Bakery | Custom Cakes in Yaba, Lagos</title>
<meta name="description" content="Custom birthday and wedding cakes, small chops and delivery across Lagos mainland. Order on WhatsApp today.">
<meta name="viewport" content="width=device-width"><link rel="canonical" href="https://crumbs.test/">
<meta property="og:title" content="Sweet Crumbs"><meta property="og:site_name" content="Sweet Crumbs Bakery">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Bakery","name":"Sweet Crumbs"}</script></head>
<body><h1>Custom cakes in Lagos</h1><h2>How much does a custom cake cost?</h2><h2>Do you deliver?</h2>
<p>%s</p><a href="/about">About</a><a href="/contact">Contact</a><a href="tel:+2348012345678">Call</a>
<img src="a.jpg" alt="cake"></body></html>""" % ("cake " * 450)
BARE = "<html><head><title>x</title></head><body><p>hi</p></body></html>"


class FakeResp:
    def __init__(self, text, status=200, url="https://crumbs.test/"):
        self.text, self.status_code, self.url = text, status, url
        self.headers, self.is_redirect = {}, False
        self.elapsed = datetime.timedelta(seconds=0.4)


def fake_get(url, max_redirects=5):
    path = url.split("crumbs.test", 1)[-1]
    if path in ("", "/"):
        return FakeResp(GOOD, url=url)
    if path == "/robots.txt":
        return FakeResp("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    if path == "/sitemap.xml":
        return FakeResp("<urlset><loc>https://crumbs.test/</loc><loc>https://crumbs.test/about</loc></urlset>")
    if path == "/llms.txt":
        return FakeResp("", status=404)
    return FakeResp(BARE, url=url)


tools._get = fake_get
tools.assert_public_url = lambda u: None
webapp.assert_public_url = lambda u: None
CALLS = []


def fake_generate(prompt, system=None, search=False, **kw):
    CALLS.append({"search": search, "prompt": prompt})
    return {"text": "## Executive summary\nStub advice.", "sources": [{"title": "rival.com", "uri": "https://x/redirect1"}]}


gemini.generate = agent_core.generate = tracker.generate = fake_generate
tracker.time.sleep = lambda s: None


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


# ---- engine ----
site = tools.crawl_site("https://crumbs.test/")
check("crawler found homepage + sitemap pages", len(site["pages"]) >= 2)
check("robots parsing flags GPTBot", "GPTBot" in site["files"]["ai_bots_blocked"])
check("llms.txt missing detected", site["files"]["llms_txt_found"] is False)
sc = scoring.score_site(site)
check("scores in range", 0 <= sc["overall"] <= 100 and all(0 <= v <= 100 for v in sc["categories"].values()))
check("good site scores higher on-page than bare page",
      scoring.score_site({"pages": [site["pages"][0]], "files": site["files"], "base": site["base"]})["categories"]["On-page SEO"] >
      scoring.score_site({"pages": [tools.analyze_html(BARE, "https://crumbs.test/")], "files": site["files"], "base": site["base"]})["categories"]["On-page SEO"])
check("name guessed from og:site_name", scoring.guess_name(site["pages"][0]) == "Sweet Crumbs Bakery")
check("llms.txt generated", "# Sweet Crumbs Bakery" in scoring.make_llms_txt(site))
check("jsonld uses phone found", "+2348012345678" in scoring.make_jsonld(site))

out = agent_core.run_audit("https://crumbs.test/", client="Mama Tee")
check("audit report has score, files, client", "Overall score" in out["report"] and "llms.txt" in out["report"] and "Mama Tee" in out["report"])
check("audit made exactly one model call", len(CALLS) == 1 and CALLS[0]["search"] is False)

# ---- tracker ----
res = tracker.track(["Best cake in Lagos?", "Who delivers cakes?"], "Sweet Crumbs", "crumbs.test")
md = tracker.tracking_markdown("Sweet Crumbs", "crumbs.test", res)
check("tracker lists rival sources", "rival.com" in md and "0 of 2" in md)

# ---- API ----
c = webapp.app.test_client()
H = {"X-Access-Code": "secret123"}


def wait(jid):
    for _ in range(80):
        j = c.get(f"/api/status/{jid}", headers=H).get_json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job timed out")


check("index page served with brand", b"Rank Agent" in c.get("/").data)
check("health ok", c.get("/health").get_json()["configured"])
check("wrong code -> 401", c.post("/api/start", json={"mode": "audit", "url": "x.com"}, headers={"X-Access-Code": "no"}).status_code == 401)
check("bad mode -> 400", c.post("/api/start", json={"mode": "zzz"}, headers=H).status_code == 400)
check("launch needs description", c.post("/api/start", json={"mode": "launch", "business": "x"}, headers=H).status_code == 400)
check("track needs questions", c.post("/api/start", json={"mode": "track", "brand": "A"}, headers=H).status_code == 400)

r = c.post("/api/start", json={"mode": "audit", "url": "crumbs.test", "client": "Mama Tee"}, headers=H)
j = wait(r.get_json()["id"])
check("audit job done with scores", j["status"] == "done" and j["scores"]["overall"] > 0 and "Overall score" in j["report"])
r = c.post("/api/start", json={"mode": "launch", "business": "Bakery in Yaba, Lagos"}, headers=H)
j = wait(r.get_json()["id"])
check("launch job done (works with no website)", j["status"] == "done" and "Launch Plan" in j["report"])
r = c.post("/api/start", json={"mode": "research", "business": "Bakery in Yaba, Lagos", "url": ""}, headers=H)
check("research job done", wait(r.get_json()["id"])["status"] == "done")
r = c.post("/api/start", json={"mode": "track", "brand": "Sweet Crumbs", "domain": "", "prompts": "Best cake in Lagos?"}, headers=H)
check("track works without a domain", wait(r.get_json()["id"])["status"] == "done")
r = c.post("/api/start", json={"mode": "rewrite", "url": "crumbs.test"}, headers=H)
check("rewrite job done", wait(r.get_json()["id"])["status"] == "done")

# errors surface cleanly
def boom(*a, **k):
    raise gemini.GeminiError("Free Gemini limit reached for now.")
agent_core.generate = boom
r = c.post("/api/start", json={"mode": "launch", "business": "Bakery in Yaba, Lagos"}, headers=H)
j = wait(r.get_json()["id"])
check("rate-limit error shown to user", j["status"] == "error" and "limit" in j["error"])
print("ALL TESTS PASSED")
