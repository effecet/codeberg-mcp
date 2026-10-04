"""Tests for the 9 new codeberg MCP tools."""

from __future__ import annotations

import json as json_lib

import httpx
import pytest
import server

# ── get_latest_commit ──────────────────────────────────────────────────────


async def test_get_latest_commit_explicit_branch(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "id": "abc123",
                    "message": "backup: 5 files + db (timestamp) [ci-host]",
                    "url": "https://codeberg.org/example/demo-repo/commit/abc123",
                    "author": {
                        "name": "example",
                        "email": "e@x",
                        "username": "example",
                    },
                    "committer": {
                        "name": "example",
                        "email": "e@x",
                        "username": "example",
                    },
                    "timestamp": "2026-04-17T10:00:00Z",
                },
                "protected": False,
            },
        )
    )
    result = await server.get_latest_commit(
        owner="example", repo="demo-repo", branch="main"
    )
    assert result["sha"] == "abc123"
    assert "[ci-host]" in result["message"]
    assert result["timestamp"] == "2026-04-17T10:00:00Z"
    assert result["url"] == "https://codeberg.org/example/demo-repo/commit/abc123"


async def test_get_latest_commit_uses_branches_endpoint_for_branch_name(
    mcp_client, respx_mock
):
    """Regression: /repos/{o}/{r}/commits/{ref} 404s for branch names on
    Forgejo/Codeberg; must resolve branches via /repos/{o}/{r}/branches/{b}."""
    respx_mock.get("/repos/example/demo-repo/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "id": "3838e26aa96c24a9a9aa66204565bb366425e30f",
                    "message": "backup: 5 files + db (2026-04-17T16:07:24Z) [ci-host]\n",
                    "url": "https://codeberg.org/example/demo-repo/commit/3838e26aa96c24a9a9aa66204565bb366425e30f",
                    "author": {
                        "name": "example",
                        "email": "example@noreply.codeberg.org",
                        "username": "example",
                    },
                    "committer": {
                        "name": "example",
                        "email": "example@noreply.codeberg.org",
                        "username": "example",
                    },
                    "timestamp": "2026-04-17T18:07:32+02:00",
                },
                "protected": False,
            },
        )
    )
    result = await server.get_latest_commit(
        owner="example", repo="demo-repo", branch="main"
    )
    assert result["sha"] == "3838e26aa96c24a9a9aa66204565bb366425e30f"
    assert "[ci-host]" in result["message"]
    assert result["timestamp"] == "2026-04-17T18:07:32+02:00"
    assert result["author"]["name"] == "example"


async def test_get_latest_commit_defaults_to_repo_default_branch(
    mcp_client, respx_mock
):
    respx_mock.get("/repos/example/demo-repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "example/demo-repo",
                "private": False,
                "html_url": "",
                "default_branch": "main",
            },
        )
    )
    respx_mock.get("/repos/example/demo-repo/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "id": "default-branch-sha",
                    "message": "x",
                    "url": "",
                    "author": {"name": "e", "email": "e@x", "username": "e"},
                    "committer": {"name": "e", "email": "e@x", "username": "e"},
                    "timestamp": "2026-04-17T10:00:00Z",
                },
                "protected": False,
            },
        )
    )
    result = await server.get_latest_commit(owner="example", repo="demo-repo")
    assert result["sha"] == "default-branch-sha"


# ── compare_refs ───────────────────────────────────────────────────────────


async def test_compare_refs_returns_commits_and_files(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/compare/abc...def").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_commits": 2,
                "commits": [
                    {"sha": "c1", "commit": {"message": "fix: thing"}, "html_url": ""},
                    {"sha": "c2", "commit": {"message": "feat: thing"}, "html_url": ""},
                ],
                "files": [
                    {
                        "filename": "src/a.ts",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 2,
                    },
                    {
                        "filename": "tests/b.ts",
                        "status": "added",
                        "additions": 30,
                        "deletions": 0,
                    },
                ],
            },
        )
    )
    result = await server.compare_refs(
        owner="example", repo="demo-repo", base="abc", head="def"
    )
    assert result["total_commits"] == 2
    assert len(result["commits"]) == 2
    assert len(result["files"]) == 2
    assert result["files"][0]["filename"] == "src/a.ts"


# ── workflow runs ──────────────────────────────────────────────────────────


async def test_list_workflow_runs_returns_shape(mcp_client, respx_mock):
    # Now mocks /actions/runs (not /actions/tasks) — fixed 2026-04-19 so
    # waiting/queued runs are visible. Response fields reflect the runs
    # endpoint: `created`/`updated` instead of `created_at`/`updated_at`.
    respx_mock.get("/repos/example/demo-repo/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 123,
                        "name": "CI",
                        "status": "success",
                        "conclusion": "success",
                        "head_branch": "main",
                        "head_sha": "abc",
                        "run_number": 42,
                        "event": "push",
                        "created": "2026-04-17T10:00:00Z",
                        "updated": "2026-04-17T10:05:00Z",
                        "html_url": "https://codeberg.org/example/demo-repo/actions/runs/123",
                    }
                ]
            },
        )
    )
    result = await server.list_workflow_runs(owner="example", repo="demo-repo")
    assert len(result) == 1
    assert result[0]["id"] == 123
    assert result[0]["status"] == "success"
    assert result[0]["run_number"] == 42
    assert result[0]["created"] == "2026-04-17T10:00:00Z"


async def test_list_workflow_runs_waiting_run_name_falls_back_to_workflow_id(
    mcp_client, respx_mock
):
    """On the /actions/runs endpoint, waiting runs may have name=null;
    mapping must fall back to workflow_id so the result is still identifiable."""
    respx_mock.get("/repos/example/demo-repo/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 999,
                        "name": None,
                        "workflow_id": "_caller-test.yml",
                        "status": "waiting",
                        "conclusion": "",
                        "head_branch": "main",
                        "commit_sha": "deadbeef",
                        "run_number": 1,
                        "event": "push",
                        "created": "2026-04-19T22:55:37+02:00",
                        "updated": "2026-04-19T22:55:37+02:00",
                        "html_url": "https://codeberg.org/example/ci-workflows/actions/runs/1",
                    }
                ]
            },
        )
    )
    result = await server.list_workflow_runs(owner="example", repo="demo-repo")
    assert len(result) == 1
    assert result[0]["status"] == "waiting"
    assert result[0]["name"] == "_caller-test.yml"
    assert result[0]["head_sha"] == "deadbeef"


async def test_get_workflow_run_uses_direct_endpoint(mcp_client, respx_mock):
    """get_workflow_run now hits /actions/runs/{id} directly — the claim that
    Codeberg didn't expose a single-run detail endpoint was wrong (verified
    2026-04-19 against a live run)."""
    respx_mock.get("/repos/example/demo-repo/actions/runs/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 123,
                "name": "CI",
                "status": "success",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": "abc",
                "run_number": 42,
                "event": "push",
                "workflow_id": "ci.yml",
                "display_title": "feat: something",
                "created": "2026-04-17T10:00:00Z",
                "updated": "2026-04-17T10:05:00Z",
                "html_url": "https://codeberg.org/example/demo-repo/actions/runs/42",
            },
        )
    )
    result = await server.get_workflow_run(
        owner="example", repo="demo-repo", run_id=123
    )
    assert result["id"] == 123
    assert result["name"] == "CI"
    assert result["run_number"] == 42
    assert result["url"].endswith("/actions/runs/42")
    # jobs are not available via API — empty list + _note
    assert result["jobs"] == []
    assert "_note" in result
    assert "not exposed" in result["_note"]


async def test_get_workflow_run_raises_when_not_found(mcp_client, respx_mock):
    """When run_id isn't on the server, raise a clear error (rather than silently
    returning None or an empty dict)."""
    respx_mock.get("/repos/example/demo-repo/actions/runs/999").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    with pytest.raises(server.ToolUsageError, match="run_id=999 not found"):
        await server.get_workflow_run(owner="example", repo="demo-repo", run_id=999)


async def test_get_workflow_logs_returns_text(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/actions/tasks/123/logs").mock(
        return_value=httpx.Response(200, text="line 1\nline 2\nline 3")
    )
    result = await server.get_workflow_logs(
        owner="example", repo="demo-repo", run_id=123
    )
    assert result == "line 1\nline 2\nline 3"


async def test_get_workflow_logs_truncates_over_max_bytes(mcp_client, respx_mock):
    huge = "x" * 100000
    respx_mock.get("/repos/example/demo-repo/actions/tasks/123/logs").mock(
        return_value=httpx.Response(200, text=huge)
    )
    result = await server.get_workflow_logs(
        owner="example", repo="demo-repo", run_id=123, max_bytes=1000
    )
    assert len(result.encode("utf-8")) < 1100
    assert "[truncated" in result


# ── issues ─────────────────────────────────────────────────────────────────


async def test_list_issues_filters_type_to_issues(mcp_client, respx_mock):
    route = respx_mock.get("/repos/example/demo-repo/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "bug",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "created_at": "2026-04-10T10:00:00Z",
                    "updated_at": "2026-04-10T10:00:00Z",
                    "comments": 2,
                    "html_url": "",
                },
            ],
        )
    )
    result = await server.list_issues(owner="example", repo="demo-repo")
    assert len(result) == 1
    assert result[0]["number"] == 1
    assert route.calls[0].request.url.params.get("type") == "issues"


async def test_get_issue_returns_full_body(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/issues/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "bug: thermal decay drops wrong tier",
                "body": "Steps to repro:\n1. ...",
                "state": "open",
                "labels": [{"name": "bug"}, {"name": "thermal"}],
                "assignees": [{"login": "example"}],
                "created_at": "2026-04-10T10:00:00Z",
                "updated_at": "2026-04-11T10:00:00Z",
                "html_url": "",
            },
        )
    )
    result = await server.get_issue(owner="example", repo="demo-repo", issue_number=42)
    assert result["number"] == 42
    assert "Steps to repro" in result["body"]
    assert result["labels"] == ["bug", "thermal"]
    assert result["assignees"] == ["example"]


async def test_create_issue_posts_json_body(mcp_client, respx_mock):
    route = respx_mock.post("/repos/example/demo-repo/issues").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 99,
                "title": "new issue",
                "state": "open",
                "labels": [],
                "html_url": "",
                "created_at": "2026-04-17T10:00:00Z",
                "updated_at": "2026-04-17T10:00:00Z",
            },
        )
    )
    result = await server.create_issue(
        owner="example",
        repo="demo-repo",
        title="new issue",
        body="description here",
        labels=["bug", "needs-triage"],
    )
    assert result["number"] == 99
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["title"] == "new issue"
    assert sent["body"] == "description here"
    assert sent["labels"] == ["bug", "needs-triage"]


async def test_comment_on_issue_posts_body(mcp_client, respx_mock):
    route = respx_mock.post("/repos/example/demo-repo/issues/42/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 1001,
                "body": "great question",
                "user": {"login": "octocat"},
                "created_at": "2026-04-17T10:00:00Z",
                "html_url": "",
            },
        )
    )
    result = await server.comment_on_issue(
        owner="example",
        repo="demo-repo",
        issue_number=42,
        body="great question",
    )
    assert result["id"] == 1001
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["body"] == "great question"


# ── user account tools ─────────────────────────────────────────────────────


async def test_list_user_keys_compact_projection_drops_pubkey_body(
    mcp_client, respx_mock
):
    """list_user_keys must NOT leak the full pubkey body (compact projection
    decision 2026-05-05). Returns id/title/fingerprint/read_only/created_at
    only — `key_type` is dropped because Forgejo uses it as a user-vs-deploy
    category, always "user" on /user/keys (verified live 2026-05-05)."""
    respx_mock.get("/user/keys").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 12345,
                    "title": "pi-deploy",
                    "key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... full body here",
                    "fingerprint": "SHA256:abc123",
                    "key_type": "user",
                    "read_only": False,
                    "created_at": "2026-04-23T10:00:00Z",
                    "url": "https://codeberg.org/api/v1/user/keys/12345",
                    "user": {"login": "example"},
                }
            ],
        )
    )
    result = await server.list_user_keys()
    assert len(result) == 1
    k = result[0]
    assert k["id"] == 12345
    assert k["title"] == "pi-deploy"
    assert k["fingerprint"] == "SHA256:abc123"
    assert k["read_only"] is False
    assert k["created_at"] == "2026-04-23T10:00:00Z"
    # Compact projection: pubkey body, url, user, key_type must be stripped
    assert "key" not in k
    assert "url" not in k
    assert "user" not in k
    assert "key_type" not in k


async def test_list_user_emails_compact_projection(mcp_client, respx_mock):
    """list_user_emails returns only email/primary/verified — drops user_id
    and username (always self)."""
    respx_mock.get("/user/emails").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "email": "user@example.com",
                    "primary": True,
                    "verified": True,
                    "user_id": 99,
                    "username": "example",
                },
                {
                    "email": "noreply@codeberg.org",
                    "primary": False,
                    "verified": True,
                    "user_id": 99,
                    "username": "example",
                },
            ],
        )
    )
    result = await server.list_user_emails()
    assert len(result) == 2
    assert result[0] == {
        "email": "user@example.com",
        "primary": True,
        "verified": True,
    }
    assert "user_id" not in result[0]
    assert "username" not in result[0]


# ── repo settings tools ────────────────────────────────────────────────────


async def test_set_repo_actions_enabled_patches_has_actions_true(
    mcp_client, respx_mock
):
    route = respx_mock.patch("/repos/example/fresh-repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "example/fresh-repo", "has_actions": True},
        )
    )
    result = await server.set_repo_actions_enabled(
        owner="example", repo="fresh-repo", enabled=True
    )
    assert result == {"full_name": "example/fresh-repo", "has_actions": True}
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent == {"has_actions": True}


async def test_set_repo_actions_enabled_patches_false(mcp_client, respx_mock):
    route = respx_mock.patch("/repos/example/old-repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "example/old-repo", "has_actions": False},
        )
    )
    result = await server.set_repo_actions_enabled(
        owner="example", repo="old-repo", enabled=False
    )
    assert result["has_actions"] is False
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent == {"has_actions": False}


async def test_list_repo_secrets_returns_names_and_timestamps(mcp_client, respx_mock):
    """Forgejo Secret schema only exposes name + created_at — values are
    write-only on the API. Verify we surface both fields cleanly."""
    respx_mock.get("/repos/example/demo-repo/actions/secrets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "SUPABASE_URL", "created_at": "2026-04-01T00:00:00Z"},
                {"name": "SUPABASE_KEY", "created_at": "2026-04-01T00:00:00Z"},
            ],
        )
    )
    result = await server.list_repo_secrets(owner="example", repo="demo-repo")
    assert result == [
        {"name": "SUPABASE_URL", "created_at": "2026-04-01T00:00:00Z"},
        {"name": "SUPABASE_KEY", "created_at": "2026-04-01T00:00:00Z"},
    ]


async def test_list_repo_secrets_empty_repo(mcp_client, respx_mock):
    respx_mock.get("/repos/example/clean-repo/actions/secrets").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await server.list_repo_secrets(owner="example", repo="clean-repo")
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 expansion — 20 new tools across 7 feature areas
# ═══════════════════════════════════════════════════════════════════════════


# ── list_pull_files ─────────────────────────────────────────────────────────


async def test_list_pull_files_returns_file_entries(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/pulls/7/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "server.py",
                    "status": "modified",
                    "additions": 42,
                    "deletions": 3,
                    "changes": 45,
                    "sha": "abc123",
                    "raw_url": "https://codeberg.org/example/demo-repo/raw/abc123/server.py",
                },
                {
                    "filename": "tests/test_new_tools.py",
                    "status": "added",
                    "additions": 80,
                    "deletions": 0,
                    "changes": 80,
                    "sha": "def456",
                    "raw_url": "",
                },
            ],
        )
    )
    result = await server.list_pull_files(owner="example", repo="demo-repo", index=7)
    assert len(result) == 2
    assert result[0]["filename"] == "server.py"
    assert result[0]["status"] == "modified"
    assert result[0]["additions"] == 42
    assert result[1]["status"] == "added"


async def test_list_pull_files_error_surfaces_404(mcp_client, respx_mock, tool_error):
    respx_mock.get("/repos/example/nope/pulls/1/files").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await tool_error(
        server.list_pull_files(owner="example", repo="nope", index=1)
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── list_pull_commits ───────────────────────────────────────────────────────


async def test_list_pull_commits_returns_commit_entries(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/pulls/7/commits").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "sha": "abc123",
                    "commit": {
                        "message": "feat: add tool X",
                        "author": {"name": "example", "date": "2026-05-12T10:00:00Z"},
                    },
                    "url": "https://codeberg.org/example/demo-repo/commit/abc123",
                }
            ],
        )
    )
    result = await server.list_pull_commits(owner="example", repo="demo-repo", index=7)
    assert len(result) == 1
    assert result[0]["sha"] == "abc123"
    assert result[0]["message"].startswith("feat:")
    assert result[0]["author"] == "example"


async def test_list_pull_commits_error_surfaces_404(mcp_client, respx_mock, tool_error):
    respx_mock.get("/repos/example/nope/pulls/1/commits").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await tool_error(
        server.list_pull_commits(owner="example", repo="nope", index=1)
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── list_pull_reviews ───────────────────────────────────────────────────────


async def test_list_pull_reviews_returns_review_entries(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/pulls/7/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 101,
                    "state": "APPROVED",
                    "body": "LGTM",
                    "user": {"login": "example"},
                    "submitted_at": "2026-05-12T11:00:00Z",
                    "commit_id": "abc123",
                }
            ],
        )
    )
    result = await server.list_pull_reviews(owner="example", repo="demo-repo", index=7)
    assert len(result) == 1
    assert result[0]["state"] == "APPROVED"
    assert result[0]["user"] == "example"


async def test_list_pull_reviews_error_surfaces_404(mcp_client, respx_mock, tool_error):
    respx_mock.get("/repos/example/nope/pulls/1/reviews").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await tool_error(
        server.list_pull_reviews(owner="example", repo="nope", index=1)
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── cancel_workflow_run ─────────────────────────────────────────────────────


async def test_cancel_workflow_run_returns_status(mcp_client, respx_mock):
    respx_mock.post("/repos/example/demo-repo/actions/runs/42/cancel").mock(
        return_value=httpx.Response(
            200,
            json={"id": 42, "status": "cancelled"},
        )
    )
    result = await server.cancel_workflow_run(
        owner="example", repo="demo-repo", run_id=42
    )
    assert result["id"] == 42
    assert result["status"] == "cancelled"


async def test_cancel_workflow_run_error_surfaces_404(mcp_client, respx_mock):
    respx_mock.post("/repos/example/nope/actions/runs/99/cancel").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await server.cancel_workflow_run(owner="example", repo="nope", run_id=99)
    assert result["status"] == 404
    assert "404" in result["error"]


# ── rerun_workflow_run ──────────────────────────────────────────────────────


async def test_rerun_workflow_run_returns_status(mcp_client, respx_mock):
    respx_mock.post("/repos/example/demo-repo/actions/runs/42/rerun").mock(
        return_value=httpx.Response(
            200,
            json={"id": 42, "status": "queued"},
        )
    )
    result = await server.rerun_workflow_run(
        owner="example", repo="demo-repo", run_id=42
    )
    assert result["id"] == 42
    assert result["status"] == "queued"


async def test_rerun_workflow_run_412_on_running(mcp_client, respx_mock):
    """Forgejo returns 412 when trying to rerun a still-running workflow."""
    respx_mock.post("/repos/example/demo-repo/actions/runs/77/rerun").mock(
        return_value=httpx.Response(
            412, json={"message": "workflow run is still running"}
        )
    )
    result = await server.rerun_workflow_run(
        owner="example", repo="demo-repo", run_id=77
    )
    assert result["status"] == 412
    assert "412" in result["error"]


# ── edit_repo ───────────────────────────────────────────────────────────────


async def test_edit_repo_sends_only_set_fields(mcp_client, respx_mock):
    """Verify PATCH body excludes fields the caller didn't set."""
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            200,
            json={
                "full_name": "example/demo-repo",
                "description": "new desc",
                "default_branch": "main",
                "archived": False,
                "private": False,
                "has_issues": True,
                "has_wiki": True,
                "has_pull_requests": True,
            },
        )

    respx_mock.patch("/repos/example/demo-repo").mock(side_effect=capture)
    result = await server.edit_repo(
        owner="example", repo="demo-repo", description="new desc"
    )
    assert captured["body"] == {"description": "new desc"}
    assert result["description"] == "new desc"


async def test_edit_repo_sends_multiple_fields(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            200,
            json={
                "full_name": "example/demo-repo",
                "description": "",
                "default_branch": "develop",
                "archived": True,
                "private": False,
                "has_issues": False,
                "has_wiki": True,
                "has_pull_requests": True,
            },
        )

    respx_mock.patch("/repos/example/demo-repo").mock(side_effect=capture)
    result = await server.edit_repo(
        owner="example",
        repo="demo-repo",
        default_branch="develop",
        archived=True,
        has_issues=False,
    )
    assert captured["body"] == {
        "default_branch": "develop",
        "archived": True,
        "has_issues": False,
    }
    assert result["default_branch"] == "develop"
    assert result["archived"] is True


async def test_edit_repo_no_fields_sends_empty_body(mcp_client, respx_mock):
    """No-op PATCH; Forgejo returns unchanged repo."""
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            200,
            json={
                "full_name": "example/demo-repo",
                "description": "unchanged",
                "default_branch": "main",
                "archived": False,
                "private": False,
                "has_issues": True,
                "has_wiki": True,
                "has_pull_requests": True,
            },
        )

    respx_mock.patch("/repos/example/demo-repo").mock(side_effect=capture)
    await server.edit_repo(owner="example", repo="demo-repo")
    assert captured["body"] == {}


async def test_edit_repo_error_surfaces_403(mcp_client, respx_mock):
    respx_mock.patch("/repos/other/repo").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await server.edit_repo(owner="other", repo="repo", archived=True)
    assert result["status"] == 403
    assert "403" in result["error"]


# ── list_issue_labels ───────────────────────────────────────────────────────


async def test_list_issue_labels_returns_labels(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/issues/3/labels").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "bug", "color": "ff0000", "description": ""},
                {
                    "id": 2,
                    "name": "wip",
                    "color": "00ff00",
                    "description": "work-in-progress",
                },
            ],
        )
    )
    result = await server.list_issue_labels(owner="example", repo="demo-repo", index=3)
    assert len(result) == 2
    assert result[0]["name"] == "bug"
    assert result[1]["description"] == "work-in-progress"


# ── add_issue_labels ────────────────────────────────────────────────────────


async def test_add_issue_labels_posts_label_ids(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"id": 1, "name": "bug", "color": "ff0000", "description": ""},
                {"id": 5, "name": "p1", "color": "ffaa00", "description": ""},
            ],
        )

    respx_mock.post("/repos/example/demo-repo/issues/3/labels").mock(
        side_effect=capture
    )
    result = await server.add_issue_labels(
        owner="example", repo="demo-repo", index=3, label_ids=[1, 5]
    )
    assert captured["body"] == {"labels": [1, 5]}
    assert len(result) == 2
    assert {lbl["name"] for lbl in result} == {"bug", "p1"}


async def test_add_issue_labels_empty_list_is_acceptable(mcp_client, respx_mock):
    respx_mock.post("/repos/example/demo-repo/issues/3/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await server.add_issue_labels(
        owner="example", repo="demo-repo", index=3, label_ids=[]
    )
    assert result == []


# ── remove_issue_label ──────────────────────────────────────────────────────


async def test_remove_issue_label_returns_none_on_204(mcp_client, respx_mock):
    respx_mock.delete("/repos/example/demo-repo/issues/3/labels/5").mock(
        return_value=httpx.Response(204)
    )
    result = await server.remove_issue_label(
        owner="example", repo="demo-repo", index=3, label_id=5
    )
    assert result is None


async def test_remove_issue_label_404_when_not_applied(
    mcp_client, respx_mock, tool_error
):
    respx_mock.delete("/repos/example/demo-repo/issues/3/labels/99").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await tool_error(
        server.remove_issue_label(
            owner="example", repo="demo-repo", index=3, label_id=99
        )
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── list_releases ───────────────────────────────────────────────────────────


async def test_list_releases_single_page(mcp_client, respx_mock):
    respx_mock.get(
        "/repos/example/demo-repo/releases",
        params={"limit": 20, "page": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "tag_name": "v2.0.1",
                    "name": "v2.0.1 — patch",
                    "body": "fixes #123",
                    "draft": False,
                    "prerelease": False,
                    "created_at": "2026-04-26T10:00:00Z",
                    "published_at": "2026-04-26T10:00:00Z",
                    "html_url": "https://codeberg.org/example/demo-repo/releases/tag/v2.0.1",
                    "target_commitish": "main",
                }
            ],
        )
    )
    result = await server.list_releases(owner="example", repo="demo-repo")
    assert len(result) == 1
    assert result[0]["tag_name"] == "v2.0.1"
    assert result[0]["draft"] is False


async def test_list_releases_paginates_when_all_true(mcp_client, respx_mock):
    respx_mock.get(
        "/repos/example/demo-repo/releases",
        params={"limit": 20, "page": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "tag_name": "v1",
                    "name": "v1",
                    "body": "",
                    "draft": False,
                    "prerelease": False,
                    "created_at": "",
                    "published_at": "",
                    "html_url": "",
                    "target_commitish": "main",
                }
            ],
        )
    )
    respx_mock.get(
        "/repos/example/demo-repo/releases",
        params={"limit": 20, "page": 2},
    ).mock(return_value=httpx.Response(200, json=[]))
    result = await server.list_releases(owner="example", repo="demo-repo", all=True)
    assert len(result) == 1


# ── get_release ─────────────────────────────────────────────────────────────


async def test_get_release_by_id(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/releases/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "tag_name": "v2.0.1",
                "name": "v2.0.1",
                "body": "patch notes",
                "draft": False,
                "prerelease": False,
                "created_at": "2026-04-26T10:00:00Z",
                "published_at": "2026-04-26T10:00:00Z",
                "html_url": "",
                "target_commitish": "main",
            },
        )
    )
    result = await server.get_release(owner="example", repo="demo-repo", release_id=42)
    assert result["id"] == 42
    assert result["tag_name"] == "v2.0.1"


# ── get_release_by_tag ──────────────────────────────────────────────────────


async def test_get_release_by_tag(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/releases/tags/v2.0.1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "tag_name": "v2.0.1",
                "name": "v2.0.1",
                "body": "patch notes",
                "draft": False,
                "prerelease": False,
                "created_at": "2026-04-26T10:00:00Z",
                "published_at": "2026-04-26T10:00:00Z",
                "html_url": "",
                "target_commitish": "main",
            },
        )
    )
    result = await server.get_release_by_tag(
        owner="example", repo="demo-repo", tag="v2.0.1"
    )
    assert result["tag_name"] == "v2.0.1"


async def test_get_release_by_tag_404(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/releases/tags/v999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await server.get_release_by_tag(
        owner="example", repo="demo-repo", tag="v999"
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── create_release ──────────────────────────────────────────────────────────


async def test_create_release_posts_payload(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": 99,
                "tag_name": "v3.0.0",
                "name": "v3.0.0 — major",
                "body": "release notes",
                "draft": False,
                "prerelease": False,
                "created_at": "2026-05-12T15:00:00Z",
                "published_at": "2026-05-12T15:00:00Z",
                "html_url": "",
                "target_commitish": "main",
            },
        )

    respx_mock.post("/repos/example/demo-repo/releases").mock(side_effect=capture)
    result = await server.create_release(
        owner="example",
        repo="demo-repo",
        tag_name="v3.0.0",
        name="v3.0.0 — major",
        body="release notes",
    )
    assert captured["body"]["tag_name"] == "v3.0.0"
    assert captured["body"]["name"] == "v3.0.0 — major"
    assert captured["body"]["body"] == "release notes"
    assert captured["body"]["draft"] is False
    assert captured["body"]["prerelease"] is False
    assert result["id"] == 99


async def test_create_release_409_on_existing_tag(mcp_client, respx_mock):
    respx_mock.post("/repos/example/demo-repo/releases").mock(
        return_value=httpx.Response(
            409, json={"message": "release for tag already exists"}
        )
    )
    result = await server.create_release(
        owner="example", repo="demo-repo", tag_name="v2.0.1"
    )
    assert result["status"] == 409
    assert "409" in result["error"]


# ── edit_release ────────────────────────────────────────────────────────────


async def test_edit_release_sends_only_set_fields(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": 42,
                "tag_name": "v2.0.1",
                "name": "v2.0.1 — patched",
                "body": "new notes",
                "draft": False,
                "prerelease": False,
                "created_at": "",
                "published_at": "",
                "html_url": "",
                "target_commitish": "main",
            },
        )

    respx_mock.patch("/repos/example/demo-repo/releases/42").mock(side_effect=capture)
    result = await server.edit_release(
        owner="example",
        repo="demo-repo",
        release_id=42,
        name="v2.0.1 — patched",
        body="new notes",
    )
    assert captured["body"] == {"name": "v2.0.1 — patched", "body": "new notes"}
    assert result["name"] == "v2.0.1 — patched"


# ── list_tags ───────────────────────────────────────────────────────────────


async def test_list_tags_single_page(mcp_client, respx_mock):
    respx_mock.get(
        "/repos/example/demo-repo/tags",
        params={"limit": 20, "page": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "v2.0.1",
                    "id": "abc123",
                    "commit": {
                        "sha": "abc123",
                        "url": "https://codeberg.org/example/demo-repo/commit/abc123",
                    },
                    "message": "release v2.0.1",
                    "tagger": {"name": "example", "date": "2026-04-26T10:00:00Z"},
                }
            ],
        )
    )
    result = await server.list_tags(owner="example", repo="demo-repo")
    assert len(result) == 1
    assert result[0]["name"] == "v2.0.1"
    assert result[0]["sha"] == "abc123"


# ── get_tag ─────────────────────────────────────────────────────────────────


async def test_get_tag_returns_tag_metadata(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/tags/v2.0.1").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "v2.0.1",
                "id": "abc123",
                "commit": {
                    "sha": "abc123",
                    "url": "https://codeberg.org/example/demo-repo/commit/abc123",
                },
                "message": "release v2.0.1",
                "tagger": {"name": "example", "date": "2026-04-26T10:00:00Z"},
            },
        )
    )
    result = await server.get_tag(owner="example", repo="demo-repo", tag="v2.0.1")
    assert result["name"] == "v2.0.1"
    assert result["sha"] == "abc123"
    assert result["tagger_name"] == "example"


# ── list_actions_variables ──────────────────────────────────────────────────


async def test_list_actions_variables(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/actions/variables").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "IMAGE_TAG", "data": "v2.0.1"},
                {"name": "FEATURE_X", "data": "true"},
            ],
        )
    )
    result = await server.list_actions_variables(owner="example", repo="demo-repo")
    assert len(result) == 2
    assert {v["name"] for v in result} == {"IMAGE_TAG", "FEATURE_X"}
    assert result[0]["value"] in ("v2.0.1", "true")


# ── get_actions_variable ────────────────────────────────────────────────────


async def test_get_actions_variable(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/actions/variables/IMAGE_TAG").mock(
        return_value=httpx.Response(200, json={"name": "IMAGE_TAG", "data": "v2.0.1"})
    )
    result = await server.get_actions_variable(
        owner="example", repo="demo-repo", name="IMAGE_TAG"
    )
    assert result["name"] == "IMAGE_TAG"
    assert result["value"] == "v2.0.1"


async def test_get_actions_variable_404(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo/actions/variables/MISSING").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await server.get_actions_variable(
        owner="example", repo="demo-repo", name="MISSING"
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── create_actions_variable ─────────────────────────────────────────────────


async def test_create_actions_variable_posts_value(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(204)

    respx_mock.post("/repos/example/demo-repo/actions/variables/IMAGE_TAG").mock(
        side_effect=capture
    )
    result = await server.create_actions_variable(
        owner="example", repo="demo-repo", name="IMAGE_TAG", value="v2.0.1"
    )
    assert captured["body"] == {"value": "v2.0.1"}
    assert result is None


async def test_create_actions_variable_409_on_existing(
    mcp_client, respx_mock, tool_error
):
    respx_mock.post("/repos/example/demo-repo/actions/variables/IMAGE_TAG").mock(
        return_value=httpx.Response(409, json={"message": "variable already exists"})
    )
    result = await tool_error(
        server.create_actions_variable(
            owner="example", repo="demo-repo", name="IMAGE_TAG", value="v3"
        )
    )
    assert result["status"] == 409
    assert "409" in result["error"]


# ── update_actions_variable ─────────────────────────────────────────────────


async def test_update_actions_variable_puts_value(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(204)

    respx_mock.put("/repos/example/demo-repo/actions/variables/IMAGE_TAG").mock(
        side_effect=capture
    )
    result = await server.update_actions_variable(
        owner="example", repo="demo-repo", name="IMAGE_TAG", value="v3.0.0"
    )
    assert captured["body"] == {"value": "v3.0.0"}
    assert result is None


async def test_update_actions_variable_404_when_missing(
    mcp_client, respx_mock, tool_error
):
    respx_mock.put("/repos/example/demo-repo/actions/variables/MISSING").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    result = await tool_error(
        server.update_actions_variable(
            owner="example", repo="demo-repo", name="MISSING", value="x"
        )
    )
    assert result["status"] == 404
    assert "404" in result["error"]


# ── multi-account spot checks ───────────────────────────────────────────────


async def test_edit_repo_routes_through_work_token(mcp_client, respx_mock):
    """Verify account='work' sends the fede token, not the example one."""
    captured: dict = {}

    def capture(request):
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200,
            json={
                "full_name": "work/some-repo",
                "description": "x",
                "default_branch": "main",
                "archived": False,
                "private": False,
                "has_issues": True,
                "has_wiki": True,
                "has_pull_requests": True,
            },
        )

    respx_mock.patch("/repos/work/some-repo").mock(side_effect=capture)
    await server.edit_repo(
        owner="work",
        repo="some-repo",
        description="x",
        account="work",
    )
    assert captured["auth"] == "token fake-token-fede"


async def test_list_releases_routes_through_default_example_token(
    mcp_client, respx_mock
):
    captured: dict = {}

    def capture(request):
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[])

    respx_mock.get("/repos/example/demo-repo/releases").mock(side_effect=capture)
    await server.list_releases(owner="example", repo="demo-repo")
    assert captured["auth"] == "token fake-token-example"


# ── edit_issue ──────────────────────────────────────────────────────────────


def _issue_json(**overrides):
    base = {
        "number": 13,
        "title": "original title",
        "body": "original body",
        "state": "open",
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "example"}],
        "created_at": "2026-05-16T00:00:00Z",
        "updated_at": "2026-05-16T01:00:00Z",
        "html_url": "https://codeberg.org/example/demo-repo/issues/13",
    }
    base.update(overrides)
    return base


async def test_edit_issue_sends_only_set_fields(mcp_client, respx_mock):
    """Body-only edit must not clobber title/state."""
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(200, json=_issue_json(body="new body"))

    respx_mock.patch("/repos/example/demo-repo/issues/13").mock(side_effect=capture)
    result = await server.edit_issue(
        owner="example", repo="demo-repo", issue_number=13, body="new body"
    )
    assert captured["body"] == {"body": "new body"}
    assert result["body"] == "new body"
    assert result["number"] == 13
    assert result["title"] == "original title"
    # Lock the full projection shape (shared _project_issue with get_issue):
    assert result["labels"] == ["bug"]
    assert result["assignees"] == ["example"]
    assert result["state"] == "open"
    assert result["created"] == "2026-05-16T00:00:00Z"
    assert result["updated"] == "2026-05-16T01:00:00Z"
    assert result["url"] == "https://codeberg.org/example/demo-repo/issues/13"


async def test_edit_issue_sends_multiple_fields(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(200, json=_issue_json(title="renamed", state="closed"))

    respx_mock.patch("/repos/example/demo-repo/issues/13").mock(side_effect=capture)
    result = await server.edit_issue(
        owner="example",
        repo="demo-repo",
        issue_number=13,
        title="renamed",
        state="closed",
    )
    assert captured["body"] == {"title": "renamed", "state": "closed"}
    assert "body" not in captured["body"]
    assert result["title"] == "renamed"
    assert result["state"] == "closed"


async def test_edit_issue_no_fields_sends_empty_body(mcp_client, respx_mock):
    captured: dict = {}

    def capture(request):
        captured["body"] = json_lib.loads(request.content)
        return httpx.Response(200, json=_issue_json())

    respx_mock.patch("/repos/example/demo-repo/issues/13").mock(side_effect=capture)
    await server.edit_issue(owner="example", repo="demo-repo", issue_number=13)
    assert captured["body"] == {}


async def test_edit_issue_error_surfaces_403(mcp_client, respx_mock):
    """work has no issue scope — PATCH should surface 403."""
    respx_mock.patch("/repos/other/repo/issues/1").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await server.edit_issue(
        owner="other", repo="repo", issue_number=1, state="closed"
    )
    assert result["status"] == 403
    assert "403" in result["error"]
