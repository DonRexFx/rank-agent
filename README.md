# Rank Agent (web app)

Mobile-friendly web app that audits a business site for Google and AI-search readiness, researches keywords and AI questions, rewrites pages, and tracks whether AI answers mention or cite the brand.

## Deploy free on Render (works from a phone browser)
1. Create a GitHub repo and upload these files, keeping this layout (`templates/index.html` must stay inside a `templates` folder; on GitHub mobile use Add file > Create new file and type `templates/index.html` as the name, then paste the contents).
2. On render.com: New > Blueprint (or Web Service) > pick the repo. It reads `render.yaml`.
3. When asked, set two environment variables:
   - `ANTHROPIC_API_KEY`: your key from console.anthropic.com
   - `APP_PASSWORD`: an access code you choose (people need it to use the app)
4. Open the URL Render gives you. Enter your access code and run an audit.

Notes: the free tier sleeps when idle (first load can take about 30 seconds). Jobs are held in memory, so keep the single worker setting.

## Run on a computer
```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... APP_PASSWORD=choose-a-code
python app.py        # http://localhost:5000
python test_app.py   # offline tests, no API key needed
```

## Safety built in
- Access code required; hourly per-IP limit; max 3 concurrent jobs.
- The server refuses private, loopback and internal addresses (including through redirects), so it can't be used to probe networks.
- Web page text is treated as data, not instructions, in the agent prompt.
- Each run uses your Anthropic credits (audits and research take several model calls), so keep the access code private.

## Files
`app.py` API and jobs, `agent_core.py` Claude tool loop, `tools.py` site analysis, `tracker.py` AI-visibility checks, `templates/index.html` the UI.
