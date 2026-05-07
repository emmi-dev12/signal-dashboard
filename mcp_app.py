"""
MCP SSE server for the Signal Intelligence Dashboard.
Mounted at /mcp by server.py — Poke connects to /mcp/sse.

Uses the low-level mcp SDK so SSE transport is explicit.
"""

import os
import requests as _requests
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount

_BASE = os.environ.get(
    'SIGNAL_API_BASE',
    'http://localhost:' + os.environ.get('PORT', '8765')
)

server = Server('signal-dashboard')

TOOLS = [
    Tool(
        name='get_signals',
        description=(
            'Fetch scored signals from the real-time tech/AI intelligence feed. '
            'Returns news, papers, GitHub repos and Reddit posts ranked 1-10.'
        ),
        inputSchema={
            'type': 'object',
            'properties': {
                'domain':    {'type': 'string', 'enum': ['all', 'AI & Tech', 'Apple', 'Geopolitics', 'Open Source'], 'default': 'all'},
                'min_score': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 1},
                'limit':     {'type': 'integer', 'default': 20, 'maximum': 500},
                'sort':      {'type': 'string', 'enum': ['score', 'time'], 'default': 'score'},
                'q':         {'type': 'string', 'description': 'Keyword filter on title and summary'},
                'since':     {'type': 'string', 'description': 'ISO 8601 datetime — only signals after this'},
            },
        },
    ),
    Tool(
        name='get_stats',
        description='Return summary stats: total signals, source breakdown, breaking count, last updated.',
        inputSchema={'type': 'object', 'properties': {}},
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == 'get_signals':
        params = {k: v for k, v in arguments.items() if v not in (None, '', [])}
        r = _requests.get(f'{_BASE}/api/v1/agent', params=params, timeout=10)
        r.raise_for_status()
        return [TextContent(type='text', text=r.text)]

    if name == 'get_stats':
        r = _requests.get(f'{_BASE}/api/stats', timeout=10)
        r.raise_for_status()
        return [TextContent(type='text', text=r.text)]

    raise ValueError(f'Unknown tool: {name}')


# Build the ASGI app with SSE transport
# Poke connects to /mcp/sse (because this app is mounted at /mcp in server.py)
sse = SseServerTransport('/mcp/messages/')

async def _handle_sse(scope, receive, send):
    async with sse.connect_sse(scope, receive, send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def _handle_messages(scope, receive, send):
    await sse.handle_post_message(scope, receive, send)

asgi_app = Starlette(routes=[
    Route('/sse',       _handle_sse),
    Route('/messages/', _handle_messages, methods=['POST']),
])
