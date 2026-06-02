#!/usr/bin/env python3
"""
codeberg-mcp — MCP server for Codeberg (Gitea API v1)
crafted by effece 🧉

Multi-account support via CODEBERG_ACCOUNTS env var:
  {"personal": "tok_abc...", "work": "tok_xyz..."}

Default account: CODEBERG_DEFAULT_ACCOUNT (defaults to the first configured account)

Transports:
  HTTP/SSE  → for claude.ai custom connectors  (default: uvicorn server.py)
  stdio     → for Claude Code                   (MCP_TRANSPORT=stdio python server.py)

Tools:
  Repos    → list_repos, get_repo, create_repo
  Files    → get_file, create_file, update_file, delete_file, list_dir
  PRs      → list_pulls, get_pull, create_pull, merge_pull
  Branches → list_branches, create_branch
  Commits  → get_latest_commit, compare_refs
  Actions  → list_workflow_runs, get_workflow_run, get_workflow_logs
  Issues   → list_issues, get_issue, create_issue, comment_on_issue
  Account  → list_user_keys, list_user_emails
  Settings → set_repo_actions_enabled, list_repo_secrets
"""

import base64
import functools
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

# ── server ──────────────────────────────────────────────────────────────────

# Defaults to Codeberg; override for any Gitea/Forgejo instance via env.
BASE_URL = os.environ.get("CODEBERG_BASE_URL", "https://codeberg.org/api/v1")

# Shared httpx client — created once via lifespan, reuses connections
_client: httpx.AsyncClient | None = None
_accounts: dict[str, str] = {}
_default_account: str = ""


import logging  # noqa: E402

logger = logging.getLogger("codeberg-mcp")


async def _validate_accounts() -> None:
    """Hit GET /user for each configured account; log result. Never raises."""
    for name, token in _accounts.items():
        try:
            r = await _client.get("/user", headers={"Authorization": f"token {token}"})
            if r.is_success:
                login = r.json().get("login", "?")
                logger.info(f"account {name}: live ({login})")
            else:
                logger.warning(
                    f"account {name}: FAILED ({r.status_code} — token invalid?)"
                )
        except Exception as e:
            logger.warning(f"account {name}: FAILED (exception: {e})")


@asynccontextmanager
async def _lifespan(server):
    global _client, _accounts, _default_account

    raw = os.environ.get("CODEBERG_ACCOUNTS", "").strip()
    if not raw:
        raise RuntimeError(
            "CODEBERG_ACCOUNTS is not set. "
            'Expected JSON: {"personal": "tok_...", "work": "tok_..."}'
        )
    _accounts = json.loads(raw)
    if not _accounts:
        raise RuntimeError("CODEBERG_ACCOUNTS is empty.")

    # Default to CODEBERG_DEFAULT_ACCOUNT, else the first configured account.
    _default_account = os.environ.get("CODEBERG_DEFAULT_ACCOUNT") or next(
        iter(_accounts)
    )
    if _default_account not in _accounts:
        raise RuntimeError(
            f"Default account '{_default_account}' not found in CODEBERG_ACCOUNTS. "
            f"Available: {', '.join(_accounts.keys())}"
        )

    _client = httpx.AsyncClient(
        base_url=BASE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=20,
    )
    await _validate_accounts()
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


mcp = FastMCP(
    "codeberg",
    instructions=(
        "Interact with Codeberg repositories, files, pull requests, and branches "
        "via the Gitea REST API. Supports multiple accounts — use the 'account' "
        "parameter to switch. The default is the first account in CODEBERG_ACCOUNTS "
        "(or set CODEBERG_DEFAULT_ACCOUNT). All write operations "
        "(create/update/delete) require a "
        "commit message. update_file and delete_file require the current file "
        "sha — call get_file first. merge_pull supports merge, rebase, and squash. "
        "ERROR CONTRACT: on any Codeberg API failure a tool returns "
        '{"error": "Codeberg API <status>: <detail>", "status": <int>} '
        "instead of its normal result (including tools that normally return a "
        "list). Treat any result containing top-level 'error' and 'status' "
        "keys as a failure and read 'error' for the reason."
    ),
    lifespan=_lifespan,
)


def _get_client(account: str | None = None) -> tuple[httpx.AsyncClient, dict]:
    """Return the shared client and auth headers for the given account."""
    if _client is None:
        raise RuntimeError("Codeberg client not initialized — server not started?")
    name = account or _default_account
    token = _accounts.get(name)
    if not token:
        raise RuntimeError(
            f"Unknown account '{name}'. Available: {', '.join(_accounts.keys())}"
        )
    return _client, {"Authorization": f"token {token}"}


class CodebergAPIError(RuntimeError):
    """A non-2xx response from the Codeberg API.

    Subclasses RuntimeError so existing `pytest.raises(RuntimeError, ...)`
    unit tests on `_raise` keep passing, while carrying the structured
    `.status` / `.detail` the `catch_api_errors` decorator returns to the
    caller.
    """

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Codeberg API {status}: {detail}")


# Forgejo's POST /issues 500s above ~4k-char bodies (observed empirically
# across sessions; see memory reference_codeberg_create_issue_workaround).
# Soft limit — we still attempt, but annotate the result / error with the
# smoke-create + PATCH workaround so the caller isn't left guessing.
_ISSUE_BODY_SOFT_LIMIT = 3500


def _detail(response: httpx.Response) -> str:
    """Extract a human-readable detail string from a non-2xx response.

    JSON 'message' or 'error' field if present, else a truncated body.
    """
    try:
        body = response.json()
        return body.get("message") or body.get("error") or str(body)[:300]
    except Exception:
        return response.text[:300]


def _raise(response: httpx.Response) -> None:
    """Raise CodebergAPIError on non-2xx responses.

    The decorator turns this into a structured error result so the message
    reaches Claude instead of being eaten by FastMCP's wrapper.
    """
    if response.is_success:
        return
    raise CodebergAPIError(response.status_code, _detail(response))


def _safe_list(value: object) -> list:
    """Return `value` if it's a list, else `[]`.

    Forgejo returns `null` (not `[]`) for empty `labels` / `assignees` /
    similar collection fields. `dict.get(key, [])` does NOT guard this —
    the key exists with a null value, so the default is never used and
    `for x in None` raises `'NoneType' object is not iterable`.
    """
    return value if isinstance(value, list) else []


def catch_api_errors(fn):
    """Convert CodebergAPIError into a structured tool result.

    Applied UNDER `@mcp.tool()` so FastMCP introspects the original
    signature (functools.wraps preserves it). API errors become
    `{"error": str(e), "status": e.status}` — the message reaches the
    caller instead of being swallowed. Non-API
    exceptions (programming bugs) propagate unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except CodebergAPIError as e:
            return {"error": str(e), "status": e.status}

    return wrapper


async def _paginate(
    fetch_page,
    limit: int,
    page: int,
    all: bool,
    cap_pages: int = 10,
) -> list[dict]:
    """Either fetch one page, or sweep pages 1..cap_pages until empty.

    fetch_page(page) is a callable that fetches a single page and returns a
    list. Returns the flat list across all fetched pages.
    """
    if not all:
        return await fetch_page(page)
    out: list[dict] = []
    hit_cap = True
    for p in range(1, cap_pages + 1):
        batch = await fetch_page(p)
        if not batch:
            hit_cap = False
            break
        out.extend(batch)
    if hit_cap:
        logger.warning(
            f"all=True stopped at {cap_pages * limit} items (page {cap_pages}). "
            "Increase cap or use explicit pagination."
        )
    return out


# ── repo tools ───────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_repos(
    username: str | None = None,
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List repositories.

    If `username` is omitted, lists repos for the authenticated user.
    If `username` is provided, lists that user's public repos.

    Args:
        username: Codeberg username to query (optional).
        limit:    Max repos to return per page (default 20, max 50).
        page:     Page number for single-page fetch (default 1).
        all:      If true, auto-paginate until empty (cap 10 pages = 500 items).
        account:  Codeberg account to use (default: the configured default account).

    Returns:
        List of repo objects with keys: full_name, description, private,
        html_url, stars_count, forks_count, default_branch, updated.
    """
    client, auth = _get_client(account)
    url = f"/users/{username}/repos" if username else "/user/repos"

    async def fetch_page(p: int) -> list[dict]:
        r = await client.get(
            url, headers=auth, params={"limit": min(limit, 50), "page": p}
        )
        _raise(r)
        return [
            {
                "full_name": repo["full_name"],
                "description": repo.get("description", ""),
                "private": repo["private"],
                "html_url": repo["html_url"],
                "stars_count": repo.get("stars_count", 0),
                "forks_count": repo.get("forks_count", 0),
                "default_branch": repo.get("default_branch", "main"),
                "updated": repo.get("updated", ""),
            }
            for repo in r.json()
        ]

    return await _paginate(fetch_page, min(limit, 50), page, all)


@mcp.tool()
@catch_api_errors
async def get_repo(
    owner: str,
    repo: str,
    account: str | None = None,
) -> dict:
    """
    Get details for a single repository.

    Args:
        owner:   Repository owner (username or org).
        repo:    Repository name.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Repo object with full metadata.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}", headers=auth)
    _raise(r)
    data = r.json()

    return {
        "full_name": data["full_name"],
        "description": data.get("description", ""),
        "private": data["private"],
        "html_url": data["html_url"],
        "clone_url": data.get("clone_url", ""),
        "ssh_url": data.get("ssh_url", ""),
        "default_branch": data.get("default_branch", "main"),
        "stars_count": data.get("stars_count", 0),
        "forks_count": data.get("forks_count", 0),
        "open_issues_count": data.get("open_issues_count", 0),
        "language": data.get("language", ""),
        "topics": data.get("topics", []),
        "updated": data.get("updated", ""),
    }


@mcp.tool()
@catch_api_errors
async def create_repo(
    name: str,
    description: str = "",
    private: bool = False,
    auto_init: bool = True,
    default_branch: str = "main",
    gitignores: str = "",
    license_template: str = "",
    account: str | None = None,
) -> dict:
    """
    Create a new repository under the authenticated user's account.

    Args:
        name:             Repository name (no spaces, use hyphens).
        description:      Short description (optional).
        private:          Whether to make the repo private (default False).
        auto_init:        Initialize with a README (default True).
        default_branch:   Default branch name (default 'main').
        gitignores:       Comma-separated gitignore templates, e.g. 'Python,Node'.
        license_template: SPDX license identifier, e.g. 'MIT', 'GPL-3.0'.
        account:          Codeberg account to use (default: the configured default account).

    Returns:
        Created repo object with full_name, html_url, clone_url, ssh_url.
    """
    payload = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init,
        "default_branch": default_branch,
    }
    if gitignores:
        payload["gitignores"] = gitignores
    if license_template:
        payload["license"] = license_template

    client, auth = _get_client(account)
    r = await client.post("/user/repos", headers=auth, json=payload)
    _raise(r)
    data = r.json()

    return {
        "full_name": data["full_name"],
        "html_url": data["html_url"],
        "clone_url": data.get("clone_url", ""),
        "ssh_url": data.get("ssh_url", ""),
        "private": data["private"],
        "default_branch": data.get("default_branch", "main"),
    }


# ── file tools ───────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def get_file(
    owner: str,
    repo: str,
    path: str,
    ref: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Read a file's contents from a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        path:    File path within the repo, e.g. 'src/main.py'.
        ref:     Branch, tag, or commit SHA (defaults to repo's default branch).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Dict with 'path', 'content' (decoded text), 'sha', 'size', 'html_url'.
    """
    params = {}
    if ref:
        params["ref"] = ref

    client, auth = _get_client(account)
    r = await client.get(
        f"/repos/{owner}/{repo}/contents/{path}", headers=auth, params=params
    )
    _raise(r)
    data = r.json()

    if data.get("type") == "dir":
        raise ValueError(
            f"'{path}' is a directory. Use list_dir to browse directories."
        )

    raw = data.get("content", "")
    # Gitea returns base64 content with potential newlines
    decoded = base64.b64decode(raw.replace("\n", "")).decode("utf-8", errors="replace")

    return {
        "path": data["path"],
        "content": decoded,
        "sha": data["sha"],
        "size": data.get("size", 0),
        "encoding": "utf-8",
        "html_url": data.get("html_url", ""),
    }


@mcp.tool()
@catch_api_errors
async def create_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Create a new file in a repository.

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        path:         File path within the repo, e.g. 'docs/notes.md'.
        content:      Plain text content to write (will be base64-encoded).
        message:      Commit message.
        branch:       Target branch (defaults to repo's default branch).
        author_name:  Commit author name (defaults to token owner's name).
        author_email: Commit author email (defaults to token owner's email).
        account:      Codeberg account to use (default: the configured default account).

    Returns:
        Dict with 'path', 'sha', 'html_url', 'commit_sha'.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict = {"message": message, "content": encoded}
    if branch:
        payload["branch"] = branch
    if author_name and author_email:
        payload["author"] = {"name": author_name, "email": author_email}

    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/contents/{path}", headers=auth, json=payload
    )
    _raise(r)
    data = r.json()

    return {
        "path": data["content"]["path"],
        "sha": data["content"]["sha"],
        "html_url": data["content"].get("html_url", ""),
        "commit_sha": data["commit"]["sha"],
        "commit_message": data["commit"]["message"],
    }


@mcp.tool()
@catch_api_errors
async def update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    sha: str,
    branch: str | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Update an existing file in a repository.

    The `sha` of the current file is required to prevent conflicts.
    Use get_file first to retrieve the current sha.

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        path:         File path within the repo.
        content:      New plain text content (will be base64-encoded).
        message:      Commit message.
        sha:          Current file SHA (from get_file).
        branch:       Target branch (defaults to repo's default branch).
        author_name:  Commit author name (optional).
        author_email: Commit author email (optional).
        account:      Codeberg account to use (default: the configured default account).

    Returns:
        Dict with 'path', 'sha', 'html_url', 'commit_sha'.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict = {"message": message, "content": encoded, "sha": sha}
    if branch:
        payload["branch"] = branch
    if author_name and author_email:
        payload["author"] = {"name": author_name, "email": author_email}

    client, auth = _get_client(account)
    r = await client.put(
        f"/repos/{owner}/{repo}/contents/{path}", headers=auth, json=payload
    )
    _raise(r)
    data = r.json()

    return {
        "path": data["content"]["path"],
        "sha": data["content"]["sha"],
        "html_url": data["content"].get("html_url", ""),
        "commit_sha": data["commit"]["sha"],
        "commit_message": data["commit"]["message"],
    }


@mcp.tool()
@catch_api_errors
async def delete_file(
    owner: str,
    repo: str,
    path: str,
    message: str,
    sha: str,
    branch: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Delete a file from a repository.

    The `sha` of the current file is required.
    Use get_file first to retrieve the current sha.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        path:    File path within the repo.
        message: Commit message.
        sha:     Current file SHA (from get_file).
        branch:  Target branch (defaults to repo's default branch).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Dict with 'commit_sha' and 'commit_message'.
    """
    payload: dict = {"message": message, "sha": sha}
    if branch:
        payload["branch"] = branch

    client, auth = _get_client(account)
    r = await client.request(
        "DELETE",
        f"/repos/{owner}/{repo}/contents/{path}",
        headers=auth,
        json=payload,
    )
    _raise(r)
    data = r.json()

    return {
        "commit_sha": data["commit"]["sha"],
        "commit_message": data["commit"]["message"],
    }


@mcp.tool()
@catch_api_errors
async def list_dir(
    owner: str,
    repo: str,
    path: str = "",
    ref: str | None = None,
    account: str | None = None,
) -> list[dict]:
    """
    List the contents of a directory in a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        path:    Directory path (empty string = repo root).
        ref:     Branch, tag, or commit SHA (defaults to default branch).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of entries with 'name', 'path', 'type' ('file'|'dir'), 'sha', 'size'.
    """
    params = {}
    if ref:
        params["ref"] = ref

    client, auth = _get_client(account)
    r = await client.get(
        f"/repos/{owner}/{repo}/contents/{path}", headers=auth, params=params
    )
    _raise(r)
    data = r.json()

    if isinstance(data, dict):
        raise ValueError(f"'{path}' is a file, not a directory. Use get_file instead.")

    return [
        {
            "name": entry["name"],
            "path": entry["path"],
            "type": entry["type"],
            "sha": entry["sha"],
            "size": entry.get("size", 0),
        }
        for entry in data
    ]


# ── pull request tools ──────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_pulls(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List pull requests for a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        state:   Filter by state: 'open', 'closed', or 'all' (default 'open').
        limit:   Max results per page (default 20, max 50).
        page:    Page number for single-page fetch (default 1).
        all:     If true, auto-paginate until empty (cap 10 pages = 500 items).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of PR objects with keys: number, title, state, head_branch,
        base_branch, user, html_url, created, updated, merged.
    """
    client, auth = _get_client(account)

    async def fetch_page(p: int) -> list[dict]:
        r = await client.get(
            f"/repos/{owner}/{repo}/pulls",
            headers=auth,
            params={"state": state, "limit": min(limit, 50), "page": p},
        )
        _raise(r)
        return [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "head_branch": pr["head"]["label"],
                "base_branch": pr["base"]["label"],
                "user": pr["user"]["login"],
                "html_url": pr["html_url"],
                "created": pr.get("created_at", ""),
                "updated": pr.get("updated_at", ""),
                "merged": pr.get("merged", False),
            }
            for pr in r.json()
        ]

    return await _paginate(fetch_page, min(limit, 50), page, all)


@mcp.tool()
@catch_api_errors
async def get_pull(
    owner: str,
    repo: str,
    index: int,
    account: str | None = None,
) -> dict:
    """
    Get details for a single pull request.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   PR number.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        PR object with full metadata including mergeable status.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/pulls/{index}", headers=auth)
    _raise(r)
    pr = r.json()

    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body", ""),
        "state": pr["state"],
        "head_branch": pr["head"]["label"],
        "base_branch": pr["base"]["label"],
        "user": pr["user"]["login"],
        "html_url": pr["html_url"],
        "mergeable": pr.get("mergeable", None),
        "merged": pr.get("merged", False),
        "merged_by": pr.get("merged_by", {}).get("login", "")
        if pr.get("merged_by")
        else "",
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "created": pr.get("created_at", ""),
        "updated": pr.get("updated_at", ""),
    }


@mcp.tool()
@catch_api_errors
async def create_pull(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    account: str | None = None,
) -> dict:
    """
    Create a pull request.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        title:   PR title.
        head:    Source branch name.
        base:    Target branch name (e.g. 'main').
        body:    PR description (optional).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Created PR object with number, title, html_url.
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/pulls",
        headers=auth,
        json={"title": title, "head": head, "base": base, "body": body},
    )
    _raise(r)
    pr = r.json()

    return {
        "number": pr["number"],
        "title": pr["title"],
        "state": pr["state"],
        "html_url": pr["html_url"],
        "head_branch": pr["head"]["label"],
        "base_branch": pr["base"]["label"],
    }


@mcp.tool()
@catch_api_errors
async def merge_pull(
    owner: str,
    repo: str,
    index: int,
    method: str = "merge",
    account: str | None = None,
) -> dict:
    """
    Merge a pull request. Branch deletion is never automatic — handle manually.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   PR number.
        method:  Merge method: 'merge', 'rebase', or 'squash' (default 'merge').
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Dict with merged status and method used.
    """
    if method not in ("merge", "rebase", "squash"):
        raise ValueError(
            f"Invalid merge method '{method}'. Use 'merge', 'rebase', or 'squash'."
        )

    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/pulls/{index}/merge",
        headers=auth,
        json={"do": method, "delete_branch_after_merge": False},
    )
    _raise(r)

    return {"merged": True, "method": method}


# ── branch tools ────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_branches(
    owner: str,
    repo: str,
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List branches for a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        limit:   Max results per page (default 20, max 50).
        page:    Page number for single-page fetch (default 1).
        all:     If true, auto-paginate until empty (cap 10 pages = 500 items).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of branch objects with name, commit SHA, and protected status.
    """
    client, auth = _get_client(account)

    async def fetch_page(p: int) -> list[dict]:
        r = await client.get(
            f"/repos/{owner}/{repo}/branches",
            headers=auth,
            params={"limit": min(limit, 50), "page": p},
        )
        _raise(r)
        return [
            {
                "name": b["name"],
                "commit_sha": b["commit"]["id"],
                "commit_message": b["commit"].get("message", "").split("\n")[0],
                "protected": b.get("protected", False),
            }
            for b in r.json()
        ]

    return await _paginate(fetch_page, min(limit, 50), page, all)


@mcp.tool()
@catch_api_errors
async def create_branch(
    owner: str,
    repo: str,
    branch_name: str,
    old_branch: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Create a new branch.

    Args:
        owner:       Repository owner.
        repo:        Repository name.
        branch_name: Name for the new branch.
        old_branch:  Source branch to create from (defaults to repo's default branch).
        account:     Codeberg account to use (default: the configured default account).

    Returns:
        Created branch object with name and commit SHA.
    """
    payload: dict = {"new_branch_name": branch_name}
    if old_branch:
        payload["old_branch_name"] = old_branch

    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/branches",
        headers=auth,
        json=payload,
    )
    _raise(r)
    b = r.json()

    return {
        "name": b["name"],
        "commit_sha": b["commit"]["id"],
        "protected": b.get("protected", False),
    }


# ── commit tools ─────────────────────────────────────────────────────────────


async def _get_default_branch(owner: str, repo: str, account: str | None) -> str:
    """Internal helper: fetch the repo's default branch via /repos/{o}/{r}."""
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}", headers=auth)
    _raise(r)
    return r.json().get("default_branch", "main")


@mcp.tool()
@catch_api_errors
async def get_latest_commit(
    owner: str,
    repo: str,
    branch: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Return the latest commit on a branch. If `branch` is None, resolves to
    the repo's default branch via a lookup.

    Args:
        owner:   Repository owner (username or org).
        repo:    Repository name.
        branch:  Branch name (optional — defaults to repo's default_branch).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        {sha, message, author, committer, timestamp, url}
    """
    if branch is None:
        branch = await _get_default_branch(owner, repo, account)
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/branches/{branch}", headers=auth)
    _raise(r)
    commit = r.json()["commit"]
    return {
        "sha": commit["id"],
        "message": commit["message"],
        "author": commit["author"],
        "committer": commit["committer"],
        "timestamp": commit.get("timestamp", ""),
        "url": commit.get("url", ""),
    }


@mcp.tool()
@catch_api_errors
async def compare_refs(
    owner: str,
    repo: str,
    base: str,
    head: str,
    account: str | None = None,
) -> dict:
    """
    Compare two refs (branches, tags, or SHAs) and return the diff summary.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        base:    Base ref (older).
        head:    Head ref (newer).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        {total_commits, commits: [{sha, message, url}],
         files: [{filename, status, additions, deletions}]}
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/compare/{base}...{head}", headers=auth)
    _raise(r)
    data = r.json()
    return {
        "total_commits": data.get("total_commits", 0),
        "commits": [
            {
                "sha": c["sha"],
                "message": c["commit"]["message"],
                "url": c.get("html_url", ""),
            }
            for c in _safe_list(data.get("commits"))
        ],
        "files": [
            {
                "filename": f["filename"],
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
            }
            for f in _safe_list(data.get("files"))
        ],
    }


# ── workflow / actions tools ─────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_workflow_runs(
    owner: str,
    repo: str,
    branch: str | None = None,
    status: str | None = None,
    limit: int = 20,
    page: int = 1,
    account: str | None = None,
) -> list[dict]:
    """
    List recent Actions workflow runs (Forgejo "tasks").

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        branch:  Optional branch filter.
        status:  Optional status filter (success/failure/running/queued).
        limit:   Max runs per page (default 20, max 50).
        page:    Page number (default 1).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of run objects: {id, name, status, conclusion, head_branch,
        head_sha, run_number, event, created, updated, url}.
    """
    client, auth = _get_client(account)
    params: dict = {"limit": min(limit, 50), "page": page}
    if branch:
        params["branch"] = branch
    if status:
        params["status"] = status
    # Use /actions/runs (not /actions/tasks) — the tasks endpoint hides
    # runs that are still in `waiting` status (Forgejo runs-vs-tasks split).
    r = await client.get(
        f"/repos/{owner}/{repo}/actions/runs", headers=auth, params=params
    )
    _raise(r)
    data = r.json()
    runs = data.get("workflow_runs", data) if isinstance(data, dict) else data
    return [
        {
            "id": run["id"],
            "name": run.get("name") or run.get("workflow_id", ""),
            "status": run.get("status", ""),
            "conclusion": run.get("conclusion", ""),
            "head_branch": run.get("head_branch", ""),
            "head_sha": run.get("head_sha") or run.get("commit_sha", ""),
            "run_number": run.get("run_number", 0),
            "event": run.get("event", ""),
            "created": run.get("created") or run.get("created_at", ""),
            "updated": run.get("updated") or run.get("updated_at", ""),
            "url": run.get("html_url", ""),
        }
        for run in runs
    ]


@mcp.tool()
@catch_api_errors
async def get_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    account: str | None = None,
) -> dict:
    """
    Get metadata for a workflow run by ID.

    Fetches the single-run endpoint `/repos/{o}/{r}/actions/runs/{id}`
    (verified working on Codeberg Forgejo 2026-04-19 against a live run;
    returns full run metadata including status, commit_sha, event_payload,
    trigger_user, html_url). Previous implementation walked the list
    endpoint because older docs claimed no detail endpoint existed —
    that claim was wrong. Per-job breakdown is still NOT available via
    API — use the `url` field to open the run in the browser.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        run_id:  Workflow run/task ID.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Run object with metadata + UI url. No per-job detail.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}", headers=auth)
    if r.status_code == 404:
        raise ValueError(
            f"run_id={run_id} not found on {owner}/{repo}. "
            f"Use list_workflow_runs first to find valid IDs."
        )
    _raise(r)
    run = r.json()
    return {
        "id": run["id"],
        "name": run.get("name") or run.get("workflow_id", ""),
        "status": run.get("status", ""),
        "conclusion": run.get("conclusion", ""),
        "head_branch": run.get("head_branch", ""),
        "head_sha": run.get("head_sha") or run.get("commit_sha", ""),
        "run_number": run.get("run_number", 0),
        "event": run.get("event", ""),
        "workflow_id": run.get("workflow_id", ""),
        "display_title": run.get("title") or run.get("display_title", ""),
        "created_at": run.get("created") or run.get("created_at", ""),
        "updated_at": run.get("updated") or run.get("updated_at", ""),
        "url": run.get("html_url") or run.get("url", ""),
        "jobs": [],
        "_note": (
            "Per-job detail is not exposed by the Codeberg "
            "Forgejo API. Open the `url` in a browser to view "
            "jobs and logs."
        ),
    }


@mcp.tool()
@catch_api_errors
async def get_workflow_logs(
    owner: str,
    repo: str,
    run_id: int,
    job_id: int | None = None,
    max_bytes: int = 50000,
    account: str | None = None,
) -> str:
    """
    Attempt to fetch raw logs for a workflow run.

    **Known limitation (2026-04-17):** Codeberg's Forgejo instance does NOT
    expose Actions run logs via REST API. None of `/actions/tasks/{id}/logs`,
    `/actions/runs/{id}/logs`, `/actions/jobs/{id}/logs` etc. are registered
    (all return 404 "resource does not exist"). The Web UI renders logs via
    an authenticated websocket/XHR that isn't part of the public API surface.

    This function still tries the documented endpoint (in case Forgejo ships
    it later) and, on 404, returns a clear degradation message with the UI
    URL of the run so the user can open it manually.

    Args:
        owner:     Repository owner.
        repo:      Repository name.
        run_id:    Workflow run/task ID (from list_workflow_runs).
        job_id:    Optional specific job ID for per-job logs.
        max_bytes: Truncation cap in bytes (default 50000).
        account:   Codeberg account to use (default: the configured default account).

    Returns:
        Raw log text if the API exposes it, OR a degradation message with
        the UI URL on 404.
    """
    client, auth = _get_client(account)
    path = f"/repos/{owner}/{repo}/actions/tasks/{run_id}/logs"
    if job_id is not None:
        path = f"/repos/{owner}/{repo}/actions/tasks/{run_id}/jobs/{job_id}/logs"
    r = await client.get(path, headers=auth)
    if r.status_code == 404:
        # Degrade: find the run's UI URL via list + filter so we can at
        # least hand the user a clickable link.
        try:
            meta = await get_workflow_run(
                owner=owner, repo=repo, run_id=run_id, account=account
            )
            url = meta.get("url", "")
        except Exception:
            url = (
                f"https://codeberg.org/{owner}/{repo}/actions "
                f"(filter by run_id={run_id})"
            )
        return (
            "Logs are not exposed by the Codeberg Forgejo API as of "
            "2026-04-17 — all /actions/*/logs endpoints return 404. "
            f"Open the run in a browser: {url}"
        )
    _raise(r)
    text = r.text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cutoff = encoded[:max_bytes].decode("utf-8", errors="ignore")
    omitted = len(encoded) - max_bytes
    return f"{cutoff}\n[truncated — {omitted} bytes omitted]"


# ── issue tools ──────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str | None = None,
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List issues (excluding PRs).

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        state:   open / closed / all (default: open).
        labels:  Comma-separated labels to filter by (optional).
        limit:   Per-page limit (default 20, max 50).
        page:    Page number (default 1).
        all:     Auto-paginate (cap 10 pages = 500 items).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {number, title, state, labels, created, updated,
        comments_count, url}.
    """
    client, auth = _get_client(account)

    async def fetch_page(p: int) -> list[dict]:
        params: dict = {
            "type": "issues",
            "state": state,
            "limit": min(limit, 50),
            "page": p,
        }
        if labels:
            params["labels"] = labels
        r = await client.get(
            f"/repos/{owner}/{repo}/issues", headers=auth, params=params
        )
        _raise(r)
        return [
            {
                "number": i["number"],
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "labels": [lbl.get("name", "") for lbl in _safe_list(i.get("labels"))],
                "created": i.get("created_at", ""),
                "updated": i.get("updated_at", ""),
                "comments_count": i.get("comments", 0),
                "url": i.get("html_url", ""),
            }
            for i in r.json()
        ]

    return await _paginate(fetch_page, min(limit, 50), page, all)


def _project_issue(data: dict) -> dict:
    """Shape a Forgejo issue record into the MCP's stable projection.

    Shared by get_issue and edit_issue so the two return identical shapes
    (same idiom as _project_release for the release tools).
    """
    return {
        "number": data["number"],
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "state": data.get("state", ""),
        "labels": [lbl.get("name", "") for lbl in _safe_list(data.get("labels"))],
        "assignees": [a.get("login", "") for a in _safe_list(data.get("assignees"))],
        "created": data.get("created_at", ""),
        "updated": data.get("updated_at", ""),
        "url": data.get("html_url", ""),
    }


@mcp.tool()
@catch_api_errors
async def get_issue(
    owner: str,
    repo: str,
    issue_number: int,
    account: str | None = None,
) -> dict:
    """
    Get a single issue with full body, labels, and assignees.

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        issue_number: Issue number.
        account:      Codeberg account to use (default: the configured default account).

    Returns:
        Full issue object.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/issues/{issue_number}", headers=auth)
    _raise(r)
    return _project_issue(r.json())


@mcp.tool()
@catch_api_errors
async def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """
    Create a new issue.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        title:   Issue title.
        body:    Issue body (markdown).
        labels:  List of label names to apply.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Created issue object.
    """
    client, auth = _get_client(account)
    oversized = len(body) > _ISSUE_BODY_SOFT_LIMIT
    workaround = (
        f"body is {len(body)} chars (soft limit {_ISSUE_BODY_SOFT_LIMIT}); "
        "Forgejo's POST /issues 500s on long bodies (~4k observed). "
        "Workaround: create with a short placeholder body, then PATCH the "
        "full body via update_file/the issues PATCH endpoint."
        if oversized
        else ""
    )
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    r = await client.post(f"/repos/{owner}/{repo}/issues", headers=auth, json=payload)
    if not r.is_success and oversized:
        raise CodebergAPIError(r.status_code, f"{_detail(r)} — {workaround}")
    _raise(r)
    data = r.json()
    result = {
        "number": data["number"],
        "title": data.get("title", ""),
        "state": data.get("state", ""),
        "labels": [lbl.get("name", "") for lbl in _safe_list(data.get("labels"))],
        "url": data.get("html_url", ""),
        "created": data.get("created_at", ""),
        "updated": data.get("updated_at", ""),
    }
    if oversized:
        result["size_warning"] = workaround
    return result


@mcp.tool()
@catch_api_errors
async def comment_on_issue(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    account: str | None = None,
) -> dict:
    """
    Add a comment to an issue or PR (Forgejo treats PRs as issues here).

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        issue_number: Issue or PR number.
        body:         Comment body (markdown).
        account:      Codeberg account to use (default: the configured default account).

    Returns:
        Comment object: {id, body, user, created, url}.
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        headers=auth,
        json={"body": body},
    )
    _raise(r)
    data = r.json()
    return {
        "id": data["id"],
        "body": data.get("body", ""),
        "user": data.get("user", {}).get("login", ""),
        "created": data.get("created_at", ""),
        "url": data.get("html_url", ""),
    }


# ── user account tools ──────────────────────────────────────────────────────


def _project_user_key(k: dict) -> dict:
    """Compact projection for a PublicKey object.

    Drops the full pubkey body (use `ssh-keygen -lf <file>` against the
    fingerprint to verify locally), the API self-link, the user object
    (always the authenticated user), and `key_type` — Forgejo uses that
    field as a user-vs-deploy-key category, not an algorithm enum, and
    `/user/keys` only ever returns "user" so the field is dead weight here.
    """
    return {
        "id": k["id"],
        "title": k.get("title", ""),
        "fingerprint": k.get("fingerprint", ""),
        "read_only": k.get("read_only", False),
        "created_at": k.get("created_at", ""),
    }


@mcp.tool()
@catch_api_errors
async def list_user_keys(account: str | None = None) -> list[dict]:
    """
    List the authenticated user's SSH keys.

    Diagnostic tool — answers "is my account set up right for git push?".
    Returns a compact projection (no full pubkey body); compare the
    `fingerprint` against `ssh-keygen -lf ~/.ssh/<key>.pub` locally.

    Args:
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {id, title, fingerprint, read_only, created_at}.
    """
    client, auth = _get_client(account)
    r = await client.get("/user/keys", headers=auth)
    _raise(r)
    return [_project_user_key(k) for k in r.json()]


@mcp.tool()
@catch_api_errors
async def list_user_emails(account: str | None = None) -> list[dict]:
    """
    List the authenticated user's email addresses.

    Diagnostic tool — verify which email is primary and which are verified.
    Codeberg uses the verified email for commit attribution; an unverified
    address won't show as "you" on the web UI.

    Args:
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {email, primary, verified}.
    """
    client, auth = _get_client(account)
    r = await client.get("/user/emails", headers=auth)
    _raise(r)
    return [
        {
            "email": e.get("email", ""),
            "primary": e.get("primary", False),
            "verified": e.get("verified", False),
        }
        for e in r.json()
    ]


# ── repo settings tools ─────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def set_repo_actions_enabled(
    owner: str,
    repo: str,
    enabled: bool,
    account: str | None = None,
) -> dict:
    """
    Enable or disable Forgejo Actions on a repository.

    Forgejo's `has_actions` field is OFF by default on freshly created repos,
    so workflow files in `.forgejo/workflows/` or `.github/workflows/` won't
    trigger until this is flipped to true.

    Args:
        owner:   Repository owner (username or org).
        repo:    Repository name.
        enabled: True to enable Actions, False to disable.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        {full_name, has_actions} reflecting the post-PATCH state.
    """
    client, auth = _get_client(account)
    r = await client.patch(
        f"/repos/{owner}/{repo}",
        headers=auth,
        json={"has_actions": enabled},
    )
    _raise(r)
    data = r.json()
    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "has_actions": data.get("has_actions", enabled),
    }


@mcp.tool()
@catch_api_errors
async def list_repo_secrets(
    owner: str,
    repo: str,
    account: str | None = None,
) -> list[dict]:
    """
    List the names of Forgejo Actions secrets configured on a repository.

    The Forgejo API exposes secret names and creation timestamps but never
    the values (write-only). To set or delete a secret, call the Forgejo API
    directly (PUT/DELETE `/repos/{owner}/{repo}/actions/secrets/{name}`) — the
    MCP intentionally omits write-side secret tools to keep credentials out of
    transcripts.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {name, created_at}.
    """
    client, auth = _get_client(account)
    r = await client.get(
        f"/repos/{owner}/{repo}/actions/secrets",
        headers=auth,
    )
    _raise(r)
    return [
        {
            "name": s.get("name", ""),
            "created_at": s.get("created_at", ""),
        }
        for s in r.json()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 expansion — 20 new tools across 7 feature areas
# ═══════════════════════════════════════════════════════════════════════════


# ── pull request primitives ─────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_pull_files(
    owner: str,
    repo: str,
    index: int,
    account: str | None = None,
) -> list[dict]:
    """
    List files changed in a pull request.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   PR number.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {filename, status, additions, deletions, changes, sha, raw_url}.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/pulls/{index}/files", headers=auth)
    _raise(r)
    return [
        {
            "filename": f.get("filename", ""),
            "status": f.get("status", ""),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "changes": f.get("changes", 0),
            "sha": f.get("sha", ""),
            "raw_url": f.get("raw_url", ""),
        }
        for f in r.json()
    ]


@mcp.tool()
@catch_api_errors
async def list_pull_commits(
    owner: str,
    repo: str,
    index: int,
    account: str | None = None,
) -> list[dict]:
    """
    List commits in a pull request.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   PR number.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {sha, message, author, date, url}.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/pulls/{index}/commits", headers=auth)
    _raise(r)
    out: list[dict] = []
    for c in r.json():
        commit_data = c.get("commit", {})
        author_data = commit_data.get("author", {})
        out.append(
            {
                "sha": c.get("sha", ""),
                "message": commit_data.get("message", ""),
                "author": author_data.get("name", ""),
                "date": author_data.get("date", ""),
                "url": c.get("url", ""),
            }
        )
    return out


@mcp.tool()
@catch_api_errors
async def list_pull_reviews(
    owner: str,
    repo: str,
    index: int,
    account: str | None = None,
) -> list[dict]:
    """
    List reviews on a pull request.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   PR number.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {id, state, body, user, submitted_at, commit_id}.
        States: APPROVED, REQUEST_CHANGES, COMMENT, PENDING.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/pulls/{index}/reviews", headers=auth)
    _raise(r)
    return [
        {
            "id": rv.get("id", 0),
            "state": rv.get("state", ""),
            "body": rv.get("body", ""),
            "user": rv.get("user", {}).get("login", ""),
            "submitted_at": rv.get("submitted_at", ""),
            "commit_id": rv.get("commit_id", ""),
        }
        for rv in r.json()
    ]


# ── workflow run control ────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def cancel_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    account: str | None = None,
) -> dict:
    """
    Cancel a running or queued workflow run.

    Idempotent on finished runs (Forgejo returns 200 or 412 depending on
    version — both pass through).

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        run_id:  Workflow run ID (numeric — get from `list_workflow_runs`).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Run object reflecting post-cancel state. May contain {id, status}
        or be empty depending on Forgejo version.
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel", headers=auth
    )
    _raise(r)
    try:
        return r.json()
    except Exception:
        return {"id": run_id, "status": "cancelled"}


@mcp.tool()
@catch_api_errors
async def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    account: str | None = None,
) -> dict:
    """
    Re-run a workflow run that has finished.

    Returns 412 if the run is still in progress — callers should
    `cancel_workflow_run` first if needed, then poll until status is
    'cancelled' or 'completed' before rerunning.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        run_id:  Workflow run ID.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Run object reflecting post-rerun state ({id, status}).
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun", headers=auth
    )
    _raise(r)
    try:
        return r.json()
    except Exception:
        return {"id": run_id, "status": "queued"}


# ── repo edit ───────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def edit_repo(
    owner: str,
    repo: str,
    description: str | None = None,
    website: str | None = None,
    default_branch: str | None = None,
    archived: bool | None = None,
    private: bool | None = None,
    has_issues: bool | None = None,
    has_wiki: bool | None = None,
    has_pull_requests: bool | None = None,
    account: str | None = None,
) -> dict:
    """
    Edit repository settings via PATCH.

    Sends only fields the caller explicitly set — None fields are skipped
    so Forgejo doesn't interpret them as "clear this field".

    Args:
        owner:             Repository owner.
        repo:              Repository name.
        description:       New description (optional).
        website:           New website URL (optional).
        default_branch:    Rename default branch (optional).
        archived:          Archive/unarchive (optional).
        private:           Toggle private/public (optional).
        has_issues:        Toggle Issues tab (optional).
        has_wiki:          Toggle Wiki tab (optional).
        has_pull_requests: Toggle PR tab (optional).
        account:           Codeberg account to use (default: the configured default account).

    Returns:
        Repo object reflecting post-PATCH state.
    """
    payload: dict = {}
    if description is not None:
        payload["description"] = description
    if website is not None:
        payload["website"] = website
    if default_branch is not None:
        payload["default_branch"] = default_branch
    if archived is not None:
        payload["archived"] = archived
    if private is not None:
        payload["private"] = private
    if has_issues is not None:
        payload["has_issues"] = has_issues
    if has_wiki is not None:
        payload["has_wiki"] = has_wiki
    if has_pull_requests is not None:
        payload["has_pull_requests"] = has_pull_requests

    client, auth = _get_client(account)
    r = await client.patch(f"/repos/{owner}/{repo}", headers=auth, json=payload)
    _raise(r)
    data = r.json()
    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "description": data.get("description", ""),
        "default_branch": data.get("default_branch", "main"),
        "archived": data.get("archived", False),
        "private": data.get("private", False),
        "has_issues": data.get("has_issues", True),
        "has_wiki": data.get("has_wiki", True),
        "has_pull_requests": data.get("has_pull_requests", True),
    }


# ── issue labels ────────────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_issue_labels(
    owner: str,
    repo: str,
    index: int,
    account: str | None = None,
) -> list[dict]:
    """
    List labels currently applied to an issue.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        index:   Issue number.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {id, name, color, description}.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/issues/{index}/labels", headers=auth)
    _raise(r)
    return [
        {
            "id": lbl.get("id", 0),
            "name": lbl.get("name", ""),
            "color": lbl.get("color", ""),
            "description": lbl.get("description", ""),
        }
        for lbl in r.json()
    ]


@mcp.tool()
@catch_api_errors
async def add_issue_labels(
    owner: str,
    repo: str,
    index: int,
    label_ids: list[int],
    account: str | None = None,
) -> list[dict]:
    """
    Add labels to an issue by label ID.

    Note: a token without write:issue scope returns 403 for this call. Use an
    account whose token has issue write access for label mutations.

    Args:
        owner:     Repository owner.
        repo:      Repository name.
        index:     Issue number.
        label_ids: List of label IDs (lookup via `list_issue_labels` on a
                   labeled issue, or via the repo's /labels endpoint).
        account:   Codeberg account to use (default: the configured default account).

    Returns:
        Full list of labels now applied to the issue (post-state).
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/issues/{index}/labels",
        headers=auth,
        json={"labels": label_ids},
    )
    _raise(r)
    return [
        {
            "id": lbl.get("id", 0),
            "name": lbl.get("name", ""),
            "color": lbl.get("color", ""),
            "description": lbl.get("description", ""),
        }
        for lbl in r.json()
    ]


@mcp.tool()
@catch_api_errors
async def remove_issue_label(
    owner: str,
    repo: str,
    index: int,
    label_id: int,
    account: str | None = None,
) -> None:
    """
    Remove a single label from an issue.

    Note: this removes the LABEL ASSIGNMENT, not the label definition.
    Removing a definition (label-level delete) is out of scope per the
    no-delete rule.

    Args:
        owner:    Repository owner.
        repo:     Repository name.
        index:    Issue number.
        label_id: ID of the label to remove.
        account:  Codeberg account to use (default: the configured default account).

    Returns:
        None on success (HTTP 204).
    """
    client, auth = _get_client(account)
    r = await client.delete(
        f"/repos/{owner}/{repo}/issues/{index}/labels/{label_id}", headers=auth
    )
    _raise(r)
    return None


# ── releases ────────────────────────────────────────────────────────────────


def _project_release(rel: dict) -> dict:
    """Shape a Forgejo release record into the MCP's stable projection."""
    return {
        "id": rel.get("id", 0),
        "tag_name": rel.get("tag_name", ""),
        "name": rel.get("name", ""),
        "body": rel.get("body", ""),
        "draft": rel.get("draft", False),
        "prerelease": rel.get("prerelease", False),
        "created_at": rel.get("created_at", ""),
        "published_at": rel.get("published_at", ""),
        "html_url": rel.get("html_url", ""),
        "target_commitish": rel.get("target_commitish", ""),
    }


@mcp.tool()
@catch_api_errors
async def list_releases(
    owner: str,
    repo: str,
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List releases for a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        limit:   Per-page limit (default 20, max 50).
        page:    Page number (default 1).
        all:     Auto-paginate (cap 10 pages = 500 items).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of release objects with {id, tag_name, name, body, draft,
        prerelease, created_at, published_at, html_url, target_commitish}.
    """
    client, auth = _get_client(account)

    async def fetch_page(p: int) -> list[dict]:
        r = await client.get(
            f"/repos/{owner}/{repo}/releases",
            headers=auth,
            params={"limit": min(limit, 50), "page": p},
        )
        _raise(r)
        return [_project_release(rel) for rel in r.json()]

    return await _paginate(fetch_page, min(limit, 50), page, all)


@mcp.tool()
@catch_api_errors
async def get_release(
    owner: str,
    repo: str,
    release_id: int,
    account: str | None = None,
) -> dict:
    """
    Get a single release by numeric ID.

    Args:
        owner:      Repository owner.
        repo:       Repository name.
        release_id: Numeric release ID (from `list_releases`).
        account:    Codeberg account to use (default: the configured default account).

    Returns:
        Release object.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/releases/{release_id}", headers=auth)
    _raise(r)
    return _project_release(r.json())


@mcp.tool()
@catch_api_errors
async def get_release_by_tag(
    owner: str,
    repo: str,
    tag: str,
    account: str | None = None,
) -> dict:
    """
    Get a release by its tag name.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        tag:     Git tag (e.g. 'v2.0.1').
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Release object (same shape as `get_release`).
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/releases/tags/{tag}", headers=auth)
    _raise(r)
    return _project_release(r.json())


@mcp.tool()
@catch_api_errors
async def create_release(
    owner: str,
    repo: str,
    tag_name: str,
    name: str = "",
    body: str = "",
    draft: bool = False,
    prerelease: bool = False,
    target_commitish: str = "",
    account: str | None = None,
) -> dict:
    """
    Create a new release.

    Returns 409 if a release already exists for the tag. Callers should
    then choose between `edit_release` (update the existing one) or abort.

    Args:
        owner:            Repository owner.
        repo:             Repository name.
        tag_name:         Git tag to anchor the release at (must exist if
                          target_commitish is empty, else will be created).
        name:             Release title (defaults to tag_name on Forgejo).
        body:             Release notes (markdown).
        draft:            Mark as draft (not published).
        prerelease:       Mark as prerelease.
        target_commitish: Branch or commit SHA to create the tag at when
                          tag_name doesn't yet exist (empty = require tag).
        account:          Codeberg account to use (default: the configured default account).

    Returns:
        Created release object.
    """
    payload: dict = {
        "tag_name": tag_name,
        "name": name,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
    }
    if target_commitish:
        payload["target_commitish"] = target_commitish

    client, auth = _get_client(account)
    r = await client.post(f"/repos/{owner}/{repo}/releases", headers=auth, json=payload)
    _raise(r)
    return _project_release(r.json())


@mcp.tool()
@catch_api_errors
async def edit_release(
    owner: str,
    repo: str,
    release_id: int,
    name: str | None = None,
    body: str | None = None,
    draft: bool | None = None,
    prerelease: bool | None = None,
    tag_name: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Edit an existing release. Sends only the fields the caller set.

    Args:
        owner:      Repository owner.
        repo:       Repository name.
        release_id: Numeric release ID.
        name:       New release title (optional).
        body:       New release notes (optional).
        draft:      Toggle draft state (optional).
        prerelease: Toggle prerelease state (optional).
        tag_name:   Rename the tag (optional).
        account:    Codeberg account to use (default: the configured default account).

    Returns:
        Release object reflecting post-PATCH state.
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if body is not None:
        payload["body"] = body
    if draft is not None:
        payload["draft"] = draft
    if prerelease is not None:
        payload["prerelease"] = prerelease
    if tag_name is not None:
        payload["tag_name"] = tag_name

    client, auth = _get_client(account)
    r = await client.patch(
        f"/repos/{owner}/{repo}/releases/{release_id}",
        headers=auth,
        json=payload,
    )
    _raise(r)
    return _project_release(r.json())


@mcp.tool()
@catch_api_errors
async def edit_issue(
    owner: str,
    repo: str,
    issue_number: int,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Edit an existing issue's title, body, or state. Sends only the
    fields the caller set (skip-None idiom, same as edit_repo/edit_release)
    so unchanged fields are not clobbered.

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        issue_number: Issue number.
        title:        New title (optional).
        body:         New body markdown (optional).
        state:        "open" or "closed" (optional). A reversible state
                      transition — not a delete.
        account:      Codeberg account to use (default: the configured default account).
                      Note: a token without issue scope returns 403.

        Note: a very long ``body`` may hit the same Forgejo size fragility
        documented on create_issue (POST /issues 500s on bodies ~4k+ chars;
        see _ISSUE_BODY_SOFT_LIMIT). The PATCH path is unverified but plausibly
        shares it — if a large-body edit 500s, split the body or trim it.

    Returns:
        Updated issue object (same shape as get_issue). Labels are NOT
        editable here — use add_issue_labels / remove_issue_label.
    """
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state

    client, auth = _get_client(account)
    r = await client.patch(
        f"/repos/{owner}/{repo}/issues/{issue_number}",
        headers=auth,
        json=payload,
    )
    _raise(r)
    return _project_issue(r.json())


# ── tags ────────────────────────────────────────────────────────────────────


def _project_tag(t: dict) -> dict:
    """Shape a Forgejo tag record into the MCP's stable projection."""
    commit = t.get("commit", {})
    tagger = t.get("tagger", {})
    return {
        "name": t.get("name", ""),
        "sha": commit.get("sha", "") or t.get("id", ""),
        "url": commit.get("url", ""),
        "message": t.get("message", ""),
        "tagger_name": tagger.get("name", ""),
        "tagger_date": tagger.get("date", ""),
    }


@mcp.tool()
@catch_api_errors
async def list_tags(
    owner: str,
    repo: str,
    limit: int = 20,
    page: int = 1,
    all: bool = False,
    account: str | None = None,
) -> list[dict]:
    """
    List git tags for a repository.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        limit:   Per-page limit (default 20, max 50).
        page:    Page number (default 1).
        all:     Auto-paginate (cap 10 pages = 500 items).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {name, sha, url, message, tagger_name, tagger_date}.
    """
    client, auth = _get_client(account)

    async def fetch_page(p: int) -> list[dict]:
        r = await client.get(
            f"/repos/{owner}/{repo}/tags",
            headers=auth,
            params={"limit": min(limit, 50), "page": p},
        )
        _raise(r)
        return [_project_tag(t) for t in r.json()]

    return await _paginate(fetch_page, min(limit, 50), page, all)


@mcp.tool()
@catch_api_errors
async def get_tag(
    owner: str,
    repo: str,
    tag: str,
    account: str | None = None,
) -> dict:
    """
    Get a single git tag by name.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        tag:     Tag name (e.g. 'v2.0.1').
        account: Codeberg account to use (default: the configured default account).

    Returns:
        Tag object: {name, sha, url, message, tagger_name, tagger_date}.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/tags/{tag}", headers=auth)
    _raise(r)
    return _project_tag(r.json())


# ── actions variables ───────────────────────────────────────────────────────


@mcp.tool()
@catch_api_errors
async def list_actions_variables(
    owner: str,
    repo: str,
    account: str | None = None,
) -> list[dict]:
    """
    List Forgejo Actions variables (non-sensitive config) on a repo.

    Unlike secrets, variable VALUES are readable. Use for image tags,
    feature flags, environment selectors.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        account: Codeberg account to use (default: the configured default account).

    Returns:
        List of {name, value}.
    """
    client, auth = _get_client(account)
    r = await client.get(f"/repos/{owner}/{repo}/actions/variables", headers=auth)
    _raise(r)
    return [{"name": v.get("name", ""), "value": v.get("data", "")} for v in r.json()]


@mcp.tool()
@catch_api_errors
async def get_actions_variable(
    owner: str,
    repo: str,
    name: str,
    account: str | None = None,
) -> dict:
    """
    Get a single Forgejo Actions variable by name.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        name:    Variable name (case-sensitive, matches `${{ vars.X }}`).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        {name, value}.
    """
    client, auth = _get_client(account)
    r = await client.get(
        f"/repos/{owner}/{repo}/actions/variables/{name}", headers=auth
    )
    _raise(r)
    v = r.json()
    return {"name": v.get("name", ""), "value": v.get("data", "")}


@mcp.tool()
@catch_api_errors
async def create_actions_variable(
    owner: str,
    repo: str,
    name: str,
    value: str,
    account: str | None = None,
) -> None:
    """
    Create a new Forgejo Actions variable.

    Returns 409 if a variable with the same name already exists — callers
    should then choose between `update_actions_variable` or abort.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        name:    Variable name (uppercase + underscore by convention).
        value:   Variable value (string).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        None on success (HTTP 204).
    """
    client, auth = _get_client(account)
    r = await client.post(
        f"/repos/{owner}/{repo}/actions/variables/{name}",
        headers=auth,
        json={"value": value},
    )
    _raise(r)
    return None


@mcp.tool()
@catch_api_errors
async def update_actions_variable(
    owner: str,
    repo: str,
    name: str,
    value: str,
    account: str | None = None,
) -> None:
    """
    Update an existing Forgejo Actions variable's value.

    Returns 404 if the variable doesn't exist — call `create_actions_variable`
    first or use the bare-create-then-fallback pattern.

    Args:
        owner:   Repository owner.
        repo:    Repository name.
        name:    Variable name.
        value:   New value (string).
        account: Codeberg account to use (default: the configured default account).

    Returns:
        None on success (HTTP 204).
    """
    client, auth = _get_client(account)
    r = await client.put(
        f"/repos/{owner}/{repo}/actions/variables/{name}",
        headers=auth,
        json={"value": value},
    )
    _raise(r)
    return None


# ── entrypoint ───────────────────────────────────────────────────────────────
#
# Transport is selected via MCP_TRANSPORT env var:
#   http   (default) → streamable-HTTP on 0.0.0.0:$PORT  ← for claude.ai
#   sse              → SSE on 0.0.0.0:$PORT
#   stdio            → stdio                              ← for Claude Code

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "http").lower()
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        mcp.run(transport="streamable-http", host=host, port=port)
