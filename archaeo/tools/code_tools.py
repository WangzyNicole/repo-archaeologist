"""Reading the code itself: file contents, search, directory listing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..evidence import EvidenceLedger
from ..repo import Repo

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}


def _safe_path(repo: Repo, path: str) -> Path | None:
    """Keep the agent inside the repo. No traversal out of the clone."""
    candidate = (repo.path / path.lstrip("/")).resolve()
    try:
        candidate.relative_to(repo.path.resolve())
    except ValueError:
        return None
    return candidate


def read_file(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    path: str,
    start_line: int = 1,
    end_line: int = 0,
    rev: str = "",
) -> str:
    """Read a slice of a file, at HEAD or at any historical revision."""
    if rev:
        from .git_tools import git

        content = git(repo, ["show", f"{rev}:{path}"])
        if content.startswith("ERROR:"):
            return content
        lines = content.splitlines()
        source = f"{path} @ {rev}"
    else:
        target = _safe_path(repo, path)
        if target is None:
            return "ERROR: path escapes the repository."
        if not target.is_file():
            return f"ERROR: {path} is not a file in this repo."
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"ERROR: {exc}"
        source = f"{path} @ working tree"

    start = max(1, start_line)
    end = end_line if end_line and end_line >= start else min(len(lines), start + 199)
    end = min(end, len(lines))
    if start > len(lines):
        return f"{source} has only {len(lines)} lines; {start} is past the end."

    ledger.record(repo.blob_url(path, start), path)
    body = "\n".join(f"{n:>6} | {lines[n - 1]}" for n in range(start, end + 1))
    footer = "" if end >= len(lines) else f"\n...[{len(lines) - end} more lines]"
    return f"{source}  (lines {start}-{end} of {len(lines)})\n{body}{footer}"


def grep_repo(
    repo: Repo,
    ledger: EvidenceLedger,
    *,
    pattern: str,
    glob: str = "",
    max_results: int = 40,
    case_insensitive: bool = False,
) -> str:
    """Search the working tree. Uses ripgrep when available, git grep otherwise."""
    max_results = max(1, min(max_results, 200))
    if shutil.which("rg"):
        args = ["rg", "--line-number", "--no-heading", "--color=never", "-m", "5", "--max-columns", "300"]
        if case_insensitive:
            args.append("-i")
        if glob:
            args += ["--glob", glob]
        args += [pattern, "."]
    else:
        args = ["git", "grep", "-n", "-I"]
        if case_insensitive:
            args.append("-i")
        args += ["-e", pattern]
        if glob:
            args += ["--", glob]

    try:
        result = subprocess.run(args, cwd=str(repo.path), capture_output=True, text=True, timeout=60, errors="replace")
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out; narrow the pattern or add a glob."
    if result.returncode not in (0, 1):
        return f"ERROR: {(result.stderr or result.stdout).strip()[:800]}"

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return f"No matches for {pattern!r}" + (f" in {glob}" if glob else "")
    shown = lines[:max_results]
    out = "\n".join(line.lstrip("./") for line in shown)
    if len(lines) > max_results:
        out += f"\n...[{len(lines) - max_results} more matches suppressed]"
    return out


def list_dir(repo: Repo, ledger: EvidenceLedger, *, path: str = ".", depth: int = 1) -> str:
    """Shallow directory listing so the agent can orient itself."""
    target = _safe_path(repo, path)
    if target is None or not target.is_dir():
        return f"ERROR: {path} is not a directory in this repo."

    depth = max(1, min(depth, 3))
    entries: list[str] = []

    def walk(directory: Path, level: int, prefix: str) -> None:
        if level > depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if child.name in SKIP_DIRS or child.name.startswith(".") and child.is_dir():
                continue
            if child.is_dir():
                entries.append(f"{prefix}{child.name}/")
                walk(child, level + 1, prefix + "  ")
            else:
                entries.append(f"{prefix}{child.name}")
            if len(entries) > 400:
                return

    walk(target, 1, "")
    listing = "\n".join(entries[:400])
    if len(entries) > 400:
        listing += "\n...[truncated]"
    return f"{path}/\n{listing}"
