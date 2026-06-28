# Connecting an AI agent to the Signal Dashboard

The dashboard is read-only as far as agents are concerned: it ingests, scores,
and clusters tech/AI signals, and exposes them so an agent can read the feed.
There are **two ways in** — pick based on what your agent speaks.

| Your agent speaks… | Use | Endpoint |
|---|---|---|
| **MCP** (Claude Desktop, Poke, MCP clients) | The MCP server | `/mcp/sse` |
| **Plain HTTP/JSON** (custom scripts, function-calling, cron) | The REST endpoint | `/api/v1/agent` |

Base URL in the cloud: `https://signal-dashboard-djgr.onrender.com`
Locally: `http://localhost:8765`

---

## Option A — MCP (recommended for Claude / Poke)

`mcp_app.py` runs an MCP SSE server that's mounted at `/mcp` by `server.py`
(only when `uvicorn`/`starlette`/`mcp` are installed — otherwise the app falls
back to plain Flask and only Option B is available).

Connect your MCP client to:

```
https://signal-dashboard-djgr.onrender.com/mcp/sse
```

It exposes two tools:

### `get_signals`
Fetch scored signals from the feed. All params optional:

| Param | Type | Default | Notes |
|---|---|---|---|
| `domain` | string | `all` | `all` · `AI & Tech` · `Apple` · `Geopolitics` · `Open Source` |
| `min_score` | int 1–10 | `1` | Minimum signal score (10 = most important) |
| `limit` | int | `20` | Max items, hard cap 500 |
| `sort` | string | `score` | `score` or `time` |
| `q` | string | — | Keyword filter on title + summary |
| `since` | ISO 8601 | — | Only signals published after this time |

### `get_stats`
Returns summary stats: totals, source breakdown, breaking count, last updated.
No params.

Under the hood both tools just proxy to the REST endpoints below, so the
response shapes are identical.

---

## Option B — REST endpoint (any agent / language)

A single read-only GET endpoint built for agents:

```
GET /api/v1/agent
```

### Query parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `domain` | string | `all` | `all` · `AI & Tech` · `Apple` · `Geopolitics` · `Open Source` |
| `min_score` | int 1–10 | `1` | Minimum score |
| `limit` | int | `50` | Hard cap 500 |
| `sort` | string | `score` | `score` (most important first) or `time` (newest first) |
| `q` | string | — | Case-insensitive substring match on title + summary |
| `since` | ISO 8601 | — | e.g. `2026-06-01T00:00:00Z`. Invalid format → `400` |

### Example calls

```bash
# Top 10 most important AI & Tech signals, score >= 7
curl "https://signal-dashboard-djgr.onrender.com/api/v1/agent?domain=AI%20%26%20Tech&min_score=7&limit=10"

# Anything mentioning "anthropic", newest first
curl "https://signal-dashboard-djgr.onrender.com/api/v1/agent?q=anthropic&sort=time"

# Everything since a timestamp
curl "https://signal-dashboard-djgr.onrender.com/api/v1/agent?since=2026-06-27T00:00:00Z"
```

### Response shape

```jsonc
{
  "_schema": {
    "endpoint": "/api/v1/agent",
    "params": ["domain", "min_score", "limit", "sort", "q", "since"],
    "domain_values": ["all", "AI & Tech", "Apple", "Geopolitics", "Open Source"],
    "sort_values": ["score", "time"],
    "score_range": "1-10 (10=most important)"
  },
  "meta": {
    "total_in_store": 412,      // signals currently held
    "returned": 10,             // after filters + limit
    "sort": "score",
    "filters": { "min_score": 7, "domain": "AI & Tech" },
    "as_of": "2026-06-28T12:00:00Z"
  },
  "signals": [
    {
      "id": "...",
      "title": "...",
      "url": "...",
      "source": "Hacker News",       // origin feed
      "domain": "AI & Tech",
      "score": 9,                     // 1-10
      "published": "2026-06-28T11:40:00Z",
      "summary": "...",
      "hn_score": 540,                // null if not from HN
      "reddit_score": null,
      "stars_today": null,            // GitHub Trending only
      "cluster_id": "..."             // shared by deduped cross-source items
    }
  ]
}
```

The `_schema` block is self-describing — an agent can read it on the first call
to learn the parameter space without this doc.

### Other endpoints an agent might use

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/stats` | GET | Summary counts + a 20-item recent ticker |
| `/api/signals` | GET | Full dashboard payload (clustered + grouped + chrono) — heavier than `/api/v1/agent` |
| `/api/weekly-brief` | GET | Generated weekly brief |
| `/api/deeper-take/<id>` | GET | On-demand Claude analysis of one signal. **Requires `ANTHROPIC_API_KEY`** to be set; returns `400` otherwise |
| `/api/fetch-now` | POST | Trigger an immediate fetch cycle |

---

## How scoring works (so your agent can trust the numbers)

Scoring and clustering are **fully deterministic — no AI involved**:

- `score_signal()` starts at a base of 3 and adds points for source tier, AI /
  breaking keywords, engagement (HN/Reddit/GitHub stars), velocity, and
  recency. Capped at 10.
- `cluster_and_boost()` dedupes near-identical stories across sources via
  Jaccard similarity. The highest-scored item becomes the representative and
  gets a `cross_source_boost` (+1 per extra source, max +3). Items sharing a
  story share a `cluster_id`.

So a high `score` means "ranked important by the engine," and a shared
`cluster_id` means "the same story showed up in multiple sources."

## Quick start checklist

1. **MCP agent:** point it at `…/mcp/sse`, then call `get_signals` / `get_stats`.
2. **HTTP agent:** GET `…/api/v1/agent`, read the `_schema` block, then filter
   with `domain` / `min_score` / `q` / `since`.
3. Poll on your own cadence — the backend refreshes on its own schedule (fast
   poll every 10 min, full poll every 30 min). No auth required for reads.
