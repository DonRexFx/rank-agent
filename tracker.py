"""AI-visibility tracker using Gemini with Google Search grounding: asks real customer questions and
records whether the brand is mentioned and which sites the AI relied on (your competitors)."""
import time
from collections import Counter
from urllib.parse import urlparse

from gemini import generate


def _host(src):
    # Grounding links are often redirect URLs; the page title is usually the real domain.
    title = (src.get("title") or "").lower().replace("www.", "")
    if "." in title and " " not in title:
        return title
    return urlparse(src.get("uri", "")).netloc.lower().replace("www.", "")


def run_prompt(question, brand, domain):
    res = generate(question, search=True, max_tokens=1500, temperature=0.2)
    text = res["text"]
    hosts = [h for h in (_host(s) for s in res["sources"]) if h]
    dom = (domain or "").lower().replace("www.", "")
    return {
        "prompt": question,
        "brand_mentioned": brand.lower() in text.lower(),
        "domain_cited": bool(dom) and any(dom in h or dom in s.get("uri", "").lower() for h, s in zip(hosts, res["sources"])),
        "cited_hosts": sorted(set(hosts)),
        "excerpt": text[:300],
    }


def track(questions, brand, domain, progress=lambda m: None, pause=4):
    results = []
    for i, q in enumerate(questions, 1):
        progress(f"Asking AI question {i} of {len(questions)}...")
        results.append(run_prompt(q, brand, domain))
        if i < len(questions):
            time.sleep(pause)  # stay under free-tier per-minute limits
    return results


def tracking_markdown(brand, domain, results):
    n = len(results)
    mention = sum(r["brand_mentioned"] for r in results)
    cited = sum(r["domain_cited"] for r in results)
    rivals = Counter(h for r in results for h in r["cited_hosts"] if not domain or domain.lower().replace("www.", "") not in h)
    lines = [f"## AI visibility snapshot: {brand}", "",
             f"- **Brand mentioned** in {mention} of {n} AI answers ({mention / n:.0%})"]
    if domain:
        lines.append(f"- **{domain} used as a source** in {cited} of {n} answers ({cited / n:.0%})")
    lines += ["", "| Customer question | Mentioned | Cited |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r['prompt']} | {'Yes' if r['brand_mentioned'] else 'No'} | {'Yes' if r['domain_cited'] else 'No'} |")
    lines += ["", "### Who the AI relies on instead", ""]
    top = rivals.most_common(10)
    lines += [f"- {h} (in {c} answers)" for h, c in top] or ["- No sources were reported."]
    lines += ["", "_AI answers vary between runs and by AI tool. Treat this as a snapshot and re-run monthly to track progress._"]
    return "\n".join(lines)
