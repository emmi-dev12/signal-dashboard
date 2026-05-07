"""
MCP server for the Signal Intelligence Dashboard.
Mounted at /mcp by server.py via DispatcherMiddleware.

Tools exposed:
  get_signals  — query the signal store (domain/min_score/limit/sort/q/since)
  get_stats    — summary counts and source breakdown
"""

import os
import requests
from fastmcp import FastMCP

_BASE = os.environ.get(
    'SIGNAL_API_BASE',
    'http://localhost:' + os.environ.get('PORT', '8765')
)

mcp = FastMCP(
    name='signal-dashboard',
    instructions=(
        'Real-time tech/AI intelligence feed. '
        'Use get_signals to fetch scored news items, '
        'get_stats for a quick summary.'
    ),
)


@mcp.tool()
def get_signals(
    domain: str = 'all',
    min_score: int = 1,
    limit: int = 20,
    sort: str = 'score',
    q: str = '',
    since: str = '',
) -> dict:
    """
    Fetch signals from the intelligence feed.

    Args:
        domain: 'all' | 'AI & Tech' | 'Apple' | 'Geopolitics' | 'Open Source'
        min_score: minimum signal score 1-10 (10 = most important)
        limit: max items to return (default 20, max 500)
        sort: 'score' (default) | 'time'
        q: keyword substring filter on title and summary
        since: ISO 8601 datetime — only return signals published after this
    """
    params = {'domain': domain, 'min_score': min_score,
              'limit': limit, 'sort': sort}
    if q:
        params['q'] = q
    if since:
        params['since'] = since
    r = requests.get(f'{_BASE}/api/v1/agent', params=params, timeout=10)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_stats() -> dict:
    """
    Return summary stats: total signals, source breakdown, top domains,
    breaking signals count, last updated time.
    """
    r = requests.get(f'{_BASE}/api/stats', timeout=10)
    r.raise_for_status()
    return r.json()


# ASGI app — mounted at /mcp by server.py
asgi_app = mcp.http_app(path='/')
