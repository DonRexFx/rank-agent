# Rank Agent (Gemini free-tier edition) - Phase 1: reliability and accuracy

A phone-friendly web app that produces client-ready reports to help a business get found on Google and in AI answers. Runs on Google's Gemini API free tier and Render's free hosting.

## Tools
| Tab | For | You get |
|---|---|---|
| **Launch** | Businesses with no website | Google Business Profile checklist, website plan, ready-to-paste copy, 30-day calendar |
| **Audit** | Businesses with a website | Up to 8 pages scored out of 100, top fixes, page rewrites, `llms.txt` and schema code |
| **Research** | Anyone | Keywords, questions people ask AI, competitors, content plan |
| **Rewrite** | One page | Titles, descriptions, headings, FAQ and schema code |
| **Track** | Proving results | Asks AI real customer questions with live search and shows whether the brand is mentioned or cited |

## What Phase 1 changed
- **Gemini errors**: Google's real status code and message are kept (API keys are stripped out). Temporary rate limits are retried only when a short wait can help, and `Retry-After` is respected. Daily/monthly quota problems, wrong model names and bad keys are never retried, so they no longer burn quota.
- **Cache**: repeat Track questions reuse a saved answer instead of calling Gemini again.
- **Accurate Track results**: each question ends as **Cited**, **Mentioned**, **Not found** or **Couldn't verify**. "Couldn't verify" is never shown or counted as "No" and is left out of the percentages. Brand matching is whole-word (no more "Acme" inside "Acmeron"), and a domain only counts if it is exactly the domain or a real subdomain of it (not `notacme.com`). Each result keeps its own sources and an evidence snippet.
- **Partial reports**: if one question fails, the other results are kept and the failed ones are listed with the reason. If nothing at all could be checked, you see the real error instead of an empty report.
- **Call budget**: a configurable cap on Gemini calls, retries included.
- Not changed: the web interface, the report layout (the Track table gains a "Result" column and an "Evidence" list), Launch, Audit, Research, Rewrite, scoring, crawling.

## Environment variables
No new packages (the cache uses Python's built-in SQLite). Your existing `GEMINI_API_KEY`, `APP_PASSWORD`, `REPORT_BRAND` and `GEMINI_MODEL` stay as they are. These new ones are all **optional**:

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_DAILY_CALL_LIMIT` | 100 | Max Gemini calls per UTC day for the whole server (0 = unlimited) |
| `GEMINI_RUN_CALL_LIMIT` | 15 | Max calls one report may make, retries included (0 = unlimited) |
| `GEMINI_MAX_RETRIES` | 2 | Retries after a temporary error |
| `GEMINI_MAX_RETRY_WAIT_SECONDS` | 30 | Longest wait worth retrying; longer waits fail immediately and say how long |
| `CACHE_TTL_SECONDS` | 86400 | How long a saved answer stays valid (0 = caching off) |
| `CACHE_PATH` | temp folder | Cache file path, or `:memory:` |
| `TRACK_PAUSE_SECONDS` | 4 | Pause between live Track requests |

## Deploy (from your phone)
1. **Make a rollback point first.** On GitHub, open your `rank-agent` repo, tap the branch button (`main`), type `backup-before-phase1`, and tap **Create branch**. Then switch back to `main`.
2. Extract the zip. In the repo's main page tap **Add file > Upload files** and upload: `gemini.py`, `tracker.py`, `agent_core.py`, `app.py`, `README.md`, `test_app.py` (these replace the old ones) and the **new** files `cache.py`, `budget.py`, `test_phase1.py`. Tap **Commit changes**.
3. Do **not** upload `index.html`, `tools.py`, `scoring.py`, `requirements.txt` or `Procfile`: they did not change.
4. Render redeploys automatically. When it shows **Live**, open `https://<your-app>.onrender.com/health` and check it says `"configured": true`.

**To roll back:** re-upload the files from `rank-agent-phase0-backup.zip`, or in GitHub switch to the `backup-before-phase1` branch and copy its files back. If your Render plan offers it, the Events page can also redeploy an earlier version.

## Limits you should know about
- **Cache on Render is temporary.** The cache is a file in the server's temporary folder. Render's free disk is wiped on every redeploy and restart, and a free service restarts after sleeping, so saved answers can disappear at any time. It saves calls while the server stays up; it is not permanent storage. If the file can't be used, it falls back to memory, which is lost the same way. Only complete, search-grounded answers are saved; failures and answers that did not use live search are never cached.
- **The cache key includes question, brand, domain, model and search settings.** The same question for a different brand is a separate entry and costs a separate call.
- **The call budget is a safety net, not Google's meter.** It counts this server's calls in memory, so it resets on restart, and Google's own free-tier limits are separate and can be stricter. If Google refuses a call, the message now says whether it looks like a per-minute limit (wait) or a daily/monthly quota (waiting won't help).
- **Matching is a careful heuristic, not mind-reading.** Brands that are common words (for example "Beacon") can match unrelated text. The check for "only named in a negative statement" uses a list of phrases and can miss unusual wording. AI answers vary from run to run, so treat a run as a snapshot.
- Only the Track tab uses the cache. Launch, Research, Audit and Rewrite still call Gemini every time.

## Run locally / tests
```
pip install -r requirements.txt
export GEMINI_API_KEY=... APP_PASSWORD=choose-a-code
python app.py                      # http://localhost:5000
python -m unittest -v test_phase1  # Phase 1 tests (no key or network needed)
python test_app.py                 # end-to-end test with a fake website
```
