"""Regression coverage for example/demo-repo#9 and adjacent reliability fixes.

- CodebergAPIError carries .status / .detail and subclasses RuntimeError
- catch_api_errors decorator turns API errors into structured tool results
  (the message reaches the caller instead of being eaten by MCPServer)
- _safe_list guards Forgejo's `null` (not `[]`) collection fields
- create_issue surfaces the smoke+PATCH workaround on oversized bodies
- the decorator preserves MCPServer's generated input schema
"""

from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest

import server


# ── CodebergAPIError ────────────────────────────────────────────────────────


def test_codeberg_api_error_is_runtimeerror_and_carries_fields():
    e = server.CodebergAPIError(404, "repo not found")
    assert isinstance(e, RuntimeError)  # keeps old _raise unit tests green
    assert e.status == 404
    assert e.detail == "repo not found"
    assert str(e) == "Codeberg API 404: repo not found"


def test_raise_raises_codeberg_api_error():
    resp = httpx.Response(
        500, json={"message": "boom"}, request=httpx.Request("GET", "https://x/")
    )
    with pytest.raises(server.CodebergAPIError) as ei:
        server._raise(resp)
    assert ei.value.status == 500
    assert "boom" in ei.value.detail


# ── catch_api_errors: structured result (the #9 fix) ────────────────────────


async def test_tool_returns_structured_error_instead_of_raising(mcp_client, respx_mock):
    respx_mock.get("/repos/example/nope").mock(
        return_value=httpx.Response(404, json={"message": "repo not found"})
    )
    result = await server.get_repo(owner="example", repo="nope")
    assert result == {
        "error": "Codeberg API 404: repo not found",
        "status": 404,
    }


async def test_non_api_exception_still_propagates(mcp_client, respx_mock):
    # 200 but a missing key inside the tool body → KeyError, NOT a
    # CodebergAPIError. Programming bugs must surface loudly, not be
    # masked as a structured "error" result.
    respx_mock.get("/repos/example/x").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    with pytest.raises(KeyError):
        await server.get_repo(owner="example", repo="x")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ReadTimeout("timed out"),
    ],
)
async def test_transport_error_returns_structured_error_with_null_status(
    mcp_client, respx_mock, exc
):
    # mcp 2.x hides the text of any exception a tool raises, so a network
    # failure would reach the caller as a bare "Error executing tool".
    # status is None: no HTTP answer, the outcome of a write is unknown.
    respx_mock.get("/repos/example/x").mock(side_effect=exc)
    result = await server.get_repo(owner="example", repo="x")
    assert result["status"] is None
    assert str(exc) in result["error"]


# ── _safe_list null guard ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [(None, []), ([], []), (["a"], ["a"]), ("str", []), (0, [])],
)
def test_safe_list(value, expected):
    assert server._safe_list(value) == expected


async def test_get_issue_handles_null_labels_and_assignees(mcp_client, respx_mock):
    # Forgejo returns null (not []) for empty collections — this used to
    # crash with "'NoneType' object is not iterable".
    respx_mock.get("/repos/example/demo-repo/issues/9").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 9,
                "title": "t",
                "body": "b",
                "state": "open",
                "labels": None,
                "assignees": None,
            },
        )
    )
    result = await server.get_issue(owner="example", repo="demo-repo", issue_number=9)
    assert result["labels"] == []
    assert result["assignees"] == []
    assert result["number"] == 9


async def test_list_issues_handles_null_labels(mcp_client, respx_mock):
    # Same null-collection class as get_issue, sibling site (server.py:1273).
    respx_mock.get("/repos/example/demo-repo/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "t",
                    "state": "open",
                    "labels": None,
                    "created_at": "",
                    "updated_at": "",
                    "html_url": "",
                }
            ],
        )
    )
    result = await server.list_issues(owner="example", repo="demo-repo")
    assert result[0]["labels"] == []


# ── create_issue body-size guard ────────────────────────────────────────────


async def test_create_issue_short_body_has_no_size_warning(mcp_client, respx_mock):
    respx_mock.post("/repos/example/r/issues").mock(
        return_value=httpx.Response(
            201, json={"number": 1, "title": "t", "state": "open"}
        )
    )
    result = await server.create_issue(owner="example", repo="r", title="t", body="hi")
    assert "size_warning" not in result


async def test_create_issue_oversized_body_annotates_success(mcp_client, respx_mock):
    respx_mock.post("/repos/example/r/issues").mock(
        return_value=httpx.Response(
            201, json={"number": 2, "title": "t", "state": "open"}
        )
    )
    big = "x" * (server._ISSUE_BODY_SOFT_LIMIT + 1)
    result = await server.create_issue(owner="example", repo="r", title="t", body=big)
    assert "size_warning" in result
    assert "smoke" not in result["size_warning"].lower()  # uses PATCH wording
    assert "PATCH" in result["size_warning"]


async def test_create_issue_oversized_failure_surfaces_workaround(
    mcp_client, respx_mock
):
    respx_mock.post("/repos/example/r/issues").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    big = "x" * (server._ISSUE_BODY_SOFT_LIMIT + 1)
    result = await server.create_issue(owner="example", repo="r", title="t", body=big)
    assert result["status"] == 500
    assert "Workaround" in result["error"]
    assert "PATCH" in result["error"]


# ── decorator preserves MCPServer schema ────────────────────────────────────


def test_decorator_preserves_signature_and_schema():
    sig = inspect.signature(server.get_issue)
    assert list(sig.parameters) == ["owner", "repo", "issue_number", "account"]

    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 48  # +1: edit_issue (demo-repo#13)
    gi = next(t for t in tools if t.name == "get_issue")
    assert set(gi.input_schema["properties"]) == {
        "owner",
        "repo",
        "issue_number",
        "account",
    }
    assert set(gi.input_schema["required"]) == {"owner", "repo", "issue_number"}
