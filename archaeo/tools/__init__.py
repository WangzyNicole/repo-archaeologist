"""The toolbox: schemas the model sees, and the dispatcher that runs them."""

from __future__ import annotations

import time
from typing import Any, Callable

from ..config import CONFIG
from ..evidence import EvidenceLedger
from ..repo import Repo
from . import code_tools, git_tools, github_tools

SUBMIT_TOOL = "submit_findings"


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


GIT_TOOL_SPECS: list[dict] = [
    _tool(
        "repo_overview",
        "Orient yourself: branch, commit count, how far back history goes, latest tags. Cheap; call it first if you know nothing about the repo.",
        {},
        [],
    ),
    _tool(
        "list_dir",
        "List a directory in the repo so you can find the file you need.",
        {
            "path": {"type": "string", "description": "Directory relative to repo root. Default '.'"},
            "depth": {"type": "integer", "description": "How many levels to descend, 1-3. Default 1."},
        },
        [],
    ),
    _tool(
        "read_file",
        "Read a slice of a file, either from the working tree or from any historical revision. Use rev to see what the code looked like before a commit.",
        {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "start_line": {"type": "integer", "description": "First line (1-based). Default 1."},
            "end_line": {"type": "integer", "description": "Last line. Defaults to start+199."},
            "rev": {"type": "string", "description": "Optional commit-ish, e.g. 'abc1234' or 'abc1234^' to read the parent."},
        },
        ["path"],
    ),
    _tool(
        "grep_repo",
        "Search the code for a pattern. Use this to locate the symbol or string the question is about.",
        {
            "pattern": {"type": "string", "description": "Regex or literal to search for."},
            "glob": {"type": "string", "description": "Optional file filter, e.g. '*.py' or 'src/**/*.ts'."},
            "max_results": {"type": "integer", "description": "Default 40."},
            "case_insensitive": {"type": "boolean"},
        },
        ["pattern"],
    ),
    _tool(
        "git_blame",
        "Attribute lines to the commits that last changed them. Whitespace-only changes are ignored by default so blame skips pure reformatting. If blame lands on a mass-rename, lint, or 'apply black' commit, re-blame with rev='<that sha>^' to step past it, or set detect_moves=true to follow code that was moved between files.",
        {
            "path": {"type": "string", "description": "File to blame."},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "rev": {"type": "string", "description": "Revision to blame at. Default HEAD. Use '<sha>^' to look before a commit."},
            "ignore_whitespace": {"type": "boolean", "description": "Default true."},
            "detect_moves": {"type": "boolean", "description": "Follow code moved or copied from other files. Slower. Default false."},
        },
        ["path"],
    ),
    _tool(
        "git_log_for_path",
        "Commit history for a file or directory, following renames. Good for seeing the arc of how something evolved.",
        {
            "path": {"type": "string"},
            "limit": {"type": "integer", "description": "Default 20, max 100."},
            "follow": {"type": "boolean", "description": "Follow renames. Default true. Set false for directories."},
        },
        ["path"],
    ),
    _tool(
        "git_show",
        "Read one commit in full: message body, files touched, and optionally the diff. The message body is where authors explain themselves, so read it before guessing.",
        {
            "sha": {"type": "string"},
            "include_patch": {"type": "boolean", "description": "Include the diff. Default false."},
            "path_filter": {"type": "string", "description": "Limit the stat/diff to one path. Use this on large commits."},
        },
        ["sha"],
    ),
    _tool(
        "git_search_commits",
        "Find commits by the code they touched or by their message. mode='content' runs a pickaxe search (-S), which finds exactly when a string was introduced or removed — the fastest route to the commit that added a workaround. mode='message' greps commit messages.",
        {
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["content", "message"], "description": "Default 'content'."},
            "path": {"type": "string", "description": "Optional path to restrict the search."},
            "limit": {"type": "integer", "description": "Default 15."},
            "regex": {"type": "boolean", "description": "Treat a content query as a regex."},
        },
        ["query"],
    ),
    _tool(
        "git_releases_containing",
        "Which tags/releases contain a commit — tells you when a change actually reached users.",
        {"sha": {"type": "string"}},
        ["sha"],
    ),
]

GITHUB_TOOL_SPECS: list[dict] = [
    _tool(
        "github_pr_for_commit",
        "Find the pull request a commit arrived in. This is the bridge from 'what changed' to 'why', because the reasoning lives in the PR, not the commit.",
        {"sha": {"type": "string"}},
        ["sha"],
    ),
    _tool(
        "github_pull_request",
        "Read a PR in full: description, conversation, and the inline review comments attached to specific lines. Reviewer objections and the author's replies are usually the single richest source of intent.",
        {
            "number": {"type": "integer"},
            "include_review_comments": {"type": "boolean", "description": "Default true."},
        },
        ["number"],
    ),
    _tool(
        "github_issue",
        "Read an issue and its comments. Issues carry the original problem report that motivated a change.",
        {"number": {"type": "integer"}},
        ["number"],
    ),
    _tool(
        "github_search",
        "Search this repo's issues and PRs by keyword. Use when you have a symptom but no commit to start from.",
        {
            "query": {"type": "string", "description": "Keywords. GitHub search qualifiers are allowed."},
            "kind": {"type": "string", "enum": ["issue", "pr", "both"], "description": "Default 'both'."},
            "limit": {"type": "integer", "description": "Default 15."},
        },
        ["query"],
    ),
]

SUBMIT_SPEC = _tool(
    SUBMIT_TOOL,
    "Submit your final answer. Call this exactly once, when you can explain the WHY and support it with evidence you actually retrieved. If the evidence is thin, submit anyway with low confidence and say what is missing — a documented dead end beats a confident guess.",
    {
        "answer": {
            "type": "string",
            "description": (
                "Markdown narrative answering the question. Lead with the answer in two or three sentences, "
                "then the supporting story. Cite evidence inline as markdown links using URLs exactly as they "
                "appeared in tool results, e.g. [#412](https://github.com/o/r/pull/412) or "
                "[a1b2c3d](https://github.com/o/r/commit/a1b2c3d). Never invent a URL. Distinguish what the "
                "record states from what you are inferring: say 'the PR states' vs 'this suggests'."
            ),
        },
        "timeline": {
            "type": "array",
            "description": "The decision points that shaped this code, oldest first. Include only events that genuinely moved the story.",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD."},
                    "kind": {"type": "string", "enum": ["commit", "pr", "issue", "release", "other"]},
                    "ref": {"type": "string", "description": "Short sha, or #number."},
                    "title": {"type": "string", "description": "What happened, in a short phrase."},
                    "url": {"type": "string", "description": "Canonical URL, copied from a tool result."},
                    "actor": {"type": "string", "description": "Who did it, if known."},
                    "why_it_matters": {"type": "string", "description": "One sentence on how this event bears on the question."},
                },
                "required": ["date", "kind", "ref", "title", "why_it_matters"],
            },
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "high = the reasoning is stated explicitly in the record. medium = strongly implied. low = mostly inference.",
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What you could not determine, and where a human should look next.",
        },
    },
    ["answer", "timeline", "confidence"],
)


class Toolbox:
    """Binds the tool functions to one repo + ledger and dispatches by name."""

    def __init__(self, repo: Repo, ledger: EvidenceLedger, *, allow_github: bool = True) -> None:
        self.repo = repo
        self.ledger = ledger
        self.github_enabled = allow_github and repo.has_github
        self._client: github_tools.GitHubClient | None = None

        self._registry: dict[str, Callable[..., str]] = {
            "repo_overview": git_tools.git_repo_overview,
            "list_dir": code_tools.list_dir,
            "read_file": code_tools.read_file,
            "grep_repo": code_tools.grep_repo,
            "git_blame": git_tools.git_blame,
            "git_log_for_path": git_tools.git_log_for_path,
            "git_show": git_tools.git_show,
            "git_search_commits": git_tools.git_search_commits,
            "git_releases_containing": git_tools.git_releases_containing,
            "github_pr_for_commit": github_tools.github_pr_for_commit,
            "github_pull_request": github_tools.github_pull_request,
            "github_issue": github_tools.github_issue,
            "github_search": github_tools.github_search,
        }

    @property
    def specs(self) -> list[dict]:
        specs = list(GIT_TOOL_SPECS)
        if self.github_enabled:
            specs += GITHUB_TOOL_SPECS
        specs.append(SUBMIT_SPEC)
        return specs

    def _github_client(self) -> github_tools.GitHubClient:
        if self._client is None:
            self._client = github_tools.GitHubClient(self.repo)
        return self._client

    def run(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool, int]:
        """Returns (result_text, is_error, duration_ms)."""
        started = time.monotonic()
        function = self._registry.get(name)
        if function is None:
            return f"ERROR: unknown tool {name!r}.", True, 0

        kwargs = {k: v for k, v in (arguments or {}).items() if v is not None}
        try:
            if name.startswith("github_"):
                if not self.github_enabled:
                    return "ERROR: GitHub tools are unavailable for this repo (no GitHub remote).", True, 0
                kwargs["client"] = self._github_client()
            result = function(self.repo, self.ledger, **kwargs)
        except TypeError as exc:
            result, error = f"ERROR: bad arguments for {name}: {exc}", True
            return result, error, int((time.monotonic() - started) * 1000)
        except github_tools.GitHubError as exc:
            return f"ERROR: {exc}", True, int((time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - tool errors must reach the model, not crash the run
            return f"ERROR: {type(exc).__name__}: {exc}", True, int((time.monotonic() - started) * 1000)

        if len(result) > CONFIG.max_tool_result_chars:
            result = result[: CONFIG.max_tool_result_chars] + "\n...[tool output truncated; narrow your query]"
        # Anything the tool surfaced is now fair game to cite.
        self.ledger.record_many([url for url in _urls_in(result)])
        return result, result.startswith("ERROR:"), int((time.monotonic() - started) * 1000)


def _urls_in(text: str) -> list[str]:
    from ..evidence import extract_urls

    return extract_urls(text)
