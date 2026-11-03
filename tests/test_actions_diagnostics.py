"""Actions diagnostics: the run projection, list_runners and list_queued_jobs,
plus merge_pull's squash title/message.

Payload shapes are taken from live API responses (2026-10-01) and the
swagger `ActionRunner` / `ActionRunJob` / `MergePullRequestOption` schemas.
"""

from __future__ import annotations

import json as json_lib
import os
import time

import httpx
import pytest
import server

RUNS = "/repos/example/demo-repo/actions/runs"


def _live_run(**overrides) -> dict:
    # Trimmed from a real /actions/runs row: no head_branch, no run_number,
    # duration in nanoseconds (Go time.Duration).
    run = {
        "id": 7213654,
        "title": "[ci] feat: example (#1)",
        "workflow_id": "smoke.yml",
        "index_in_repo": 41,
        "trigger_user": {"id": 1, "login": "example"},
        "prettyref": "main",
        "commit_sha": "43ee6f4e",
        "need_approval": False,
        "approved_by": 0,
        "event": "push",
        "status": "success",
        "started": "2026-09-22T15:49:02+02:00",
        "stopped": "2026-09-22T15:52:44+02:00",
        "created": "2026-09-22T15:48:53+02:00",
        "updated": "2026-09-22T15:52:44+02:00",
        "duration": 222_000_000_000,
        "html_url": "https://codeberg.org/example/demo-repo/actions/runs/41",
    }
    run.update(overrides)
    return run


async def _list_run(respx_mock, run: dict) -> dict:
    respx_mock.get(RUNS).mock(
        return_value=httpx.Response(200, json={"workflow_runs": [run]})
    )
    [projected] = await server.list_workflow_runs(owner="example", repo="demo-repo")
    return projected


_PROJECTED = {
    "id": 7213654,
    "name": "smoke.yml",
    "title": "[ci] feat: example (#1)",
    "status": "success",
    "conclusion": "",
    # /actions/runs has no head_branch / run_number; both were always empty.
    "head_branch": "main",
    "head_sha": "43ee6f4e",
    "run_number": 41,
    "event": "push",
    "trigger_user": "example",
    "created": "2026-09-22T15:48:53+02:00",
    "started": "2026-09-22T15:49:02+02:00",
    "stopped": "2026-09-22T15:52:44+02:00",
    "duration_s": 222,
    "updated": "2026-09-22T15:52:44+02:00",
    "need_approval": False,
    "approved_by": 0,
    "url": "https://codeberg.org/example/demo-repo/actions/runs/41",
}


# ── list_workflow_runs ──────────────────────────────────────────────────────


async def test_list_workflow_runs_projects_a_live_runs_row(mcp_client, respx_mock):
    assert await _list_run(respx_mock, _live_run()) == _PROJECTED


@pytest.mark.parametrize(
    ("overrides", "expected_s"),
    [
        ({"duration": 0}, None),
        ({"duration": None}, None),
        ({"duration": 6_600_000_000}, 7),
        # Still running: elapsed time so far is not a finished duration.
        ({"status": "running", "stopped": None}, None),
    ],
    ids=["zero", "null", "rounds-subsecond", "not-stopped"],
)
async def test_list_workflow_runs_duration_s(
    mcp_client, respx_mock, overrides, expected_s
):
    run = await _list_run(respx_mock, _live_run(**overrides))
    assert run["duration_s"] == expected_s


async def test_list_workflow_runs_epoch_times_mean_not_yet(mcp_client, respx_mock):
    # Live 2026-10-01: a run cancelled while queued reports the Unix epoch.
    epoch = "1970-01-01T01:00:00+01:00"
    run = await _list_run(
        respx_mock,
        _live_run(status="cancelled", started=epoch, stopped=epoch, duration=None),
    )
    assert (run["started"], run["stopped"], run["duration_s"]) == (None, None, None)


@pytest.mark.parametrize("tz", ["Etc/GMT+4", "Etc/GMT-9"])
async def test_list_workflow_runs_naive_epoch_is_utc(mcp_client, respx_mock, tz):
    # A timestamp without an offset is UTC, so the epoch reads as "not yet"
    # whatever this machine's timezone is.
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        run = await _list_run(
            respx_mock, _live_run(started="1970-01-01T00:00:00", duration=None)
        )
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert run["started"] is None


async def test_list_workflow_runs_null_trigger_user(mcp_client, respx_mock):
    run = await _list_run(respx_mock, _live_run(trigger_user=None))
    assert run["trigger_user"] == ""


async def test_list_workflow_runs_pull_request_ref(mcp_client, respx_mock):
    # Live 2026-10-01: prettyref is "#111" for a pull_request run.
    run = await _list_run(respx_mock, _live_run(event="pull_request", prettyref="#111"))
    assert run["head_branch"] == "#111"


async def test_list_workflow_runs_always_sends_page(mcp_client, respx_mock):
    # The API ignores `limit` unless `page` is also sent.
    route = respx_mock.get(RUNS).mock(
        return_value=httpx.Response(200, json={"workflow_runs": []})
    )
    await server.list_workflow_runs(owner="example", repo="demo-repo", limit=5)
    assert dict(route.calls.last.request.url.params) == {"limit": "5", "page": "1"}


async def test_list_workflow_runs_keeps_an_unparseable_time(mcp_client, respx_mock):
    run = await _list_run(respx_mock, _live_run(started="garbage"))
    assert run["started"] == "garbage"


async def test_get_workflow_run_url_falls_back_to_api_url(mcp_client, respx_mock):
    raw = _live_run(url="https://codeberg.org/api/v1/repos/x/runs/1")
    del raw["html_url"]
    respx_mock.get(f"{RUNS}/7213654").mock(return_value=httpx.Response(200, json=raw))
    run = await server.get_workflow_run(
        owner="example", repo="demo-repo", run_id=7213654
    )
    assert run["url"] == "https://codeberg.org/api/v1/repos/x/runs/1"


async def test_get_workflow_run_shares_the_list_projection(mcp_client, respx_mock):
    # get_workflow_run used to keep its own mapping and still read the
    # tasks-endpoint keys, so its branch was "" and its run number 0.
    respx_mock.get(f"{RUNS}/7213654").mock(
        return_value=httpx.Response(200, json=_live_run())
    )
    run = await server.get_workflow_run(
        owner="example", repo="demo-repo", run_id=7213654
    )
    assert {k: run[k] for k in _PROJECTED} == _PROJECTED
    assert {
        k: run[k] for k in ("workflow_id", "display_title", "created_at", "updated_at")
    } == {
        "workflow_id": "smoke.yml",
        "display_title": _PROJECTED["title"],
        "created_at": _PROJECTED["created"],
        "updated_at": _PROJECTED["updated"],
    }


# ── list_runners ────────────────────────────────────────────────────────────

_RUNNER = {
    "id": 470033,
    "uuid": "cb46d22d",
    "owner_id": 645603,
    "repo_id": 0,
    "name": "example-runner",
    "status": "idle",
    "version": "v12.10.0",
    "labels": ["self-hosted-arm64"],
    "description": "",
    "ephemeral": False,
}
_RUNNER_KEYS = {"id", "name", "status", "labels", "version", "ephemeral"}


async def test_list_runners_defaults_to_user_scope(mcp_client, respx_mock):
    route = respx_mock.get("/user/actions/runners").mock(
        return_value=httpx.Response(200, json=[_RUNNER])
    )
    result = await server.list_runners()
    assert dict(route.calls.last.request.url.params) == {"limit": "50", "page": "1"}
    assert result == [{k: _RUNNER[k] for k in _RUNNER_KEYS}]


async def test_list_runners_repo_scope(mcp_client, respx_mock):
    route = respx_mock.get("/repos/example/demo-repo/actions/runners").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert await server.list_runners(owner="example", repo="demo-repo") == []
    assert route.called


async def test_list_runners_api_error_is_structured(mcp_client, respx_mock, tool_error):
    respx_mock.get("/user/actions/runners").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    result = await tool_error(server.list_runners())
    assert result == {"error": "Codeberg API 403: forbidden", "status": 403}


@pytest.mark.parametrize("half", [{"owner": "example"}, {"repo": "demo-repo"}])
async def test_list_runners_rejects_half_a_repo(mcp_client, respx_mock, half):
    with pytest.raises(server.ToolUsageError, match="owner and repo"):
        await server.list_runners(**half)


# ── list_queued_jobs ────────────────────────────────────────────────────────

JOBS = "/repos/example/demo-repo/actions/runners/jobs"


async def test_list_queued_jobs_projects_attempt_and_runs_on(mcp_client, respx_mock):
    job = {
        "id": 9,
        "repo_id": 1,
        "owner_id": 1,
        "name": "smoke",
        "needs": [],
        "runs_on": ["self-hosted-arm64"],
        "task_id": 0,
        "status": "waiting",
        "run_id": 7213654,
        "attempt": 2,
        "handle": "h",
    }
    respx_mock.get(JOBS).mock(return_value=httpx.Response(200, json=[job]))
    result = await server.list_queued_jobs(owner="example", repo="demo-repo")
    assert result == [
        {
            "id": 9,
            "name": "smoke",
            "run_id": 7213654,
            "attempt": 2,
            "status": "waiting",
            "runs_on": ["self-hosted-arm64"],
        }
    ]


async def test_list_queued_jobs_empty_queue_is_null(mcp_client, respx_mock):
    # The API answers an empty queue with a literal `null`, not `[]`.
    respx_mock.get(JOBS).mock(return_value=httpx.Response(200, text="null"))
    assert await server.list_queued_jobs(owner="example", repo="demo-repo") == []


async def test_list_queued_jobs_transport_error_is_unknown(
    mcp_client, respx_mock, tool_error
):
    respx_mock.get(JOBS).mock(side_effect=httpx.ConnectError("refused"))
    result = await tool_error(
        server.list_queued_jobs(owner="example", repo="demo-repo")
    )
    assert result["status"] is None
    assert "ConnectError" in result["error"]


async def test_list_queued_jobs_passes_label_filter(mcp_client, respx_mock):
    route = respx_mock.get(JOBS).mock(return_value=httpx.Response(200, text="null"))
    await server.list_queued_jobs(
        owner="example", repo="demo-repo", labels="self-hosted-arm64"
    )
    assert route.calls.last.request.url.params["labels"] == "self-hosted-arm64"


# ── merge_pull title / message ──────────────────────────────────────────────

MERGE = "/repos/e/r/pulls/7/merge"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "method": "squash",
                "merge_title": "[x] feat: thing (#7)",
                "merge_message": "Long rationale.",
            },
            {
                "do": "squash",
                "delete_branch_after_merge": False,
                "MergeTitleField": "[x] feat: thing (#7)",
                "MergeMessageField": "Long rationale.",
            },
        ),
        ({}, {"do": "merge", "delete_branch_after_merge": False}),
        # An explicit empty message is sent, not mistaken for "not given".
        (
            {"merge_message": ""},
            {
                "do": "merge",
                "delete_branch_after_merge": False,
                "MergeMessageField": "",
            },
        ),
    ],
    ids=["title-and-message", "omitted-by-default", "empty-message"],
)
async def test_merge_pull_request_body(mcp_client, respx_mock, kwargs, expected):
    route = respx_mock.post(MERGE).mock(return_value=httpx.Response(200))
    await server.merge_pull(owner="e", repo="r", index=7, **kwargs)
    assert json_lib.loads(route.calls.last.request.content) == expected
