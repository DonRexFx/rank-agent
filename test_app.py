"""Offline tests: stubs the Anthropic SDK so no network or API key is needed.
Run: python test_app.py"""
import os
import sys
import time
import types

if "anthropic" not in sys.modules:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        stub = types.ModuleType("anthropic")
        stub.Anthropic = lambda *a, **k: object()
        sys.modules["anthropic"] = stub

os.environ["APP_PASSWORD"] = "secret123"
os.environ["ANTHROPIC_API_KEY"] = "test-key"

import app as webapp  # noqa: E402
from tools import assert_public_url  # noqa: E402

webapp.run_agent = lambda mode, progress=None, **kw: (progress("step 1"), f"# Report for {mode}\n\n{kw}")[1]
webapp.run_prompt = lambda client, q, brand, domain: {
    "prompt": q, "brand_mentioned": "acme" in q.lower(), "domain_cited": False, "sources": ["https://a.test/x"],
    "answer_excerpt": "",
}
c = webapp.app.test_client()
H = {"X-Access-Code": "secret123"}


def wait(jid):
    for _ in range(50):
        j = c.get(f"/api/status/{jid}", headers=H).get_json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job timed out")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


check("index page served", b"Rank Agent" in c.get("/").data)
check("health ok", c.get("/health").get_json()["ok"])
check("wrong code -> 401", c.post("/api/start", json={"mode": "audit", "url": "x.com"}, headers={"X-Access-Code": "no"}).status_code == 401)
check("no code -> 401", c.post("/api/start", json={"mode": "audit"}).status_code == 401)
check("bad mode -> 400", c.post("/api/start", json={"mode": "zzz"}, headers=H).status_code == 400)
check("empty url -> 400", c.post("/api/start", json={"mode": "audit", "url": ""}, headers=H).status_code == 400)
check("localhost blocked", c.post("/api/start", json={"mode": "audit", "url": "http://127.0.0.1:8000"}, headers=H).status_code == 400)
check("metadata IP blocked", c.post("/api/start", json={"mode": "audit", "url": "http://169.254.169.254/latest"}, headers=H).status_code == 400)
check("file:// blocked", c.post("/api/start", json={"mode": "audit", "url": "file:///etc/passwd"}, headers=H).status_code == 400)
check("research needs description", c.post("/api/start", json={"mode": "research", "business": "x"}, headers=H).status_code == 400)
check("track needs fields", c.post("/api/start", json={"mode": "track", "brand": "A"}, headers=H).status_code == 400)

try:
    assert_public_url("http://10.0.0.5/")
    check("private ip blocked in tools", False)
except ValueError:
    check("private ip blocked in tools", True)

r = c.post("/api/start", json={"mode": "research", "business": "Dental clinic in Lagos", "url": ""}, headers=H)
j = wait(r.get_json()["id"])
check("research job done", j["status"] == "done" and "Report for research" in j["report"])

r = c.post("/api/start", json={"mode": "track", "brand": "Acme", "domain": "https://www.acme.com/page",
                               "prompts": "Best Acme alternative?\nWho sells widgets?"}, headers=H)
j = wait(r.get_json()["id"])
check("track job done", j["status"] == "done" and "1/2" in j["report"] and "www.acme.com" in j["report"])
check("status needs code", c.get(f"/api/status/{r.get_json()['id']}").status_code == 401)
check("unknown job -> 404", c.get("/api/status/nope", headers=H).status_code == 404)
print("ALL TESTS PASSED")
