"""Deterministic scoring and generated assets (llms.txt, JSON-LD). No AI involved, so numbers
are consistent from run to run and can be shown to clients."""
import json
import re
from urllib.parse import urlparse

ENTITY_TYPES = {"organization", "localbusiness", "dentist", "restaurant", "store", "professionalservice",
                "medicalbusiness", "legalservice", "realestateagent", "hotel", "person", "website",
                "autodealer", "healthandbeautybusiness", "homeandconstructionbusiness", "financialservice"}
WEIGHTS = {"Technical": 0.20, "On-page SEO": 0.25, "Structured data": 0.20, "AI readiness": 0.25, "Content": 0.10}


def _avg(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0


def _pct(x):
    return max(0, min(100, round(x)))


def grade(n):
    return "Strong" if n >= 80 else "Good" if n >= 65 else "Needs work" if n >= 40 else "Weak"


def score_site(site):
    pages, files = site["pages"], site["files"]
    home = pages[0]

    # Technical
    def tech(p):
        s = 30 * (p["status"] < 400) + 20 * bool(p["has_viewport"]) + 10 * bool(p["lang"]) + 20 * bool(p["canonical"])
        rs = p.get("response_seconds")
        return s + (20 if rs is not None and rs <= 2.5 else 10 if rs is not None and rs <= 5 else 0)

    site_tech = _avg([site["base"].startswith("https"), files.get("sitemap_found", False), files.get("robots_status") == 200]) * 100
    technical = 0.7 * _avg(tech(p) for p in pages) + 0.3 * site_tech

    # On-page
    def onpage(p):
        t, d = p["title"], p["meta_description"]
        s = 30 * bool(t and 30 <= len(t) <= 65) + 12 * bool(t and not 30 <= len(t) <= 65)
        s += 25 * bool(d and 70 <= len(d) <= 160) + 10 * bool(d and not 70 <= len(d) <= 160)
        s += 25 * (p["h1_count"] == 1) + 10 * (p["h1_count"] > 1)
        s += 10 * bool(p["has_og"]) + 10 * (len(p["headings"]) >= 3)
        return s
    onpage_s = _avg(onpage(p) for p in pages)

    # Structured data
    types = {t.lower() for p in pages for t in p["schema_types"] if isinstance(t, str)}
    home_types = {t.lower() for t in home["schema_types"] if isinstance(t, str)}
    structured = (40 * _avg(bool(p["schema_types"]) for p in pages)
                  + 30 * bool(home_types & ENTITY_TYPES) + 15 * ("faqpage" in types)
                  + 15 * bool(types - ENTITY_TYPES - {"faqpage"}))

    # AI readiness
    blocked = files.get("ai_bots_blocked", [])
    ai = (30 * (not blocked) + 15 * bool(files.get("llms_txt_found"))
          + 20 * min(1, _avg(p["question_headings"] > 0 for p in pages) * 2)
          + 15 * min(1, _avg(p["word_count"] for p in pages) / 400)
          + 10 * bool(home_types or types)
          + 10 * bool(any(p["phones"] or p["emails"] for p in pages)))

    # Content
    content = (50 * min(1, _avg(p["word_count"] for p in pages) / 600)
               + 25 * _avg(1 - (p["images_missing_alt"] / p["images"] if p["images"] else 0) for p in pages)
               + 25 * min(1, _avg(p["internal_links"] for p in pages) / 8))

    cats = {"Technical": _pct(technical), "On-page SEO": _pct(onpage_s), "Structured data": _pct(structured),
            "AI readiness": _pct(ai), "Content": _pct(content)}
    overall = _pct(sum(cats[k] * w for k, w in WEIGHTS.items()))
    return {"overall": overall, "grade": grade(overall), "categories": cats}


def guess_name(home):
    if home.get("og_site_name"):
        return home["og_site_name"]
    t = home.get("title") or urlparse(home["url"]).netloc
    return re.split(r"\s[|\-–—:]\s", t)[0].strip()[:80]


def make_llms_txt(site):
    home = site["pages"][0]
    lines = [f"# {guess_name(home)}", ""]
    if home.get("meta_description"):
        lines += [f"> {home['meta_description']}", ""]
    lines += ["## Key pages", ""]
    for p in site["pages"][:8]:
        if p["status"] < 400 and p.get("title"):
            d = f": {p['meta_description']}" if p.get("meta_description") else ""
            lines.append(f"- [{p['title']}]({p['url']}){d}")
    return "\n".join(lines)


def make_jsonld(site):
    home = site["pages"][0]
    data = {
        "@context": "https://schema.org", "@type": "LocalBusiness",
        "name": guess_name(home), "url": home["url"],
        "description": home.get("meta_description") or "[CONFIRM: one-sentence description]",
        "telephone": (home["phones"] or ["[CONFIRM: phone number]"])[0],
        "email": (home["emails"] or ["[CONFIRM: email]"])[0],
        "address": {"@type": "PostalAddress", "streetAddress": "[CONFIRM]", "addressLocality": "[CONFIRM]",
                    "addressCountry": "[CONFIRM]"},
        "openingHours": "[CONFIRM: e.g. Mo-Sa 09:00-18:00]",
        "sameAs": ["[CONFIRM: Facebook/Instagram/LinkedIn/Google Business Profile URLs]"],
    }
    return json.dumps(data, indent=2)
