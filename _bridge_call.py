"""Tiny CLI bridge: call a server tool by name with JSON kwargs.

Usage: python _bridge_call.py <tool_name> '<json-kwargs>'
Requires CODEBERG_ACCOUNTS in the environment (see .env.example).
"""

import asyncio
import json
import os
import sys

import httpx

import server

tool = sys.argv[1]
kwargs = json.loads(sys.argv[2])
# Only allow calling public tool functions (not private/internal helpers).
if tool.startswith("_") or not callable(getattr(server, tool, None)):
    raise SystemExit(f"unknown or non-callable tool: {tool!r}")
fn = getattr(server, tool)
server._accounts = json.loads(os.environ["CODEBERG_ACCOUNTS"])
server._default_account = os.environ.get("CODEBERG_DEFAULT_ACCOUNT") or next(
    iter(server._accounts)
)


async def main():
    server._client = httpx.AsyncClient(
        base_url=server.BASE_URL,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    try:
        print(json.dumps(await fn(**kwargs)))
    finally:
        await server._client.aclose()


asyncio.run(main())
