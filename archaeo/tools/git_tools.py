"""Git-based evidence gathering: blame, history, pickaxe search, releases."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from ..evidence import EvidenceLedger
from ..repo import Repo

REC = "\x1e"  # record separator
FLD = "\x1f"  # field separator
LOG_FORMAT = FLD.join(["%H", "%an", "%ad", "%s", "%b"]) + REC


def git(repo: Repo, args: list[str], timeout: int = 120) -> str:
    """Run git, returning stdout. Non-zero exit becomes readable tool output."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo.path),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "ERROR: git command timed out."
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        return f"ERROR: {message[:1500] or 'git exited non-zero'}"
    return result.stdout


def _epoch_to_date(value: str) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return ""


def _parse_log(output: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for record in output.split(REC):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(FLD)
        if len(parts) < 4:
            continue
        sha, author, date, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        commits.append(
            {
                "sha": sha.strip(),
                "author": author.strip(),
                "date": date.strip(),
                "subject": subject.strip(),
                "body": body.strip(),
            }
        )
    return commits


def _render_commits(repo: Repo, commits: list[dict[str, str]], ledger: EvidenceLedger, body_chars: int) -> str:
    if not commits:
        return "No commits matched."
    lines: list[str] = []
    for commit in commits:
        short = commit["sha"][:10]
        url = repo.commit_url(commit["sha"])
        ledger.record(url, commit["subject"])
        lines.append(f"- {short}  {commit['date']}  {commit['author']}")
        lines.append(f"  subject: {commit['subject']}")
        if commit["body"]:
            body = commit["body"]
            if len(body) > body_chars:
                body = body[:body_chars] + "\n  ...[body truncated]"
            indented = "\n".join(f"  | {line}" for line in body.splitlines())
            lines.append(indented)
        if url:
            lines.append(f"  url: {url}")
        lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def git_blame(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    rev: str = "HEAD",
    ignore_whitespace: bool = True,
    detect_moves: bool = False,
) -> str:
    """Who last touched these lines, grouped into contiguous runs per commit."""
    args = ["blame", "--line-porcelain"]
    if ignore_whitespace:
        args.append("-w")          # skip pure-whitespace reformatting
    if detect_moves:
        args += ["-C", "-C"]       # follow code moved or copied between files
    if start_line:
        args += ["-L", f"{start_line},{end_line or start_line}"]
    args += [rev, "--", path]

    output = git(repo, args)
    if output.startswith("ERROR:"):
        return output

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("\t"):
            current["content"] = line[1:]
            entries.append(current)
            current = {}
            continue
        parts = line.split(" ", 1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            nums = rest.split()
            current["sha"] = head
            current["line"] = nums[1] if len(nums) > 1 else ""
        elif head == "author":
            current["author"] = rest
        elif head == "author-time":
            current["date"] = _epoch_to_date(rest)
        elif head == "summary":
            current["summary"] = rest
        elif head == "previous":
            current["previous"] = rest.split(" ")[0]

    if not entries:
        return f"No blame output for {path} (does the path exist at {rev}?)"

    blocks: list[list[dict[str, str]]] = []
    for entry in entries:
        if blocks and blocks[-1][0].get("sha") == entry.get("sha"):
            blocks[-1].append(entry)
        else:
            blocks.append([entry])

    lines = [f"Blame for {path} @ {rev}" + (f" lines {start_line}-{end_line or start_line}" if start_line else "")]
    if ignore_whitespace:
        lines.append("(whitespace-only changes ignored, so blame points past pure reformatting)")
    lines.append("")
    for block in blocks:
        head = block[0]
        sha = head.get("sha", "")
        span = f"{block[0].get('line', '?')}-{block[-1].get('line', '?')}" if len(block) > 1 else block[0].get("line", "?")
        url = repo.commit_url(sha)
        ledger.record(url, head.get("summary", ""))
        lines.append(f"lines {span} <- {sha[:10]}  {head.get('date', '')}  {head.get('author', '')}")
        lines.append(f"  summary: {head.get('summary', '')}")
        if head.get("previous"):
            lines.append(f"  previous revision of this line: {head['previous'][:10]} (blame that with rev=<sha>^ to go further back)")
        if url:
            lines.append(f"  url: {url}")
        for entry in block[:12]:
            lines.append(f"    {entry.get('line', ''):>6} | {entry.get('content', '')}")
        if len(block) > 12:
            lines.append(f"    ... {len(block) - 12} more lines from this commit")
        lines.append("")
    return "\n".join(lines).strip()


def git_log_for_path(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    path: str,
    limit: int = 20,
    follow: bool = True,
    body_chars: int = 1200,
) -> str:
    """Commit history for a file or directory, following renames by default."""
    args = ["log", f"--format={LOG_FORMAT}", "--date=short", f"-n{max(1, min(limit, 100))}"]
    if follow:
        args.append("--follow")     # only valid for a single path
    args += ["--", path]
    output = git(repo, args)
    if output.startswith("ERROR:") and follow:
        output = git(repo, ["log", f"--format={LOG_FORMAT}", "--date=short", f"-n{limit}", "--", path])
    if output.startswith("ERROR:"):
        return output
    commits = _parse_log(output)
    header = f"{len(commits)} commit(s) touching {path}:\n\n"
    return header + _render_commits(repo, commits, ledger, body_chars)


def git_show(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    sha: str,
    include_patch: bool = False,
    path_filter: str = "",
    max_patch_chars: int = 8000,
) -> str:
    """Full message for a commit, plus a stat summary and optional diff."""
    meta = git(repo, ["show", "--no-patch", f"--format={LOG_FORMAT}", sha])
    if meta.startswith("ERROR:"):
        return meta
    commits = _parse_log(meta)
    if not commits:
        return f"Commit {sha} not found."
    commit = commits[0]
    url = repo.commit_url(commit["sha"])
    ledger.record(url, commit["subject"])

    parts = [
        f"commit {commit['sha']}",
        f"author: {commit['author']}   date: {commit['date']}",
        f"subject: {commit['subject']}",
    ]
    if commit["body"]:
        parts.append("\nmessage body:\n" + commit["body"])
    if url:
        parts.append(f"url: {url}")

    stat_args = ["show", "--stat", "--format=", sha]
    if path_filter:
        stat_args += ["--", path_filter]
    stat = git(repo, stat_args)
    if not stat.startswith("ERROR:") and stat.strip():
        parts.append("\nfiles changed:\n" + stat.strip()[:3000])

    if include_patch:
        patch_args = ["show", "--format=", "--unified=3", sha]
        if path_filter:
            patch_args += ["--", path_filter]
        patch = git(repo, patch_args)
        if not patch.startswith("ERROR:"):
            if len(patch) > max_patch_chars:
                patch = patch[:max_patch_chars] + "\n...[patch truncated; re-run with a path_filter to narrow it]"
            parts.append("\npatch:\n" + patch)

    return "\n".join(parts)


def git_search_commits(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    query: str,
    mode: str = "content",
    path: str = "",
    limit: int = 15,
    regex: bool = False,
) -> str:
    """Find commits by the code they changed (pickaxe) or by message text.

    ``mode="content"`` answers "when did this string appear or disappear?",
    which is usually the fastest way to find the commit that introduced a
    workaround. ``mode="message"`` greps commit messages instead.
    """
    args = ["log", f"--format={LOG_FORMAT}", "--date=short", f"-n{max(1, min(limit, 50))}"]
    if mode == "message":
        args += ["--grep", query, "-i"]
    else:
        args.append(f"-S{query}")
        if regex:
            args.append("--pickaxe-regex")
    if path:
        args += ["--", path]
    output = git(repo, args)
    if output.startswith("ERROR:"):
        return output
    commits = _parse_log(output)
    label = "changed the string" if mode == "content" else "mention"
    return f"{len(commits)} commit(s) that {label} {query!r}:\n\n" + _render_commits(repo, commits, ledger, 1200)


def git_releases_containing(repo: Repo, ledger: EvidenceLedger, *, sha: str, limit: int = 8) -> str:
    """Which tags/releases first shipped a commit. Useful for dating impact."""
    output = git(repo, ["tag", "--contains", sha, "--sort=creatordate"])
    if output.startswith("ERROR:"):
        return output
    tags = [tag for tag in output.split() if tag]
    if not tags:
        return f"No tags contain {sha[:10]} (it may be unreleased, or the clone has no tags)."
    shown = tags[:limit]
    lines = [f"{sha[:10]} first shipped in: {shown[0]}", f"contained in {len(tags)} tag(s): {', '.join(shown)}"]
    if len(tags) > limit:
        lines.append(f"...and {len(tags) - limit} more")
    return "\n".join(lines)


def git_repo_overview(repo: Repo, ledger: EvidenceLedger) -> str:
    """Cheap orientation: default branch, age, size, most-churned files."""
    parts = [f"repo: {repo.slug}", f"local path: {repo.path}"]
    head = git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    parts.append(f"checked-out branch: {head}")
    count = git(repo, ["rev-list", "--count", "HEAD"]).strip()
    parts.append(f"commits on HEAD: {count}")
    first = git(repo, ["log", "--reverse", "--date=short", "--format=%ad", "--max-parents=0"]).strip().splitlines()
    last = git(repo, ["log", "-1", "--date=short", "--format=%ad"]).strip()
    if first:
        parts.append(f"history spans: {first[0]} -> {last}")
    tags = git(repo, ["tag", "--sort=-creatordate"]).split()
    if tags:
        parts.append(f"latest tags: {', '.join(tags[:8])}")
    return "\n".join(parts)
