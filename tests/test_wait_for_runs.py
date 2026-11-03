"""wait_for_runs — block until every Actions run for a commit is terminal.

Run rows reuse the live /actions/runs shape from test_actions_diagnostics
(commit_sha, prettyref, no head_sha). The clock and sleep are faked through
server._wait_now / server._wait_sleep so no real time passes.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp.client import Client

import server

RUNS = "/repos/example/demo-repo/actions/runs"
SHA = "43ee6f4e9a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _run(
    run_id: int, status: str = "success", sha: str = SHA, ref: str = "main"
) -> dict:
    return {
        "id": run_id,
        "workflow_id": "lint-unit.yml",
        "index_in_repo": run_id,
        "prettyref": ref,
        "commit_sha": sha,
        "event": "push" if ref == "main" else "pull_request",
        "status": status,
        "html_url": f"https://codeberg.org/example/demo-repo/actions/runs/{run_id}",
    }


def _jobs(run_id: int, *statuses: str) -> list[dict]:
    return [
        {"id": run_id * 10 + i, "name": f"job{i}", "status": s}
        for i, s in enumerate(statuses)
    ]


@pytest.fixture
def clock(monkeypatch):
    """A fake clock that only moves when the tool sleeps."""
    state = {"t": 1000.0, "sleeps": []}

    async def fake_sleep(seconds):
        state["sleeps"].append(seconds)
        state["t"] += seconds

    monkeypatch.setattr(server, "_wait_now", lambda: state["t"])
    monkeypatch.setattr(server, "_wait_sleep", fake_sleep)
    return state


def _mock_runs(respx_mock, *pages: list[dict]):
    """Each poll gets the next page; the last one repeats."""
    responses = [httpx.Response(200, json={"workflow_runs": p}) for p in pages]
    route = respx_mock.get(RUNS)
    route.side_effect = responses + [responses[-1]] * 50
    return route


def _mock_jobs(respx_mock, run_id: int, *pages: list[dict]):
    responses = [httpx.Response(200, json=p) for p in pages]
    route = respx_mock.get(f"{RUNS}/{run_id}/jobs")
    route.side_effect = responses + [responses[-1]] * 50
    return route


async def _wait(**kw):
    kw.setdefault("owner", "example")
    kw.setdefault("repo", "demo-repo")
    kw.setdefault("head_sha", SHA)
    return await server.wait_for_runs(**kw)


async def test_returns_done_for_all_runs_on_the_sha_only(mcp_client, respx_mock, clock):
    # A push run and a PR run for the same commit, plus another commit's run.
    _mock_runs(respx_mock, [_run(2), _run(3, ref="#130"), _run(4, sha="deadbeef00")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))
    _mock_jobs(respx_mock, 3, _jobs(3, "success", "skipped"))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is True
    assert result["waited_s"] == 0
    assert [r["id"] for r in result["runs"]] == [2, 3]
    assert result["runs"][1]["head_branch"] == "#130"
    # Jobs are reported even for runs that were terminal on the first read.
    assert [j["status"] for j in result["runs"][1]["jobs"]] == ["success", "skipped"]
    assert clock["sleeps"] == []


async def test_matches_a_short_sha_prefix(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2)])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait(head_sha=SHA[:7].upper())

    assert result["outcome"] == "done"
    assert [r["id"] for r in result["runs"]] == [2]


async def test_polls_until_the_run_finishes(mcp_client, respx_mock, clock):
    runs = _mock_runs(respx_mock, [_run(2, "running")], [_run(2, "success")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"), _jobs(2, "success"))

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["waited_s"] == 30
    assert clock["sleeps"] == [30]
    assert runs.call_count == 2


async def test_trusts_finished_jobs_over_a_lagging_run_row(
    mcp_client, respx_mock, clock
):
    # Seen 2026-10-23: the run list said `running` after every job had passed.
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success", "success"))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is True
    assert result["runs"][0]["status"] == "running"
    assert result["runs"][0]["effective_status"] == "success"
    assert clock["sleeps"] == []


async def test_a_failed_job_makes_a_lagging_run_a_failure(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success", "failure"))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is False
    assert result["runs"][0]["effective_status"] == "failure"


async def test_waits_while_a_lagging_run_still_has_a_running_job(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(
        respx_mock, 2, _jobs(2, "success", "running"), _jobs(2, "success", "success")
    )

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert clock["sleeps"] == [30]


async def test_any_failed_run_clears_all_success(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2), _run(3, "failure")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))
    _mock_jobs(respx_mock, 3, _jobs(3, "failure"))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is False


async def test_no_runs_after_settle(mcp_client, respx_mock, clock):
    # A merge whose response was lost can land without creating a push run.
    _mock_runs(respx_mock, [_run(4, sha="deadbeef00")])

    result = await _wait(poll_s=30, settle_s=90, timeout_s=600)

    assert result["outcome"] == "no_runs"
    assert result["runs"] == []
    assert result["waited_s"] == 90


async def test_a_run_that_appears_late_is_waited_for(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [], [_run(2, "waiting")], [_run(2, "success")])
    _mock_jobs(respx_mock, 2, _jobs(2, "waiting"), _jobs(2, "success"))

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["waited_s"] == 60


async def test_timeout_reports_where_runs_are(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"))

    result = await _wait(poll_s=30, timeout_s=75)

    assert result["outcome"] == "timeout"
    assert result["all_success"] is False
    assert result["waited_s"] == 75
    # The last sleep is trimmed so the final check lands on the deadline.
    assert clock["sleeps"] == [30, 30, 15]
    assert result["runs"][0]["effective_status"] == "running"


async def test_timeout_is_capped(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"))

    result = await _wait(poll_s=600, timeout_s=10_000)

    assert result["outcome"] == "timeout"
    assert result["waited_s"] == server._WAIT_MAX_TIMEOUT_S


async def test_sends_page_so_the_api_honours_limit(mcp_client, respx_mock, clock):
    route = _mock_runs(respx_mock, [_run(2)])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    await _wait()

    params = route.calls[0].request.url.params
    assert params["page"] == "1" and params["limit"] == "50"


async def test_rejects_a_too_short_sha(mcp_client, respx_mock, clock):
    with pytest.raises(server.ToolUsageError, match="at least 7"):
        await _wait(head_sha="43ee6f")


async def test_a_definite_api_error_comes_back_structured(
    mcp_client, respx_mock, clock
):
    respx_mock.get(RUNS).mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    result = await _wait()

    assert result["status"] == 403
    assert "forbidden" in result["error"]
    assert clock["sleeps"] == []


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.Response(200, text="<html>proxy</html>")],
    ids=["transport", "unreadable-body"],
)
async def test_a_run_list_with_no_definite_answer_is_retried(
    mcp_client, respx_mock, clock, failure
):
    ok = httpx.Response(200, json={"workflow_runs": [_run(2)]})
    respx_mock.get(RUNS).side_effect = [failure, ok, ok]
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["transient_errors"] == 1


async def test_a_transient_list_error_is_retried(mcp_client, respx_mock, clock):
    respx_mock.get(RUNS).side_effect = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"workflow_runs": [_run(2)]}),
    ]
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["transient_errors"] == 2
    assert result["all_success"] is True


async def test_transient_errors_until_the_deadline_end_in_timeout(
    mcp_client, respx_mock, clock
):
    respx_mock.get(RUNS).mock(return_value=httpx.Response(500, json={}))

    result = await _wait(poll_s=30, timeout_s=60, settle_s=600)

    # Not "no_runs": the list was never read, so nothing is known about runs.
    assert result["outcome"] == "timeout"
    assert result["transient_errors"] == 3


async def test_a_transient_jobs_error_is_no_info_this_poll(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    respx_mock.get(f"{RUNS}/2/jobs").side_effect = [
        httpx.Response(500, json={}),
        httpx.Response(200, json=_jobs(2, "success")),
    ]

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["waited_s"] == 30
    assert result["runs"][0]["effective_status"] == "success"


async def test_a_definite_jobs_error_comes_back_structured(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    respx_mock.get(f"{RUNS}/2/jobs").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    result = await _wait()

    assert result["status"] == 403


async def test_unreadable_jobs_are_null_not_empty(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2)])
    respx_mock.get(f"{RUNS}/2/jobs").mock(return_value=httpx.Response(404))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["runs"][0]["jobs"] is None


async def test_a_lagging_run_with_unreadable_jobs_keeps_waiting(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    respx_mock.get(f"{RUNS}/2/jobs").mock(return_value=httpx.Response(404))

    result = await _wait(poll_s=30, timeout_s=60)

    assert result["outcome"] == "timeout"
    assert result["runs"][0]["effective_status"] == "running"


async def test_reads_a_gitea_wrapped_jobs_body(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, {"jobs": _jobs(2, "success")})

    result = await _wait()

    assert result["runs"][0]["effective_status"] == "success"
    assert [j["id"] for j in result["runs"][0]["jobs"]] == [20]


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (("cancelled", "success"), "cancelled"),
        (("failure", "cancelled"), "failure"),
        (("skipped", "skipped"), "skipped"),
    ],
)
async def test_job_verdict_precedence(mcp_client, respx_mock, clock, statuses, verdict):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, *statuses))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["runs"][0]["effective_status"] == verdict
    assert result["all_success"] is (verdict == "skipped")


async def test_a_cancelled_run_is_terminal_and_not_a_success(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "cancelled")])
    _mock_jobs(respx_mock, 2, _jobs(2, "cancelled"))

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is False
    assert clock["sleeps"] == []


async def test_a_skipped_run_counts_as_success(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "skipped")])
    _mock_jobs(respx_mock, 2, _jobs(2, "skipped"))

    result = await _wait()

    assert result["all_success"] is True


async def test_a_run_without_a_sha_never_matches(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(5, "running", sha="")])

    result = await _wait(settle_s=0)

    assert result["outcome"] == "no_runs"


async def test_event_and_branch_skip_stale_runs_on_the_same_sha(
    mcp_client, respx_mock, clock
):
    # Rebase merge: the PR run and the branch push run for this sha finished
    # earlier; the push run on main only appears on the second poll.
    pr_run = _run(2, ref="#130")
    branch_push = {**_run(3, ref="feat/x"), "event": "push"}
    main_push = _run(4, "running")
    _mock_runs(
        respx_mock,
        [pr_run, branch_push],
        [main_push, pr_run, branch_push],
        [_run(4), pr_run, branch_push],
    )
    _mock_jobs(respx_mock, 4, _jobs(4, "running"), _jobs(4, "success"))

    result = await _wait(event="push", branch="main", poll_s=30)

    assert result["outcome"] == "done"
    assert [r["id"] for r in result["runs"]] == [4]
    assert result["waited_s"] == 60


async def test_without_filters_stale_runs_answer_immediately(
    mcp_client, respx_mock, clock
):
    # Pins the documented caveat: unfiltered, old runs on the sha are enough.
    _mock_runs(respx_mock, [_run(2, ref="#130")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait()

    assert result["outcome"] == "done" and result["waited_s"] == 0


async def test_a_run_held_for_approval_returns_needs_approval(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [{**_run(2, "waiting"), "need_approval": True}])
    _mock_jobs(respx_mock, 2, _jobs(2, "waiting"))

    result = await _wait()

    assert result["outcome"] == "needs_approval"
    assert result["all_success"] is False
    assert clock["sleeps"] == []


async def test_settle_is_capped_at_timeout(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [])

    result = await _wait(poll_s=30, timeout_s=60, settle_s=600)

    assert result["outcome"] == "no_runs"
    assert result["waited_s"] == 60


async def test_poll_interval_has_a_floor(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")], [_run(2)])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"), _jobs(2, "success"))

    await _wait(poll_s=1)

    assert clock["sleeps"] == [server._WAIT_MIN_POLL_S]


async def test_zero_timeout_checks_once(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"))

    result = await _wait(timeout_s=0)

    assert result["outcome"] == "timeout"
    assert clock["sleeps"] == []


async def test_every_result_says_how_many_runs_were_searched(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [])

    result = await _wait(settle_s=0)

    assert result["searched"] == server._WAIT_RUN_WINDOW == 50


async def test_event_alone_drops_pr_runs(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2), _run(3, "running", ref="#130")])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait(event="push")

    assert result["outcome"] == "done"
    assert [r["id"] for r in result["runs"]] == [2]


async def test_a_dropped_jobs_connection_is_no_info_this_poll(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    respx_mock.get(f"{RUNS}/2/jobs").side_effect = [
        httpx.ConnectError("reset"),
        httpx.Response(200, json=_jobs(2, "success")),
    ]

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"
    assert result["waited_s"] == 30


async def test_an_approved_run_is_waited_for(mcp_client, respx_mock, clock):
    held = {**_run(2, "waiting"), "need_approval": True, "approved_by": 7}
    _mock_runs(respx_mock, [held], [_run(2)])
    _mock_jobs(respx_mock, 2, _jobs(2, "waiting"), _jobs(2, "success"))

    result = await _wait(poll_s=30)

    assert result["outcome"] == "done"


async def test_a_finished_run_flagged_for_approval_does_not_block(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [{**_run(2), "need_approval": True}])
    _mock_jobs(respx_mock, 2, _jobs(2, "success"))

    result = await _wait()

    assert result["outcome"] == "done"


async def test_a_finished_flagged_run_beside_a_running_one_is_not_held(
    mcp_client, respx_mock, clock
):
    # Only a pending run can be held for approval; a finished one that still
    # carries the flag must not turn a normal wait into "needs_approval".
    _mock_runs(respx_mock, [{**_run(2), "need_approval": True}, _run(3, "running")])
    _mock_jobs(respx_mock, 3, _jobs(3, "running"))

    result = await _wait(timeout_s=60, poll_s=30)

    assert result["outcome"] == "timeout"


async def test_a_running_run_with_no_jobs_yet_keeps_waiting(
    mcp_client, respx_mock, clock
):
    # A run row can exist before its jobs do; an empty list is "not started",
    # never an all-skipped (and so all_success) verdict.
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, [])

    result = await _wait(timeout_s=60, poll_s=30)

    assert result["outcome"] == "timeout"
    assert result["all_success"] is False


async def test_a_definite_error_on_the_final_jobs_fetch_keeps_the_verdict(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "failure")])
    respx_mock.get(f"{RUNS}/2/jobs").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    result = await _wait()

    assert result["outcome"] == "done"
    assert result["all_success"] is False
    assert result["runs"][0]["jobs"] is None


class _RecordingContext:
    """Stands in for MCPServer's Context: records report_progress calls."""

    def __init__(self, raises: BaseException | None = None):
        self.calls: list[tuple] = []
        self.raises = raises

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))
        if self.raises is not None:
            raise self.raises


async def test_progress_is_reported_on_every_poll(mcp_client, respx_mock, clock):
    _mock_runs(
        respx_mock,
        [],
        [_run(2, "running"), _run(3)],
        [_run(2), _run(3)],
    )
    _mock_jobs(respx_mock, 2, _jobs(2, "running"), _jobs(2, "success"))
    _mock_jobs(respx_mock, 3, _jobs(3, "success"))
    ctx = _RecordingContext()

    result = await _wait(poll_s=30, timeout_s=600, ctx=ctx)

    assert result["outcome"] == "done"
    assert ctx.calls == [
        (0, 600, "no matching run yet"),
        (30, 600, "1/2 runs finished"),
    ]


async def test_a_failing_progress_notification_does_not_end_the_wait(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")], [_run(2)])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"), _jobs(2, "success"))

    ctx = _RecordingContext(raises=RuntimeError("client went away"))

    result = await _wait(poll_s=30, ctx=ctx)

    assert result["outcome"] == "done"
    assert len(ctx.calls) == 1  # it was attempted, and its failure was absorbed


async def test_cancelling_during_a_progress_notification_still_cancels(
    mcp_client, respx_mock, clock
):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"))

    with pytest.raises(asyncio.CancelledError):
        await _wait(ctx=_RecordingContext(raises=asyncio.CancelledError()))


async def test_progress_says_when_the_run_list_could_not_be_read(
    mcp_client, respx_mock, clock
):
    respx_mock.get(RUNS).side_effect = [
        httpx.Response(200, json={"workflow_runs": [_run(2, "running")]}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json={"workflow_runs": [_run(2)]}),
    ]
    _mock_jobs(respx_mock, 2, _jobs(2, "running"), _jobs(2, "success"))
    ctx = _RecordingContext()

    result = await _wait(poll_s=30, ctx=ctx)

    assert result["outcome"] == "done"
    assert [m for _, _, m in ctx.calls] == [
        "0/1 runs finished",
        "run list unavailable, retrying",
    ]


async def test_no_progress_on_the_poll_that_times_out(mcp_client, respx_mock, clock):
    _mock_runs(respx_mock, [_run(2, "running")])
    _mock_jobs(respx_mock, 2, _jobs(2, "running"))
    ctx = _RecordingContext()

    result = await _wait(poll_s=30, timeout_s=60, ctx=ctx)

    assert result["outcome"] == "timeout"
    assert [p for p, _, _ in ctx.calls] == [0, 30]


async def test_progress_reaches_a_real_mcp_client(fake_accounts, respx_mock, clock):
    # End to end through MCPServer: the client's progress token must reach the
    # tool, or report_progress silently does nothing.
    respx_mock.get(f"{server.BASE_URL}/user").mock(
        return_value=httpx.Response(200, json={"login": "example"})
    )
    respx_mock.get(f"{server.BASE_URL}{RUNS}").side_effect = [
        httpx.Response(200, json={"workflow_runs": [_run(2, "running")]}),
        httpx.Response(200, json={"workflow_runs": [_run(2)]}),
    ]
    respx_mock.get(f"{server.BASE_URL}{RUNS}/2/jobs").side_effect = [
        httpx.Response(200, json=_jobs(2, "running")),
        httpx.Response(200, json=_jobs(2, "success")),
    ]
    seen: list[tuple] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "wait_for_runs",
            {"owner": "example", "repo": "demo-repo", "head_sha": SHA},
            progress_callback=on_progress,
        )

    assert not result.is_error
    assert '"outcome": "done"' in result.content[0].text
    assert seen == [(0, 600, "0/1 runs finished")]


async def test_a_client_without_a_progress_token_still_gets_its_result(
    fake_accounts, respx_mock, clock
):
    respx_mock.get(f"{server.BASE_URL}/user").mock(
        return_value=httpx.Response(200, json={"login": "example"})
    )
    respx_mock.get(f"{server.BASE_URL}{RUNS}").side_effect = [
        httpx.Response(200, json={"workflow_runs": [_run(2, "running")]}),
        httpx.Response(200, json={"workflow_runs": [_run(2)]}),
    ]
    respx_mock.get(f"{server.BASE_URL}{RUNS}/2/jobs").side_effect = [
        httpx.Response(200, json=_jobs(2, "running")),
        httpx.Response(200, json=_jobs(2, "success")),
    ]

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "wait_for_runs",
            {"owner": "example", "repo": "demo-repo", "head_sha": SHA},
        )

    assert not result.is_error
    assert '"outcome": "done"' in result.content[0].text
