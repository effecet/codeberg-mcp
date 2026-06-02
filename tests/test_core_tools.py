"""Coverage for tools not exercised by test_new_tools / test_ergonomics:

- _get_client account selection (incl. unknown account)
- get_repo / create_repo shape
- get_file / create_file / update_file / delete_file — base64 round-trip,
  file-vs-dir dispatch
- list_dir — dispatch + shape
- list_pulls / get_pull / create_pull / merge_pull — shape + method validation
- create_branch — payload shape
"""

from __future__ import annotations

import base64
import json as json_lib

import httpx
import pytest

import server


# ── _get_client ─────────────────────────────────────────────────────────────


def test_get_client_returns_default_account_when_not_specified(mcp_client):
    client, auth = server._get_client()
    assert client is server._client
    assert auth["Authorization"] == "token fake-token-example"


def test_get_client_switches_account_when_specified(mcp_client):
    _client, auth = server._get_client(account="work")
    assert auth["Authorization"] == "token fake-token-fede"


def test_get_client_rejects_unknown_account(mcp_client):
    with pytest.raises(RuntimeError, match="Unknown account 'nobody'"):
        server._get_client(account="nobody")


def test_get_client_raises_when_client_uninitialized(fake_accounts):
    # fake_accounts sets env only — client is not wired via mcp_client fixture
    saved = server._client
    server._client = None
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            server._get_client()
    finally:
        server._client = saved


# ── get_repo / create_repo ──────────────────────────────────────────────────


async def test_get_repo_returns_normalized_shape(mcp_client, respx_mock):
    respx_mock.get("/repos/example/demo-repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "example/demo-repo",
                "description": "brain",
                "private": True,
                "html_url": "https://codeberg.org/example/demo-repo",
                "clone_url": "https://codeberg.org/example/demo-repo.git",
                "ssh_url": "ssh://git@codeberg.org/example/demo-repo.git",
                "default_branch": "main",
                "stars_count": 3,
                "forks_count": 0,
                "open_issues_count": 1,
                "language": "TypeScript",
                "topics": ["mcp", "memory"],
                "updated": "2026-04-17T10:00:00Z",
            },
        )
    )
    r = await server.get_repo(owner="example", repo="demo-repo")
    assert r["full_name"] == "example/demo-repo"
    assert r["private"] is True
    assert r["default_branch"] == "main"
    assert r["topics"] == ["mcp", "memory"]
    assert r["stars_count"] == 3


async def test_create_repo_passes_license_and_gitignores(mcp_client, respx_mock):
    route = respx_mock.post("/user/repos").mock(
        return_value=httpx.Response(
            201,
            json={
                "full_name": "example/new-repo",
                "html_url": "https://codeberg.org/example/new-repo",
                "clone_url": "",
                "ssh_url": "",
                "private": False,
                "default_branch": "main",
            },
        )
    )
    await server.create_repo(
        name="new-repo",
        description="demo",
        gitignores="Python,Node",
        license_template="MIT",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["name"] == "new-repo"
    assert sent["gitignores"] == "Python,Node"
    assert sent["license"] == "MIT"
    assert sent["auto_init"] is True


async def test_create_repo_omits_optional_fields(mcp_client, respx_mock):
    route = respx_mock.post("/user/repos").mock(
        return_value=httpx.Response(
            201,
            json={
                "full_name": "example/min-repo",
                "html_url": "",
                "private": False,
                "default_branch": "main",
            },
        )
    )
    await server.create_repo(name="min-repo")
    sent = json_lib.loads(route.calls[0].request.content)
    assert "gitignores" not in sent
    assert "license" not in sent


# ── get_file / create_file / update_file / delete_file ──────────────────────


async def test_get_file_decodes_base64_content(mcp_client, respx_mock):
    raw = "hello\nworld\n"
    encoded = base64.b64encode(raw.encode()).decode("ascii")
    respx_mock.get("/repos/example/brain/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "path": "README.md",
                "content": encoded,
                "sha": "file-sha",
                "size": len(raw),
                "html_url": "",
            },
        )
    )
    r = await server.get_file(owner="example", repo="brain", path="README.md")
    assert r["content"] == raw
    assert r["sha"] == "file-sha"
    assert r["encoding"] == "utf-8"


async def test_get_file_handles_linewrapped_base64(mcp_client, respx_mock):
    # Gitea may return base64 with embedded newlines
    raw = "hello"
    encoded = base64.b64encode(raw.encode()).decode("ascii")
    wrapped = "\n".join([encoded[i : i + 2] for i in range(0, len(encoded), 2)])
    respx_mock.get("/repos/e/r/contents/a.txt").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "path": "a.txt",
                "content": wrapped,
                "sha": "s",
                "size": 5,
                "html_url": "",
            },
        )
    )
    r = await server.get_file(owner="e", repo="r", path="a.txt")
    assert r["content"] == raw


async def test_get_file_rejects_directory(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/contents/src").mock(
        return_value=httpx.Response(200, json={"type": "dir"})
    )
    with pytest.raises(ValueError, match="is a directory"):
        await server.get_file(owner="e", repo="r", path="src")


async def test_create_file_base64_encodes_content(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/contents/new.txt").mock(
        return_value=httpx.Response(
            201,
            json={
                "content": {"path": "new.txt", "sha": "new-sha", "html_url": ""},
                "commit": {"sha": "commit-sha", "message": "add new"},
            },
        )
    )
    await server.create_file(
        owner="e",
        repo="r",
        path="new.txt",
        content="hello world",
        message="add new",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["message"] == "add new"
    assert base64.b64decode(sent["content"]).decode() == "hello world"


async def test_create_file_attaches_author_when_both_provided(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/contents/x").mock(
        return_value=httpx.Response(
            201,
            json={
                "content": {"path": "x", "sha": "s", "html_url": ""},
                "commit": {"sha": "c", "message": "m"},
            },
        )
    )
    await server.create_file(
        owner="e",
        repo="r",
        path="x",
        content="y",
        message="m",
        author_name="Ada",
        author_email="a@e.com",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["author"] == {"name": "Ada", "email": "a@e.com"}


async def test_create_file_omits_author_when_only_one_provided(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/contents/x").mock(
        return_value=httpx.Response(
            201,
            json={
                "content": {"path": "x", "sha": "s", "html_url": ""},
                "commit": {"sha": "c", "message": "m"},
            },
        )
    )
    await server.create_file(
        owner="e",
        repo="r",
        path="x",
        content="y",
        message="m",
        author_name="Ada",  # email missing
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert "author" not in sent


async def test_update_file_includes_sha_in_body(mcp_client, respx_mock):
    route = respx_mock.put("/repos/e/r/contents/x").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": {"path": "x", "sha": "new-sha", "html_url": ""},
                "commit": {"sha": "c", "message": "m"},
            },
        )
    )
    await server.update_file(
        owner="e",
        repo="r",
        path="x",
        content="updated",
        message="m",
        sha="old-sha",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["sha"] == "old-sha"
    assert base64.b64decode(sent["content"]).decode() == "updated"


async def test_delete_file_sends_sha_and_message(mcp_client, respx_mock):
    route = respx_mock.delete("/repos/e/r/contents/x").mock(
        return_value=httpx.Response(
            200,
            json={
                "commit": {"sha": "c", "message": "drop"},
            },
        )
    )
    r = await server.delete_file(
        owner="e",
        repo="r",
        path="x",
        message="drop",
        sha="s1",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["sha"] == "s1"
    assert sent["message"] == "drop"
    assert r["commit_sha"] == "c"


# ── list_dir ────────────────────────────────────────────────────────────────


async def test_list_dir_returns_normalized_entries(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/contents/src").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "a.py",
                    "path": "src/a.py",
                    "type": "file",
                    "sha": "s1",
                    "size": 10,
                },
                {"name": "sub", "path": "src/sub", "type": "dir", "sha": "s2"},
            ],
        )
    )
    r = await server.list_dir(owner="e", repo="r", path="src")
    assert len(r) == 2
    assert r[0]["name"] == "a.py"
    assert r[0]["size"] == 10
    assert r[1]["type"] == "dir"
    # dir entries have no size field — default to 0
    assert r[1]["size"] == 0


async def test_list_dir_rejects_single_file(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/contents/README.md").mock(
        return_value=httpx.Response(200, json={"type": "file"})
    )
    with pytest.raises(ValueError, match="is a file, not a directory"):
        await server.list_dir(owner="e", repo="r", path="README.md")


# ── pull request tools ──────────────────────────────────────────────────────


async def test_list_pulls_default_single_page(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "fix",
                    "state": "open",
                    "user": {"login": "e"},
                    "head": {"label": "f:fix"},
                    "base": {"label": "main"},
                    "html_url": "",
                    "created_at": "c",
                    "updated_at": "u",
                    "merged": False,
                },
            ],
        )
    )
    r = await server.list_pulls(owner="e", repo="r")
    assert len(r) == 1
    assert r[0]["number"] == 1
    assert r[0]["head_branch"] == "f:fix"
    assert r[0]["base_branch"] == "main"


async def test_get_pull_includes_merge_metadata(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/pulls/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "t",
                "body": "b",
                "state": "closed",
                "user": {"login": "e"},
                "head": {"label": "f"},
                "base": {"label": "main"},
                "html_url": "",
                "mergeable": True,
                "merged": True,
                "merged_by": {"login": "octocat"},
                "additions": 10,
                "deletions": 2,
                "changed_files": 3,
                "created_at": "",
                "updated_at": "",
            },
        )
    )
    r = await server.get_pull(owner="e", repo="r", index=42)
    assert r["merged"] is True
    assert r["merged_by"] == "octocat"
    assert r["changed_files"] == 3


async def test_get_pull_handles_no_merged_by(mcp_client, respx_mock):
    respx_mock.get("/repos/e/r/pulls/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 1,
                "title": "t",
                "state": "open",
                "user": {"login": "e"},
                "head": {"label": "f"},
                "base": {"label": "main"},
                "html_url": "",
                "merged": False,
                "merged_by": None,
            },
        )
    )
    r = await server.get_pull(owner="e", repo="r", index=1)
    assert r["merged_by"] == ""


async def test_create_pull_posts_body(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/pulls").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 1,
                "title": "t",
                "state": "open",
                "head": {"label": "feat"},
                "base": {"label": "main"},
                "html_url": "",
            },
        )
    )
    await server.create_pull(
        owner="e",
        repo="r",
        title="t",
        head="feat",
        base="main",
        body="desc",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent == {"title": "t", "head": "feat", "base": "main", "body": "desc"}


@pytest.mark.parametrize("method", ["merge", "rebase", "squash"])
async def test_merge_pull_accepts_valid_methods(mcp_client, respx_mock, method):
    route = respx_mock.post("/repos/e/r/pulls/1/merge").mock(
        return_value=httpx.Response(200, text="")
    )
    r = await server.merge_pull(owner="e", repo="r", index=1, method=method)
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent["do"] == method
    assert sent["delete_branch_after_merge"] is False
    assert r == {"merged": True, "method": method}


async def test_merge_pull_rejects_invalid_method(mcp_client):
    with pytest.raises(ValueError, match="Invalid merge method"):
        await server.merge_pull(owner="e", repo="r", index=1, method="force")


# ── branch tools ────────────────────────────────────────────────────────────


async def test_create_branch_with_old_branch(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/branches").mock(
        return_value=httpx.Response(
            201,
            json={
                "name": "feat",
                "commit": {"id": "abc"},
                "protected": False,
            },
        )
    )
    await server.create_branch(
        owner="e",
        repo="r",
        branch_name="feat",
        old_branch="main",
    )
    sent = json_lib.loads(route.calls[0].request.content)
    assert sent == {"new_branch_name": "feat", "old_branch_name": "main"}


async def test_create_branch_without_old_branch(mcp_client, respx_mock):
    route = respx_mock.post("/repos/e/r/branches").mock(
        return_value=httpx.Response(
            201,
            json={
                "name": "feat",
                "commit": {"id": "abc"},
                "protected": False,
            },
        )
    )
    await server.create_branch(owner="e", repo="r", branch_name="feat")
    sent = json_lib.loads(route.calls[0].request.content)
    assert "old_branch_name" not in sent
    assert sent["new_branch_name"] == "feat"


# ── _paginate direct unit ───────────────────────────────────────────────────


async def test_paginate_all_false_returns_single_page():
    async def fetch(p):
        return [p, p]  # dummy page

    r = await server._paginate(fetch, limit=10, page=3, all=False)
    assert r == [3, 3]


async def test_paginate_all_true_sweeps_until_empty():
    calls = []

    async def fetch(p):
        calls.append(p)
        return [{"p": p}] if p < 4 else []

    r = await server._paginate(fetch, limit=10, page=1, all=True)
    assert [x["p"] for x in r] == [1, 2, 3]
    assert calls == [1, 2, 3, 4]


async def test_paginate_all_true_stops_at_cap(caplog):
    async def fetch(p):
        return [{"p": p}]  # never empty, to force cap

    import logging

    caplog.set_level(logging.WARNING, logger="codeberg-mcp")
    r = await server._paginate(fetch, limit=5, page=1, all=True, cap_pages=3)
    assert len(r) == 3
    assert any("stopped at" in rec.message for rec in caplog.records)
