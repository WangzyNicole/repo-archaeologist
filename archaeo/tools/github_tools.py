"""GitHub evidence: the pull request and issue discussion behind a commit.

Git tells you what changed. GitHub tells you why people argued about it, what
they tried first, and what broke when they shipped it. That is where the real
answer usually lives, so these tools pull PR bodies, review threads (including
the inline comments attached to specific lines) and linked issues.

Responses are cached on disk. Historical discussion does not change, so a cache
hit is both a rate-limit saver and what makes a repeat demo instant.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from ..config import CONFIG
from ..evidence import EvidenceLedger
from ..repo import Repo

API = "https://api.github.com"
CACHE_TTL_SECONDS = 7 * 24 * 3600
LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|ref|see|refs)\b[:\s]*#(\d+)", re.IGNORECASE
)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, repo: Repo, token: str | None = None) -> None:
        if not repo.has_github:
            raise GitHubError("This repo has no GitHub remote, so GitHub tools are unavailable.")
        self.repo = repo
        self.token = token or CONFIG.github_token
        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-archaeologist",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)
        CONFIG.ensure_dirs()
        self.cache_dir = CONFIG.http_cache

    # -- plumbing ---------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def get(self, path: str, params: dict[str, Any] | None = None, use_cache: bool = True) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else "")
        cache_file = self._cache_path(full)

        if use_cache and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text())
                if time.time() - cached.get("_fetched_at", 0) < CACHE_TTL_SECONDS:
                    return cached["data"]
            except (json.JSONDecodeError, KeyError, OSError):
                pass

        response = self.session.get(url, params=params, timeout=30)
        if response.status_code == 404:
            raise GitHubError(f"Not found: {full}")
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = response.headers.get("X-RateLimit-Reset", "")
            hint = " Set GITHUB_TOKEN to raise the limit from 60/hr to 5000/hr." if not self.token else ""
            raise GitHubError(f"GitHub rate limit exhausted (resets at epoch {reset}).{hint}")
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned {response.status_code} for {full}: {response.text[:300]}")

        data = response.json()
        try:
            cache_file.write_text(json.dumps({"_fetched_at": time.time(), "data": data}))
        except OSError:
            pass
        return data

    @property
    def base(self) -> str:
        return f"/repos/{self.repo.owner}/{self.repo.name}"


def _trim(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} chars omitted]"


def _format_comments(comments: list[dict], limit: int, body_chars: int, inline: bool = False) -> list[str]:
    lines: list[str] = []
    for comment in comments[:limit]:
        who = (comment.get("user") or {}).get("login", "unknown")
        when = (comment.get("created_at") or "")[:10]
        header = f"  @{who} ({when})"
        if inline and comment.get("path"):
            header += f" on {comment['path']}"
            if comment.get("line"):
                header += f":{comment['line']}"
        lines.append(header)
        if inline and comment.get("diff_hunk"):
            hunk = "\n".join(comment["diff_hunk"].splitlines()[-6:])
            lines.append("\n".join(f"    > {row}" for row in hunk.splitlines()))
        body = _trim(comment.get("body"), body_chars)
        lines.append("\n".join(f"    {row}" for row in body.splitlines()))
        lines.append("")
    if len(comments) > limit:
        lines.append(f"  ...[{len(comments) - limit} more comments not shown]")
    return lines


# -- tools -----------------------------------------------------------------


def github_pr_for_commit(repo: Repo, ledger: EvidenceLedger, *, sha: str, client: GitHubClient | None = None) -> str:
    """Find the pull request that brought a commit into the default branch."""
    client = client or GitHubClient(repo)
    try:
        pulls = client.get(f"{client.base}/commits/{sha}/pulls")
    except GitHubError as exc:
        return f"ERROR: {exc}"
    if not pulls:
        return (
            f"No pull request is associated with {sha[:10]}. It was probably pushed directly; "
            "the commit message and any linked issue are your best evidence."
        )
    lines = [f"{len(pulls)} pull request(s) contain {sha[:10]}:"]
    for pr in pulls:
        url = pr.get("html_url", "")
        ledger.record(url, pr.get("title", ""))
        state = "merged" if pr.get("merged_at") else pr.get("state", "")
        lines.append(f"- #{pr.get('number')} [{state}] {pr.get('title')}")
        lines.append(f"  url: {url}")
        lines.append(f"  fetch the discussion with github_pull_request(number={pr.get('number')})")
    return "\n".join(lines)


def github_pull_request(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    number: int,
    include_review_comments: bool = True,
    client: GitHubClient | None = None,
) -> str:
    """The full record of a PR: description, review threads, linked issues."""
    client = client or GitHubClient(repo)
    try:
        pr = client.get(f"{client.base}/pulls/{number}")
    except GitHubError as exc:
        return f"ERROR: {exc}"

    url = pr.get("html_url", "")
    ledger.record(url, pr.get("title", ""))
    author = (pr.get("user") or {}).get("login", "unknown")
    state = "merged" if pr.get("merged_at") else pr.get("state", "")
    labels = ", ".join(label.get("name", "") for label in pr.get("labels") or [])

    lines = [
        f"PR #{number}: {pr.get('title')}",
        f"state: {state}   author: @{author}   opened: {(pr.get('created_at') or '')[:10]}"
        + (f"   merged: {(pr.get('merged_at') or '')[:10]}" if pr.get("merged_at") else ""),
        f"changed: +{pr.get('additions', '?')}/-{pr.get('deletions', '?')} across {pr.get('changed_files', '?')} files",
    ]
    if labels:
        lines.append(f"labels: {labels}")
    lines.append(f"url: {url}")
    lines.append("\ndescription:")
    body = _trim(pr.get("body"), CONFIG.max_body_chars)
    lines.append("\n".join(f"  {row}" for row in (body or "(empty)").splitlines()))

    linked = sorted({int(n) for n in LINKED_ISSUE_RE.findall(pr.get("body") or "")})
    if linked:
        lines.append(f"\nissues referenced in the description: {', '.join('#' + str(n) for n in linked)}")
        lines.append("  fetch these with github_issue(number=...) — they usually hold the original motivation")

    try:
        discussion = client.get(f"{client.base}/issues/{number}/comments", {"per_page": 100})
        if discussion:
            lines.append("\nconversation:")
            lines += _format_comments(discussion, CONFIG.max_comments, CONFIG.max_body_chars // 2)
    except GitHubError as exc:
        lines.append(f"\n(could not load conversation: {exc})")

    if include_review_comments:
        try:
            reviews = client.get(f"{client.base}/pulls/{number}/comments", {"per_page": 100})
            if reviews:
                lines.append("\ninline review comments (attached to specific lines):")
                lines += _format_comments(reviews, CONFIG.max_comments, CONFIG.max_body_chars // 3, inline=True)
        except GitHubError as exc:
            lines.append(f"\n(could not load review comments: {exc})")

    return "\n".join(lines)


def github_issue(repo: Repo, ledger: EvidenceLedger, *, number: int, client: GitHubClient | None = None) -> str:
    """An issue and its discussion — often the origin story for a workaround."""
    client = client or GitHubClient(repo)
    try:
        issue = client.get(f"{client.base}/issues/{number}")
    except GitHubError as exc:
        return f"ERROR: {exc}"

    url = issue.get("html_url", "")
    ledger.record(url, issue.get("title", ""))
    kind = "PR" if issue.get("pull_request") else "issue"
    lines = [
        f"{kind} #{number}: {issue.get('title')}",
        f"state: {issue.get('state')}   author: @{(issue.get('user') or {}).get('login', 'unknown')}"
        f"   opened: {(issue.get('created_at') or '')[:10]}"
        + (f"   closed: {(issue.get('closed_at') or '')[:10]}" if issue.get("closed_at") else ""),
    ]
    labels = ", ".join(label.get("name", "") for label in issue.get("labels") or [])
    if labels:
        lines.append(f"labels: {labels}")
    lines.append(f"url: {url}")
    lines.append("\nbody:")
    body = _trim(issue.get("body"), CONFIG.max_body_chars)
    lines.append("\n".join(f"  {row}" for row in (body or "(empty)").splitlines()))

    if issue.get("comments"):
        try:
            comments = client.get(f"{client.base}/issues/{number}/comments", {"per_page": 100})
            lines.append("\ncomments:")
            lines += _format_comments(comments, CONFIG.max_comments, CONFIG.max_body_chars // 2)
        except GitHubError as exc:
            lines.append(f"\n(could not load comments: {exc})")

    return "\n".join(lines)


def github_search(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    query: str,
    kind: str = "both",
    limit: int = 15,
    client: GitHubClient | None = None,
) -> str:
    """Search this repo's issues and PRs by keyword.

    The way in when you have a symptom ("why is this retried three times?") but
    no commit to start from.
    """
    client = client or GitHubClient(repo)
    scope = {"issue": " is:issue", "pr": " is:pr"}.get(kind, "")
    full_query = f"repo:{repo.owner}/{repo.name}{scope} {query}"
    try:
        data = client.get("/search/issues", {"q": full_query, "per_page": max(1, min(limit, 30))})
    except GitHubError as exc:
        return f"ERROR: {exc}"

    items = data.get("items") or []
    if not items:
        return f"No issues or PRs matched {query!r}."
    lines = [f"{data.get('total_count', len(items))} result(s) for {query!r} (showing {len(items)}):"]
    for item in items:
        url = item.get("html_url", "")
        ledger.record(url, item.get("title", ""))
        kind_label = "PR" if item.get("pull_request") else "issue"
        lines.append(
            f"- {kind_label} #{item.get('number')} [{item.get('state')}] {item.get('title')}"
            f"  ({(item.get('created_at') or '')[:10]}, {item.get('comments', 0)} comments)"
        )
        snippet = _trim(item.get("body"), 240).replace("\n", " ")
        if snippet:
            lines.append(f"    {snippet}")
        lines.append(f"    url: {url}")
    return "\n".join(lines)
