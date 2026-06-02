"""Shared fixtures for codeberg MCP tests.

Uses respx for httpx route mocking. Tests import server module-level tools
directly; this conftest sets up the minimum globals needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

# Make the server module importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@pytest.fixture
def fake_accounts(monkeypatch):
    """Stamp in fake Codeberg account tokens before server imports read env."""
    accounts = {"example": "fake-token-example", "work": "fake-token-fede"}
    monkeypatch.setenv("CODEBERG_ACCOUNTS", json.dumps(accounts))
    monkeypatch.setenv("CODEBERG_DEFAULT_ACCOUNT", "example")
    return accounts


@pytest.fixture
async def mcp_client(fake_accounts):
    """Initialize the shared httpx client + account globals the way _lifespan does.

    We avoid running the real _lifespan (which also wires FastMCP); instead we
    inject the module-level state test code needs.
    """
    server._accounts = fake_accounts
    server._default_account = "example"
    server._client = httpx.AsyncClient(
        base_url=server.BASE_URL,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    try:
        yield server._client
    finally:
        await server._client.aclose()
        server._client = None


@pytest.fixture
def respx_mock():
    """Plain respx mock that intercepts all httpx requests during a test."""
    with respx.mock(
        base_url="https://codeberg.org/api/v1", assert_all_called=False
    ) as mock:
        yield mock


# Enable pytest-asyncio's auto mode for async tests without per-test markers
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if item.get_closest_marker("asyncio") is None:
            import inspect

            if inspect.iscoroutinefunction(item.function):
                item.add_marker(pytest.mark.asyncio)
