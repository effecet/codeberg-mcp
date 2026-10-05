"""Write recovery and the unknown-outcome error contract.

- A lost response (timeout, truncated body) or gateway error after a write
  was sent means "unknown", not "failed": create_pull / merge_pull re-check
  the real state and report it, instead of inviting a retry that
  double-applies. If the re-check cannot settle it, the result still says
  "unknown" (status None) — never an error that reads as a definite failure.
- httpx transport errors stringify to '' — they used to surface as
  `Error executing tool X: ` with nothing after it. They are now a
  structured result naming the exception class.
"""

from __future__ import annotations

import json

import httpx
import pytest

import server

PULLS = "/repos/e/r/pulls"
MERGE = f"{PULLS}/7/merge"
# Captured before the autouse fixture zeroes it for speed.
PRODUCTION_MERGE_RECHECK_DELAYS = server._MERGE_RECHECK_DELAYS


@pytest.fixture(autouse=True)
def no_merge_recheck_wait(monkeypatch):
    monkeypatch.setattr(server, "_MERGE_RECHECK_DELAYS", (0.0, 0.0, 0.0))


def _pr(
    number: int = 12,
    head: str = "feat",
    base: str = "main",
    state: str = "open",
    merged: bool = False,
    head_repo: str = "e/r",
) -> dict:
    return {
        "number": number,
        "title": "t",
        "state": state,
        "html_url": f"https://codeberg.org/e/r/pulls/{number}",
        "head": {"ref": head, "label": head, "repo": {"full_name": head_repo}},
        "base": {"ref": base, "label": base},
        "user": {"login": "effece"},
        "merged": merged,
    }


def _open_prs(*pages: list[dict]):
    """A GET /pulls side effect serving `pages` by ?page=, then empty."""

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])

    return respond


async def _create(**overrides):
    args = {"owner": "e", "repo": "r", "title": "t", "head": "feat", "base": "main"}
    return await server.create_pull(**{**args, **overrides})


# ── errors surface with a message ───────────────────────────────────────────


async def test_transport_error_on_read_is_structured_not_empty(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r").mock(side_effect=httpx.ReadTimeout(""))
    result = await server.get_repo(owner="e", repo="r")
    assert result["status"] is None
    assert "ReadTimeout" in result["error"]
    assert "may have been applied" in result["error"]


async def test_truncated_body_on_any_tool_is_structured(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r").mock(
        return_value=httpx.Response(200, content=b'{"full_name": "e/r", "desc')
    )
    result = await server.get_repo(owner="e", repo="r")
    assert result["status"] is None
    assert "JSONDecodeError" in result["error"]


@pytest.mark.parametrize("code", [502, 503, 504])
async def test_gateway_error_on_any_tool_is_unknown_not_definite(
    mcp_client, respx_mock, code
):
    # create_issue has no re-check, but a 504 still must not read as "failed".
    respx_mock.post("/repos/e/r/issues").mock(
        return_value=httpx.Response(code, json={"message": "gateway timeout"})
    )
    result = await server.create_issue(owner="e", repo="r", title="t")
    assert result["status"] is None
    assert str(code) in result["error"]
    assert "may have been applied" in result["error"]


# ── create_pull ─────────────────────────────────────────────────────────────


async def test_create_pull_recovers_when_pr_exists_after_read_error(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(
        side_effect=httpx.RemoteProtocolError("incomplete chunked read")
    )
    listing = respx_mock.get(PULLS).mock(
        side_effect=_open_prs([_pr(11, head="other"), _pr(12)])
    )
    result = await _create()
    assert listing.called
    assert result["number"] == 12
    assert result["recovered_after_read_error"] is True
    assert "RemoteProtocolError" in result["read_error"]


async def test_create_pull_recovery_searches_past_the_first_page(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    first_page = [_pr(n, head=f"b{n}") for n in range(50)]
    respx_mock.get(PULLS).mock(side_effect=_open_prs(first_page, [_pr(99)]))
    result = await _create()
    assert result["number"] == 99
    assert result["recovered_after_read_error"] is True


async def test_create_pull_recovery_matches_branch_names_with_slashes(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(PULLS).mock(
        side_effect=_open_prs(
            [
                _pr(11, head="feature/x", base="main"),
                _pr(12, head="feature/x", base="release/1.0"),
            ]
        )
    )
    result = await _create(head="feature/x", base="release/1.0")
    assert result["number"] == 12


async def test_create_pull_recovery_ignores_a_pr_that_is_not_open(
    mcp_client, respx_mock
):
    # A reused branch name: an old merged PR must not read as "created".
    respx_mock.post(PULLS).mock(side_effect=httpx.ConnectError(""))
    respx_mock.get(PULLS).mock(
        side_effect=_open_prs([_pr(5, state="closed", merged=True)])
    )
    result = await _create()
    assert result["status"] is None
    assert "recovered_after_read_error" not in result


async def test_create_pull_recovery_ignores_a_forks_same_named_branch(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    fork = _pr(40, head_repo="someone/r")
    fork["head"]["label"] = "someone:feat"
    respx_mock.get(PULLS).mock(side_effect=_open_prs([fork]))
    result = await _create()
    assert result["status"] is None
    assert "recovered_after_read_error" not in result


async def test_create_pull_recovery_survives_a_deleted_fork(mcp_client, respx_mock):
    # Forgejo reports head.repo as null once a fork is deleted.
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    orphan = _pr(39)
    orphan["head"]["repo"] = None
    respx_mock.get(PULLS).mock(
        side_effect=_open_prs([orphan, _pr(12, head_repo="E/R")])
    )
    result = await _create()
    assert result["number"] == 12  # also: owner/repo match ignores case


async def test_create_pull_recovery_matches_a_fork_head_by_label(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    fork = _pr(41, head_repo="someone/r")
    fork["head"]["label"] = "someone:feat"
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(40), fork]))
    result = await _create(head="someone:feat")
    assert result["number"] == 41


async def test_create_pull_reports_unknown_when_no_pr_after_read_error(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(11, head="other")]))
    result = await _create()
    assert result["status"] is None
    assert "ReadTimeout" in result["error"]
    assert "did not find" in result["error"]
    assert "recovered_after_read_error" not in result


async def test_create_pull_recovers_from_truncated_json_body(mcp_client, respx_mock):
    # 201 whose body was cut off mid-stream: the PR exists, the parse fails.
    respx_mock.post(PULLS).mock(
        return_value=httpx.Response(201, content=b'{"number": 12, "tit')
    )
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(12)]))
    result = await _create()
    assert result["number"] == 12
    assert result["recovered_after_read_error"] is True


async def test_create_pull_rechecks_after_gateway_error(mcp_client, respx_mock):
    # A 504 from the proxy can hide a write that landed (issues #28/#29).
    respx_mock.post(PULLS).mock(
        return_value=httpx.Response(504, json={"message": "gateway timeout"})
    )
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(12)]))
    result = await _create()
    assert result["number"] == 12
    assert result["recovered_after_read_error"] is True


async def test_create_pull_stays_unknown_when_recheck_fails(mcp_client, respx_mock):
    # The re-check's own error must not replace "unknown" with a status that
    # reads as a definite failure — that would invite the double-apply.
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(PULLS).mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )
    result = await _create()
    assert result["status"] is None
    assert "ReadTimeout" in result["error"]
    assert "Re-checking also failed" in result["error"]


async def test_create_pull_stays_unknown_when_recheck_times_out(mcp_client, respx_mock):
    respx_mock.post(PULLS).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(PULLS).mock(side_effect=httpx.ConnectError(""))
    result = await _create()
    assert result["status"] is None
    assert "ReadTimeout" in result["error"]
    assert "ConnectError" in result["error"]


async def test_create_pull_api_error_is_not_rechecked(mcp_client, respx_mock):
    # A real 409 is a definite answer — no recovery lookup, no masking.
    respx_mock.post(PULLS).mock(
        return_value=httpx.Response(
            409, json={"message": "pull request already exists"}
        )
    )
    listing = respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(12)]))
    result = await _create()
    assert result == {
        "error": "Codeberg API 409: pull request already exists",
        "status": 409,
    }
    assert not listing.called


# ── merge_pull ──────────────────────────────────────────────────────────────


async def test_merge_pull_recovers_when_merged_after_read_error(mcp_client, respx_mock):
    respx_mock.post(MERGE).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7, merged=True))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7, method="squash")
    assert result["merged"] is True
    assert result["method"] == "squash"
    assert result["recovered_after_read_error"] is True


async def test_merge_pull_waits_out_a_stale_merged_flag(mcp_client, respx_mock):
    # Codeberg can report merged:false for a while after a merge that landed.
    respx_mock.post(MERGE).mock(side_effect=httpx.ReadTimeout(""))
    reads = respx_mock.get(f"{PULLS}/7").mock(
        side_effect=[
            httpx.Response(200, json=_pr(7)),
            httpx.Response(200, json=_pr(7)),
            httpx.Response(200, json=_pr(7, merged=True)),
        ]
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["recovered_after_read_error"] is True
    assert reads.call_count == 3


async def test_merge_pull_empty_405_is_rechecked_as_already_merged(
    mcp_client, respx_mock
):
    respx_mock.post(MERGE).mock(
        return_value=httpx.Response(
            405, json={"message": "", "url": "https://codeberg.org/api/swagger"}
        )
    )
    respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7, merged=True))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["merged"] is True
    assert result["recovered_after_read_error"] is True


async def test_merge_pull_empty_405_on_unmerged_pr_is_unknown_not_refused(
    mcp_client, respx_mock
):
    respx_mock.post(MERGE).mock(return_value=httpx.Response(405, json={"message": ""}))
    reads = respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["status"] is None
    assert reads.call_count == len(server._MERGE_RECHECK_DELAYS)


@pytest.mark.parametrize("body", [b"<html>Method Not Allowed</html>", b'["x"]'])
async def test_merge_pull_405_without_a_message_object_is_rechecked(
    mcp_client, respx_mock, body
):
    respx_mock.post(MERGE).mock(return_value=httpx.Response(405, content=body))
    respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7, merged=True))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["recovered_after_read_error"] is True


async def test_merge_pull_descriptive_405_is_not_rechecked(mcp_client, respx_mock):
    respx_mock.post(MERGE).mock(
        return_value=httpx.Response(
            405, json={"message": "Not all required status checks successful"}
        )
    )
    reads = respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7, merged=True))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result == {
        "error": "Codeberg API 405: Not all required status checks successful",
        "status": 405,
    }
    assert not reads.called


async def test_merge_pull_reports_unknown_when_not_merged_after_read_error(
    mcp_client, respx_mock
):
    respx_mock.post(MERGE).mock(side_effect=httpx.ReadTimeout(""))
    reads = respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json=_pr(7))
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["status"] is None
    assert "ReadTimeout" in result["error"]
    assert reads.call_count == len(server._MERGE_RECHECK_DELAYS)


async def test_merge_pull_stays_unknown_when_recheck_fails(mcp_client, respx_mock):
    respx_mock.post(MERGE).mock(side_effect=httpx.ReadTimeout(""))
    respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(404, json={"message": "nf"})
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["status"] is None
    assert "Re-checking also failed" in result["error"]


def test_merge_recheck_outlasts_a_single_read():
    # One immediate read is the stale read that caused the 2026-06-05 and
    # 2026-08-31 double-merge attempts; several seconds of re-reads is the floor.
    assert len(PRODUCTION_MERGE_RECHECK_DELAYS) >= 2
    assert sum(PRODUCTION_MERGE_RECHECK_DELAYS) >= 5


# ── token never echoed ──────────────────────────────────────────────────────

_LEAKY = "Illegal header value b'token fake-token-example\\n'"


async def test_create_pull_recheck_failure_does_not_echo_token(mcp_client, respx_mock):
    # The re-check sends the same bad header, so both errors would carry it.
    respx_mock.post(PULLS).mock(side_effect=httpx.LocalProtocolError(_LEAKY))
    respx_mock.get(PULLS).mock(side_effect=httpx.LocalProtocolError(_LEAKY))
    result = await _create()
    assert result["status"] is None
    assert "Re-checking also failed" in result["error"]
    assert "fake-token-example" not in result["error"]


async def test_create_pull_recovered_read_error_does_not_echo_token(
    mcp_client, respx_mock
):
    respx_mock.post(PULLS).mock(side_effect=httpx.LocalProtocolError(_LEAKY))
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(12)]))
    result = await _create()
    assert result["recovered_after_read_error"] is True
    assert "LocalProtocolError" in result["read_error"]
    assert "fake-token-example" not in result["read_error"]


async def test_merge_pull_sends_merge_title_and_message(mcp_client, respx_mock):
    merge = respx_mock.post(MERGE).mock(return_value=httpx.Response(200))
    await server.merge_pull(
        owner="e",
        repo="r",
        index=7,
        method="squash",
        merge_title="T",
        merge_message="M",
    )
    sent = json.loads(merge.calls.last.request.content)
    assert sent["MergeTitleField"] == "T"
    assert sent["MergeMessageField"] == "M"
    assert sent["do"] == "squash"


async def test_merge_pull_omits_unset_title_and_message(mcp_client, respx_mock):
    # A null MergeTitleField could replace Forgejo's default commit title.
    merge = respx_mock.post(MERGE).mock(return_value=httpx.Response(200))
    await server.merge_pull(owner="e", repo="r", index=7)
    sent = json.loads(merge.calls.last.request.content)
    assert "MergeTitleField" not in sent
    assert "MergeMessageField" not in sent


async def test_merge_recheck_waits_the_production_delays(
    mcp_client, respx_mock, monkeypatch
):
    # The autouse fixture zeroes the delays for speed; restore them and record
    # the sleeps instead, so the "re-read over a few seconds" fix is pinned.
    monkeypatch.setattr(
        server, "_MERGE_RECHECK_DELAYS", PRODUCTION_MERGE_RECHECK_DELAYS
    )
    slept = []

    async def record(delay):
        slept.append(delay)

    monkeypatch.setattr(server.asyncio, "sleep", record)
    respx_mock.post(MERGE).mock(side_effect=httpx.ReadTimeout(""))
    reads = respx_mock.get(f"{PULLS}/7").mock(
        return_value=httpx.Response(200, json={"merged": False})
    )
    result = await server.merge_pull(owner="e", repo="r", index=7)
    assert result["status"] is None
    assert slept == list(PRODUCTION_MERGE_RECHECK_DELAYS)
    assert reads.call_count == len(PRODUCTION_MERGE_RECHECK_DELAYS)
    timeout = reads.calls.last.request.extensions["timeout"]
    assert timeout["read"] == server._RECHECK_TIMEOUT


async def test_non_utf8_body_after_create_pull_is_rechecked(mcp_client, respx_mock):
    respx_mock.post(PULLS).mock(
        return_value=httpx.Response(
            201, content="<html>Passerelle dépassée</html>".encode("latin-1")
        )
    )
    respx_mock.get(PULLS).mock(side_effect=_open_prs([_pr(12)]))
    result = await _create()
    assert result["number"] == 12
    assert result["recovered_after_read_error"] is True
    assert "UnicodeDecodeError" in result["read_error"]
