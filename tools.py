"""Site-analysis tools the agent can call: page audit, robots/sitemap/llms.txt checks, link discovery."""
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "RankAgent/1.0 (+site audit)"
TIMEOUT = 15

AI_BOTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
    "Claude-User", "PerplexityBot", "Google-Extended", "Applebot-Extended",
    "CCBot", "Bytespider",
]


def assert_public_url(url):
    """Refuse non-http(s) URLs and hosts that resolve to private/loopback/link-local addresses
    (prevents the server being used to probe internal networks)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"Cannot resolve host: {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("That address is not a public website")


def _get(url, max_redirects=5):
    """GET with every redirect hop validated as a public URL."""
    for _ in range(max_redirects + 1):
        assert_public_url(url)
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=False)
        if r.is_redirect and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            continue
        return r
    raise ValueError("Too many redirects")


def analyze_html(html, url, status=200, elapsed=None):
    """Pure function: turn HTML into a dict of SEO / AI-readiness signals."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
        return tag.get("content", "").strip() if tag else None

    schema_types, schema_errors = [], 0
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            schema_errors += 1
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict):
                t = it.get("@type")
                schema_types.extend(t if isinstance(t, list) else [t] if t else [])

    host = urlparse(url).netloc
    internal, external = set(), set()
    for a in soup.find_all("a", href=True):
        full = urljoin(url, a["href"]).split("#")[0]
        if not full.startswith("http"):
            continue
        (internal if urlparse(full).netloc == host else external).add(full)

    imgs = soup.find_all("img")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    headings = [(h.name, h.get_text(" ", strip=True)[:120]) for h in soup.find_all(["h1", "h2", "h3"])]
    title = soup.title.get_text(strip=True) if soup.title else None
    desc = meta(name="description")
    canonical = soup.find("link", rel="canonical")
    html_tag = soup.find("html")

    issues = []
    if status >= 400: issues.append(f"HTTP status {status}")
    if not title: issues.append("Missing <title>")
    elif not 30 <= len(title) <= 65: issues.append(f"Title length {len(title)} (aim 30-65)")
    if not desc: issues.append("Missing meta description")
    elif not 70 <= len(desc) <= 160: issues.append(f"Meta description length {len(desc)} (aim 70-160)")
    h1s = [h for n, h in headings if n == "h1"]
    if len(h1s) != 1: issues.append(f"{len(h1s)} H1 tags (want exactly 1)")
    if not canonical: issues.append("No canonical link")
    if not meta(name="viewport"): issues.append("No viewport meta (mobile)")
    if not (html_tag and html_tag.get("lang")): issues.append("No <html lang>")
    if not schema_types: issues.append("No JSON-LD structured data")
    if schema_errors: issues.append(f"{schema_errors} invalid JSON-LD block(s)")
    no_alt = sum(1 for i in imgs if not i.get("alt"))
    if no_alt: issues.append(f"{no_alt}/{len(imgs)} images missing alt text")
    if len(text.split()) < 300: issues.append(f"Thin content ({len(text.split())} words)")
    if not meta(prop="og:title"): issues.append("No Open Graph tags")
    if "FAQPage" not in schema_types and not any("?" in h for _, h in headings):
        issues.append("No question-style headings or FAQ schema (helps AI answer engines)")

    phones = sorted({a["href"][4:].strip() for a in soup.find_all("a", href=True) if a["href"].lower().startswith("tel:")})
    emails = sorted({a["href"][7:].split("?")[0].strip() for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")})
    question_heads = sum(1 for _, h in headings if "?" in h or re.match(r"(?i)(how|what|why|when|where|who|can|do|does|is|are|which)\b", h))

    return {
        "has_viewport": bool(meta(name="viewport")), "has_og": bool(meta(prop="og:title")),
        "og_site_name": meta(prop="og:site_name"), "h1_count": len(h1s),
        "question_headings": question_heads, "phones": phones[:3], "emails": emails[:3],
        "url": url, "status": status, "response_seconds": elapsed,
        "title": title, "meta_description": desc,
        "canonical": canonical.get("href") if canonical else None,
        "lang": html_tag.get("lang") if html_tag else None,
        "headings": headings[:30], "word_count": len(text.split()),
        "schema_types": sorted(set(schema_types)), "images": len(imgs), "images_missing_alt": no_alt,
        "internal_links": len(internal), "external_links": len(external),
        "issues": issues, "text_excerpt": text[:1500],
        "_internal_urls": sorted(internal)[:200],
    }


def fetch_page(url):
    try:
        r = _get(url)
        res = analyze_html(r.text, r.url, r.status_code, round(r.elapsed.total_seconds(), 2))
        res.pop("_internal_urls", None)
        return res
    except Exception as e:
        return {"url": url, "error": str(e)}


def find_internal_links(url, limit=15):
    try:
        r = _get(url)
        res = analyze_html(r.text, r.url, r.status_code)
        return {"url": url, "links": res["_internal_urls"][:limit]}
    except Exception as e:
        return {"url": url, "error": str(e)}


def check_site_files(url):
    """Check robots.txt (incl. AI crawler rules), sitemap.xml and llms.txt."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    out = {"base": base}
    try:
        r = _get(base + "/robots.txt")
        out["robots_status"] = r.status_code
        if r.status_code == 200:
            txt = r.text
            out["robots_has_sitemap"] = "sitemap:" in txt.lower()
            blocked = []
            groups = re.split(r"(?im)^user-agent:\s*", txt)[1:]
            for g in groups:
                agent = g.splitlines()[0].strip()
                if re.search(r"(?im)^disallow:\s*/\s*$", g):
                    blocked.append(agent)
            out["agents_blocked_entirely"] = blocked
            out["ai_bots_blocked"] = [b for b in AI_BOTS if b.lower() in [x.lower() for x in blocked] or "*" in blocked]
            out["robots_excerpt"] = txt[:800]
    except Exception as e:
        out["robots_error"] = str(e)
    for name, path in (("sitemap", "/sitemap.xml"), ("llms_txt", "/llms.txt")):
        try:
            r = _get(base + path)
            out[f"{name}_found"] = r.status_code == 200 and len(r.text) > 20
        except Exception as e:
            out[f"{name}_error"] = str(e)
    return out


PRIORITY = ("about", "contact", "service", "product", "pricing", "price", "shop", "menu", "blog", "faq", "location", "book", "treatment")
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|pdf|zip|css|js|svg|webp|mp4|xml)(\?|$)", re.I)


def _sitemap_urls(base, host):
    try:
        r = _get(base + "/sitemap.xml")
        if r.status_code != 200:
            return []
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.I | re.S)
        if locs and all(l.endswith(".xml") for l in locs[:3]):
            r2 = _get(locs[0])
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r2.text, re.I | re.S) if r2.status_code == 200 else []
        return [l for l in locs if urlparse(l).netloc == host]
    except Exception:
        return []


def crawl_site(url, max_pages=8):
    """Fetch the homepage plus the most important internal pages. Returns {base, pages, files}."""
    r = _get(url)
    final = r.url
    home = analyze_html(r.text, final, r.status_code, round(r.elapsed.total_seconds(), 2))
    p = urlparse(final)
    base = f"{p.scheme}://{p.netloc}"
    cands = _sitemap_urls(base, p.netloc) + home["_internal_urls"]

    def pri(u):
        return 0 if any(k in urlparse(u).path.lower() for k in PRIORITY) else 1

    seen, targets = {final.rstrip("/")}, []
    for u in sorted(dict.fromkeys(cands), key=pri):
        k = u.rstrip("/")
        if k in seen or SKIP_EXT.search(u):
            continue
        seen.add(k)
        targets.append(u)
    targets = targets[: max_pages - 1]

    def one(u):
        try:
            rr = _get(u)
            return analyze_html(rr.text, rr.url, rr.status_code, round(rr.elapsed.total_seconds(), 2))
        except Exception:
            return None

    pages = [home]
    if targets:
        with ThreadPoolExecutor(4) as ex:
            pages += [x for x in ex.map(one, targets) if x]
    for pg in pages:
        pg.pop("_internal_urls", None)
    return {"base": base, "pages": pages, "files": check_site_files(final)}


TOOL_FUNCS = {
    "fetch_page": lambda a: fetch_page(a["url"]),
    "find_internal_links": lambda a: find_internal_links(a["url"], a.get("limit", 15)),
    "check_site_files": lambda a: check_site_files(a["url"]),
}

TOOL_SCHEMAS = [
    {"name": "fetch_page", "description": "Fetch a page and return SEO/AI-readiness signals: title, meta, headings, schema types, word count, issues, text excerpt.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "find_internal_links", "description": "List internal links found on a page, to pick key pages to audit.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["url"]}},
    {"name": "check_site_files", "description": "Check robots.txt (AI crawler blocking), sitemap.xml and llms.txt for a site.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]
