"""Resolving a repo argument into a local clone we can run git against."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG

_GITHUB_PATTERNS = (
    re.compile(r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
    re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$"),
)


class RepoError(RuntimeError):
    pass


@dataclass
class Repo:
    """A local working copy plus, when we know it, its GitHub coordinates."""

    path: Path
    owner: str | None = None
    name: str | None = None

    @property
    def slug(self) -> str:
        if self.owner and self.name:
            return f"{self.owner}/{self.name}"
        return self.path.name

    @property
    def has_github(self) -> bool:
        return bool(self.owner and self.name)

    def commit_url(self, sha: str) -> str:
        if not self.has_github:
            return ""
        return f"https://github.com/{self.owner}/{self.name}/commit/{sha}"

    def blob_url(self, path: str, line: int | None = None, rev: str = "HEAD") -> str:
        if not self.has_github:
            return ""
        anchor = f"#L{line}" if line else ""
        return f"https://github.com/{self.owner}/{self.name}/blob/{rev}/{path}{anchor}"


def _run_git(args: list[str], cwd: Path | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RepoError((result.stderr or result.stdout).strip()[:2000])
    return result.stdout


def _parse_github(target: str) -> tuple[str, str] | None:
    for pattern in _GITHUB_PATTERNS:
        match = pattern.match(target.strip())
        if match:
            return match.group("owner"), match.group("repo")
    return None


def _github_coords_from_remote(path: Path) -> tuple[str, str] | None:
    try:
        remote = _run_git(["remote", "get-url", "origin"], cwd=path, timeout=15).strip()
    except (RepoError, subprocess.SubprocessError):
        return None
    return _parse_github(remote)


def resolve_repo(target: str, *, refresh: bool = False, progress=None) -> Repo:
    """Accept a GitHub URL, an ``owner/repo`` slug, or a local path.

    Remote repos are cloned into the cache directory (blobless, so the initial
    clone is fast) and reused across runs. Blame and diffs backfill blobs on
    demand, which keeps the first run responsive on large repositories.
    """

    def say(message: str) -> None:
        if progress:
            progress(message)

    local = Path(target).expanduser()
    if local.is_dir() and (local / ".git").exists():
        coords = _github_coords_from_remote(local)
        return Repo(path=local.resolve(), owner=coords[0] if coords else None, name=coords[1] if coords else None)

    coords = _parse_github(target)
    if not coords:
        raise RepoError(
            f"Could not interpret {target!r}. Give a GitHub URL, an owner/repo slug, or a path to a local git repo."
        )

    owner, name = coords
    CONFIG.ensure_dirs()
    dest = CONFIG.repo_cache / f"{owner}__{name}"

    if dest.exists() and not (dest / ".git").exists():
        shutil.rmtree(dest)

    if dest.exists():
        if refresh:
            say(f"Updating cached clone of {owner}/{name}")
            try:
                _run_git(["fetch", "--all", "--quiet", "--tags"], cwd=dest)
            except RepoError as exc:
                say(f"Fetch failed, using cached copy ({exc.__class__.__name__})")
        else:
            say(f"Using cached clone of {owner}/{name}")
    else:
        say(f"Cloning {owner}/{name} (history only, blobs on demand)")
        _run_git(
            [
                "clone",
                "--filter=blob:none",
                "--quiet",
                f"https://github.com/{owner}/{name}.git",
                str(dest),
            ],
            timeout=600,
        )

    return Repo(path=dest, owner=owner, name=name)
