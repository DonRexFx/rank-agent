"""AI-visibility tracker: ask Claude (with live web search) real customer questions and
record whether the brand/domain is mentioned or cited. Results append to a JSONL history."""
import datetime
import json
import os

import anthropic

MODEL = os.environ.get("RANK_AGENT_MODEL", "claude-sonnet-5-5")
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


def run_prompt(client, prompt, brand, domain):
    resp = client.messages.create(
        model=MODEL, max_tokens=1500, tools=[WEB_SEARCH],
        messages=[{"role": "user", "content": prompt}],
    )
    text, urls = "", set()
    for block in resp.content:
        if block.type == "text":
            text += block.text
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None):
                    urls.add(c.url)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                if getattr(r, "url", None):
                    urls.add(r.url)
    dom = domain.lower().replace("www.", "")
    return {
        "prompt": prompt,
        "brand_mentioned": brand.lower() in text.lower(),
        "domain_cited": any(dom in u.lower() for u in urls),
        "sources": sorted(urls)[:10],
        "answer_excerpt": text[:400],
    }


def track(prompts, brand, domain, history_path="reports/visibility_history.jsonl"):
    client = anthropic.Anthropic()
    results = [run_prompt(client, p, brand, domain) for p in prompts]
    n = len(results)
    summary = {
        "date": datetime.date.today().isoformat(), "brand": brand, "domain": domain,
        "mention_rate": sum(r["brand_mentioned"] for r in results) / n,
        "citation_rate": sum(r["domain_cited"] for r in results) / n,
        "results": results,
    }
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "a") as f:
        f.write(json.dumps(summary) + "\n")
    return summary
