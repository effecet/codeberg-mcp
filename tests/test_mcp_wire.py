"""What a real MCP client receives, end to end through MCPServer.

The other suites call tool functions directly, which skips the SDK's result
conversion. mcp 2.x withholds the text of most exceptions and validates
list/str results against an output schema, so the error contract only holds
if it is checked here, through an in-memory client.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.client import Client

import server


async def _call(respx_mock, tool: str, args: dict):
    # The client is opened inside the test, not in a fixture: anyio's cancel
    # scope must be entered and exited in the same task.
    respx_mock.get("/user").mock(
        return_value=httpx.Response(200, json={"login": "example"})
    )
    async with Client(server.mcp) as c:
        return await c.call_tool(tool, args)


def _text(result) -> str:
    return result.content[0].text


async def test_dict_tool_api_error_is_structured_result(fake_accounts, respx_mock):
    respx_mock.get("/repos/e/nope").mock(
        return_value=httpx.Response(404, json={"message": "repo not found"})
    )
    result = await _call(respx_mock, "get_repo", {"owner": "e", "repo": "nope"})
    assert not result.is_error
    assert json.loads(_text(result)) == {
        "error": "Codeberg API 404: repo not found",
        "status": 404,
    }


@pytest.mark.parametrize(
    "side_effect,status",
    [
        (httpx.Response(404, json={"message": "repo not found"}), 404),
        (httpx.ConnectError("All connection attempts failed"), None),
    ],
)
async def test_list_tool_error_reaches_client_as_json(
    fake_accounts, respx_mock, side_effect, status
):
    route = respx_mock.get("/repos/e/r/branches")
    if isinstance(side_effect, Exception):
        route.mock(side_effect=side_effect)
    else:
        route.mock(return_value=side_effect)
    result = await _call(respx_mock, "list_branches", {"owner": "e", "repo": "r"})
    assert result.is_error
    text = _text(result)
    error = json.loads(text[text.index("{") :])
    assert error["status"] == status
    assert error["error"]


async def test_usage_error_message_reaches_client(fake_accounts, respx_mock):
    result = await _call(
        respx_mock, "get_repo", {"owner": "e", "repo": "r", "account": "nobody"}
    )
    assert result.is_error
    assert "Unknown account 'nobody'" in _text(result)


async def test_programming_bug_stays_opaque(fake_accounts, respx_mock):
    # 200 with an unexpected shape raises KeyError inside the tool. That must
    # not be dressed up as an API error result.
    respx_mock.get("/repos/e/x").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    result = await _call(respx_mock, "get_repo", {"owner": "e", "repo": "x"})
    assert result.is_error
    assert _text(result) == "Error executing tool get_repo"


async def test_local_protocol_error_does_not_echo_token(mcp_client, respx_mock):
    respx_mock.get("/repos/e/x").mock(
        side_effect=httpx.LocalProtocolError(
            "Illegal header value b'token fake-token-example\\n'"
        )
    )
    result = await server.get_repo(owner="e", repo="x")
    assert result["status"] is None
    assert "fake-token-example" not in result["error"]


@pytest.mark.parametrize("token", ["tok\n", " tok ", "to\x00k", ""])
async def test_lifespan_rejects_or_strips_bad_tokens(monkeypatch, token):
    monkeypatch.setenv("CODEBERG_ACCOUNTS", json.dumps({"a": token}))
    monkeypatch.delenv("CODEBERG_DEFAULT_ACCOUNT", raising=False)
    if token.strip() and token.strip().isprintable():
        with pytest.MonkeyPatch.context() as m:
            m.setattr(server, "_validate_accounts", _noop)
            async with server._lifespan(server.mcp):
                assert server._accounts == {"a": token.strip()}
    else:
        with pytest.raises(RuntimeError, match="token for 'a'"):
            async with server._lifespan(server.mcp):
                pass


async def _noop() -> None:
    return None


async def test_decoding_error_is_an_unknown_outcome(mcp_client, respx_mock):
    # A body httpx cannot decode gives no usable answer either: for a write,
    # the server may have applied it, so it reports status null.
    respx_mock.get("/repos/e/x").mock(side_effect=httpx.DecodingError("bad gzip"))
    result = await server.get_repo(owner="e", repo="x")
    assert result["status"] is None
    assert "DecodingError" in result["error"]


async def test_write_tool_transport_error_has_null_status(
    mcp_client, respx_mock, tool_error
):
    respx_mock.put("/repos/e/r/actions/variables/X").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    error = await tool_error(
        server.update_actions_variable(owner="e", repo="r", name="X", value="v")
    )
    assert error["status"] is None
    assert "ReadTimeout" in error["error"]


async def test_list_tool_gateway_error_is_unknown_on_the_wire(
    fake_accounts, respx_mock
):
    respx_mock.get("/repos/e/r/branches").mock(
        return_value=httpx.Response(504, json={"message": "gateway timeout"})
    )
    result = await _call(respx_mock, "list_branches", {"owner": "e", "repo": "r"})
    assert result.is_error
    text = _text(result)
    error = json.loads(text[text.index("{") :])
    assert error["status"] is None
    assert "504" in error["error"]
