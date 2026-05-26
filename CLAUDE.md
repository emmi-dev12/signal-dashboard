# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
# Start locally (port 8765 by default)
python server.py

# Restart cleanly
pkill -f "signal-dashboard/server.py" 2>/dev/null; sleep 1
nohup python3 ~/signal-dashboard/server.py > ~/Library/Logs/signal-dashboard.log 2>&1 &

# Check logs
tail -50 ~/Library/Logs/signal-dashboard.log

# Force an immediate fetch cycle
curl -X POST http://localhost:8765/api/fetch-now
```

## Deploying

Render auto-deploys on every push to `main`:
```bash
git add -A && git commit -m "..." && git push
```
Render URL: `https://signal-dashboard-djgr.onrender.com`

## Architecture

Everything lives in two files:

**`server.py`** (~1650 lines) — monolith containing:
- Signal store: in-memory `signals` dict (keyed by `id`), persisted to `~/.signal-dashboard/signals.json`. Protected by `threading.Lock`.
- Scoring engine (`score_signal()`): deterministic, no AI. Starts at `base=3`, adds points for source tier (TIER1/TIER2 dicts), AI keywords, breaking keywords, engagement (HN/Reddit/GitHub stars), velocity, recency. Capped at 10.
- Clustering (`cluster_and_boost()`): Jaccard similarity on TF-IDF tokens. Items above threshold=0.28 are merged; highest-scored item becomes the representative with a `cross_source_boost` (+1 per extra source, max +3).
- Fetchers: `fetch_hn_algolia()`, `fetch_rss()` (uses `RSS_FEEDS` dict), `fetch_reddit()` (uses `SUBREDDITS` list), `fetch_github_trending()`, `fetch_arxiv()`. All upsert into `signals` dict via `store_signal()`.
- Scheduler: fast poll (HN+Reddit) every 10 min, full poll every 30 min, ArXiv hourly.
- Flask routes: `/` (dashboard HTML), `/api/signals`, `/api/stats`, `/api/weekly-brief`, `/api/fetch-now`, `/api/deeper-take/<id>`, `/api/v1/agent`.
- `DASHBOARD_HTML` string (~800 lines inline): complete single-page app with inline CSS and JS. No build step. Edit it directly by searching for class names or JS function names.

**`mcp_app.py`** — MCP SSE server exposing `get_signals` and `get_stats` tools. Mounted at `/mcp` by server.py via Starlette/uvicorn when those deps are present; falls back to plain Flask if not.

## Key locations in server.py

| What to change | Where |
|---|---|
| Add/remove RSS feeds | `RSS_FEEDS` dict ~line 302 |
| Add/remove subreddits | `SUBREDDITS` list ~line 324 |
| Scoring weights | `score_signal()` ~line 163 |
| Breaking/AI keyword lists | `BREAKING_KW`, `AI_KW` ~line 137 |
| Alert threshold | `breaking_threshold` in config (default 8) |
| Brand detection list | `KNOWN_BRANDS` ~line 257 |
| Dashboard HTML/CSS/JS | `DASHBOARD_HTML` string ~line 840 |

## Config

Local: `~/.signal-dashboard/config.json`  
Cloud: env vars (`ANTHROPIC_API_KEY`, `NTFY_TOPIC`, `SIGNAL_DATA_DIR`, `SIGNAL_HOST`, `PORT`/`SIGNAL_DASHBOARD_PORT`)

`ANTHROPIC_API_KEY` is optional — enables the "✦ Analysis" on-demand feature per signal (`/api/deeper-take/<id>`). All scoring/clustering is purely algorithmic otherwise.

## Domain labels

Signals are tagged with one of: `'AI & Tech'`, `'Apple'`, `'Geopolitics'`, `'Open Source'`. This drives both the grouped API response and the dashboard tabs. The label comes from the source's entry in `RSS_FEEDS`/`SUBREDDITS`, or from keyword-matching in `DOMAIN_KW` for HN items.

## Frontend notes

The frontend is a vanilla JS SPA inside `DASHBOARD_HTML`. Key patterns:
- `_data` / `_stats` are module-level globals populated by `loadAll()`, called on init and every 10 min.
- `renderAllPanes(data)` rebuilds all tab panes from `data.grouped` and `data.chrono`.
- Delegated click handling via `document.addEventListener('click', ...)` — all row interactions go through this single handler.
- Pins stored in `localStorage['pinnedSignals']` as a JSON array of signal IDs.
- Theme toggled via `data-theme` attribute on `<html>`, persisted in `localStorage`.
