"""Tests for ergonomic polish: _raise, pagination, startup validation, default branch."""

from __future__ import annotations

import logging

import httpx
import pytest

import server


def test_raise_surfaces_json_message():
    resp = httpx.Response(
        404,
        json={"message": "repo not found"},
        request=httpx.Request("GET", "https://x/"),
    )
    with pytest.raises(RuntimeError, match="404.*repo not found"):
        server._raise(resp)


def test_raise_falls_back_to_error_key():
    resp = httpx.Response(
        422,
        json={"error": "invalid payload"},
        request=httpx.Request("POST", "https://x/"),
    )
    with pytest.raises(RuntimeError, match="422.*invalid payload"):
        server._raise(resp)


def test_raise_truncates_non_json_response():
    long_html = "<html>" + "x" * 1000 + "</html>"
    resp = httpx.Response(
        500,
        text=long_html,
        request=httpx.Request("GET", "https://x/"),
    )
    with pytest.raises(RuntimeError) as exc_info:
        server._raise(resp)
    assert len(str(exc_info.value)) < 400  # 300 char cap + wrapper text
    assert "<html>" in str(exc_info.value)


# ── startup account validation ─────────────────────────────────────────────


async def test_validate_accounts_logs_live_for_ok(mcp_client, caplog, respx_mock):
    respx_mock.get("/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"})
    )
    caplog.set_level(logging.INFO, logger="codeberg-mcp")
    await server._validate_accounts()

    live_lines = [r.message for r in caplog.records if "live" in r.message]
    # Two accounts configured in the fixture — both should log "live"
    assert len(live_lines) == 2
    assert any("example" in m for m in live_lines)
    assert any("work" in m for m in live_lines)


async def test_validate_accounts_logs_failed_for_401(mcp_client, caplog, respx_mock):
    # Both accounts hit /user; first returns 401 (invalid token), second returns 200
    respx_mock.get("/user").mock(
        side_effect=[
            httpx.Response(401, json={"message": "unauthorized"}),
            httpx.Response(200, json={"login": "work"}),
        ]
    )
    caplog.set_level(logging.WARNING, logger="codeberg-mcp")
    await server._validate_accounts()

    warn_lines = [r.message for r in caplog.records if "FAILED" in r.message]
    assert len(warn_lines) == 1


# ── pagination all=True flag ────────────────────────────────────────────────


async def test_list_repos_all_sweeps_until_empty(mcp_client, respx_mock):
    respx_mock.get("/user/repos").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "full_name": "e/a",
                        "private": False,
                        "html_url": "",
                        "default_branch": "main",
                    },
                    {
                        "full_name": "e/b",
                        "private": False,
                        "html_url": "",
                        "default_branch": "main",
                    },
                ],
            ),
            httpx.Response(
                200,
                json=[
                    {
                        "full_name": "e/c",
                        "private": False,
                        "html_url": "",
                        "default_branch": "main",
                    },
                ],
            ),
            httpx.Response(200, json=[]),
        ]
    )
    result = await server.list_repos(all=True)
    assert [r["full_name"] for r in result] == ["e/a", "e/b", "e/c"]


async def test_list_repos_all_respects_cap(mcp_client, respx_mock, caplog):
    full_page = [
        {
            "full_name": f"e/r{i}",
            "private": False,
            "html_url": "",
            "default_branch": "main",
        }
        for i in range(50)
    ]
    respx_mock.get("/user/repos").mock(return_value=httpx.Response(200, json=full_page))
    caplog.set_level(logging.WARNING, logger="codeberg-mcp")
    result = await server.list_repos(all=True, limit=50)
    assert len(result) == 500
    assert any("stopped at 500" in r.message for r in caplog.records)


async def test_list_branches_all_sweeps(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/branches").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "name": "main",
                        "commit": {"id": "abc", "message": "msg"},
                        "protected": True,
                    },
                ],
            ),
            httpx.Response(200, json=[]),
        ]
    )
    result = await server.list_branches(owner="example", repo="demo-repo", all=True)
    assert [b["name"] for b in result] == ["main"]


async def test_list_pulls_all_sweeps(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/pulls").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "x",
                        "state": "open",
                        "user": {"login": "e"},
                        "head": {"label": "f"},
                        "base": {"label": "main"},
                        "html_url": "",
                        "created_at": "",
                        "updated_at": "",
                    },
                ],
            ),
            httpx.Response(200, json=[]),
        ]
    )
    result = await server.list_pulls(owner="example", repo="demo-repo", all=True)
    assert len(result) == 1
