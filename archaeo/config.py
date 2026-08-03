"""Configuration, loaded from environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader so we don't take a dependency on python-dotenv."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()


def _cache_root() -> Path:
    override = os.environ.get("ARCHAEO_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "repo-archaeologist"


@dataclass
class Config:
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY"))
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    model: str = field(default_factory=lambda: os.environ.get("ARCHAEO_MODEL", "claude-sonnet-5"))

    # Budgets. The agent is a research loop, so these are the main cost controls.
    max_turns: int = field(default_factory=lambda: int(os.environ.get("ARCHAEO_MAX_TURNS", "40")))
    max_output_tokens: int = field(default_factory=lambda: int(os.environ.get("ARCHAEO_MAX_TOKENS", "8000")))

    # Thinking. Newer models take adaptive thinking plus an effort level; models
    # from the 4.5 generation and earlier take a manual token budget and reject
    # adaptive. "auto" starts adaptive and steps down if the model refuses, so
    # the same config works across both without the caller knowing which is which.
    thinking_mode: str = field(default_factory=lambda: os.environ.get("ARCHAEO_THINKING", "auto"))
    effort: str = field(default_factory=lambda: os.environ.get("ARCHAEO_EFFORT", "high"))
    thinking_budget: int = field(default_factory=lambda: int(os.environ.get("ARCHAEO_THINKING_BUDGET", "3000")))

    # Truncation limits, applied to every tool result before it reaches the model.
    max_tool_result_chars: int = 14_000
    max_body_chars: int = 4_000
    max_comments: int = 30

    cache_dir: Path = field(default_factory=_cache_root)

    @property
    def repo_cache(self) -> Path:
        return self.cache_dir / "repos"

    @property
    def http_cache(self) -> Path:
        return self.cache_dir / "http"

    @property
    def run_dir(self) -> Path:
        return self.cache_dir / "runs"

    def ensure_dirs(self) -> None:
        for path in (self.repo_cache, self.http_cache, self.run_dir):
            path.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
