# Rank Agent (Gemini free-tier edition)

A phone-friendly web app that produces client-ready reports to help a business get found on Google and in AI answers (ChatGPT, Gemini, Perplexity, Google AI Overviews). Runs on free services: Google Gemini API free tier + Render free hosting.

## The five tools
| Tab | For | You get |
|---|---|---|
| **Launch** | Businesses with NO website | Google Business Profile checklist, website plan, ready-to-paste copy, 12 customer questions, review plan, 30-day calendar |
| **Audit** | Businesses with a website | Reads up to 8 pages, scores 5 areas out of 100, top 10 fixes, page-by-page rewrites, 30-day plan, plus a ready `llms.txt` and business schema code |
| **Research** | Anyone | Keywords, questions people ask AI, competitors, 8-page content plan (uses Google Search) |
| **Rewrite** | One page | Titles, meta descriptions, headings, AI-quotable summary, FAQ and schema code |
| **Track** | Proving results | Asks AI real customer questions, shows if the brand is mentioned and which competitor sites the AI relies on |

Scores are calculated by fixed rules (not by AI), so they are consistent and you can re-run them later to show progress. Tap **Save as PDF** on any report to send it to a client.

## 1. Get a free Gemini key
Go to https://aistudio.google.com/apikey, sign in with Google, create a key. No card needed for the free tier. Free tiers have daily and per-minute limits that Google can change; if you hit them the app shows a friendly message, and you can try again later.

## 2. Update your GitHub repo (from your phone)
Upload these files into the repo root, using the same names (GitHub replaces the old ones):
`app.py, agent_core.py, tools.py, tracker.py, requirements.txt, render.yaml, README.md, test_app.py`
and the NEW files: `gemini.py, scoring.py`.

Then open the `templates` folder in the repo, tap **Add file > Upload files**, and upload the new `index.html` to replace the old one. `Procfile` stays as it is. Tap **Commit changes** each time.

## 3. Deploy on Render
Render > New > Blueprint (or Web Service) > pick the repo. Set these environment variables:
- `GEMINI_API_KEY`: the key from step 1
- `APP_PASSWORD`: an access code you choose
- `REPORT_BRAND`: your business name, printed on every report (optional)
- `GEMINI_MODEL`: optional. Defaults to `gemini-2.5-flash`. If Google retires that name, set the current Flash model name from AI Studio.

If you already deployed, just change the variables (remove `ANTHROPIC_API_KEY`, add the ones above) and Render redeploys when you commit.

## Safety built in
Access code on every request, per-IP hourly limit, max 2 concurrent jobs, internal/private network addresses blocked (including via redirects), web text treated as data rather than instructions, and the AI is told never to invent facts (it marks unknowns `[CONFIRM: ...]`).

## Running on a computer
```
pip install -r requirements.txt
export GEMINI_API_KEY=... APP_PASSWORD=choose-a-code
python app.py          # http://localhost:5000
python test_app.py     # offline tests, no key needed
```
