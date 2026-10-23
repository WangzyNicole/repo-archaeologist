"""The investigation loop.

The agent is given evidence tools and a method, not a fixed script. It decides
where to dig. What makes the output trustworthy is not the loop but the two
constraints around it: every tool result carries canonical URLs, and every
citation in the final answer is checked against the URLs the tools actually
returned before the report is rendered.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from .config import CONFIG
from .evidence import EvidenceLedger
from .models import Findings, Investigation, ToolCall
from .repo import Repo, resolve_repo
from .tools import SUBMIT_TOOL, Toolbox

Emit = Callable[[dict[str, Any]], None]

SYSTEM_PROMPT = """\
You are a software archaeologist. Someone points you at a piece of code and asks
"why is it like this?" Your job is to reconstruct the *reasoning* behind it from
the historical record — not to restate what the code does or narrate the diff.

An answer that describes the current behaviour is a failure. An answer that says
"commit abc123 changed this line" is a failure. A good answer says what problem
someone hit, what they considered, what they chose, and what constraint made
that choice necessary — and shows you where that is written down.

METHOD

1. Locate. Find the exact code in question: grep for the symbol, read the file
   around it. Do not investigate a line you have not read.
2. Attribute. Blame the lines to get candidate commits.
3. Bridge. Get from the commit to the conversation: find its PR, read the
   description, then the review comments. Follow "Fixes #N" style links to the
   originating issue. The reasoning almost never lives in the commit message —
   it lives in the discussion.
4. Widen only if needed. If the record is thin, search issues by symptom, or
   pickaxe-search for when the strange string first appeared.
5. Corroborate. Before you conclude, check that your story is consistent with
   the dates, and that you are not resting on a single ambiguous sentence.

WHAT EXPERIENCED ARCHAEOLOGISTS KNOW

- Blame's first answer is usually a lie. It points at reformatting, a mass
  rename, a lint sweep, or a file move. If the commit that "wrote" the line has
  a subject like "apply black", "reformat", "move X to Y", or touches hundreds
  of files, it did not author this logic. Re-blame with rev='<that sha>^' to
  step past it, or set detect_moves=true. Keep stepping back until you land on
  a commit that was actually about this behaviour.
- Odd-looking code is usually a scar. A sleep, a retry count, a magic number, a
  defensive re-check, a version pin, a try/except around something that should
  not fail — each one probably has an incident behind it. Look for that incident
  rather than judging the code.
- Read the rejected alternative. Review threads often contain "why not just do
  X?" and the answer. That answer *is* the reasoning, and it is frequently
  nowhere else in the repo.
- Check the negative space. If something was tried and reverted, a pickaxe
  search on the removed string finds it. A revert is strong evidence about what
  does not work.
- Watch for external causes. Workarounds are often tied to a specific version of
  a dependency, a platform bug, or a since-fixed upstream issue. Dates and
  release tags help you tell whether the constraint still holds.
- A dependency pin is a scar with a version number. For "why is X pinned,
  capped or excluded?", find the line in the manifest (requirements*.txt,
  pyproject.toml, setup.py/cfg, package.json, go.mod, Cargo.toml, Gemfile...)
  and pickaxe-search the exact constraint string (e.g. '<2.0' or '!=1.4.1') to
  find the commit that introduced it — blame alone often lands on a later bump.
  Lockfile churn and bot upgrades (dependabot, renovate) are the reformatting of
  dependency history: step past them. The reason is usually an upstream breakage,
  so look for the upstream issue or changelog linked from the PR, and check
  whether a later release lifted the constraint.

EVIDENCE DISCIPLINE

- Cite only URLs that appeared verbatim in a tool result. Never construct a URL
  from a number you inferred. Bare references are held to the same rule: write
  a sha or a #number only if a tool showed it to you. Every citation is checked
  after you submit; unretrieved ones are flagged, and an answer that cites no
  retrieved evidence at all is flagged as unsupported.
- Separate record from inference in your wording. "The PR states..." for what is
  written; "this suggests..." for what you concluded. Never blur the two.
- If the record does not answer the question, say so and report what you ruled
  out. A documented dead end is a legitimate, useful result. Confident fiction
  is the only unacceptable outcome.

BUDGET

Work in depth, not breadth: a handful of well-chosen calls beats twenty shallow
ones. Aim to finish inside about 25 tool calls. When new calls stop changing
your understanding, stop and call submit_findings — exactly once.
"""


def _initial_prompt(repo: Repo, question: str, target: str) -> str:
    lines = [
        f"Repository: {repo.slug}",
        f"GitHub: https://github.com/{repo.owner}/{repo.name}" if repo.has_github else "GitHub: not available (local repo only; GitHub tools are disabled)",
    ]
    if target:
        lines.append(f"Focus: {target}")
    lines.append("")
    lines.append(f"Question: {question}")
    lines.append("")
    if target:
        lines.append("Start by reading the code at the focus location, then blame it.")
    else:
        lines.append("No specific file was given. Locate the relevant code first, then investigate.")
    return "\n".join(lines)


def _noop(_event: dict[str, Any]) -> None:
    return None


# Thinking configuration differs by model generation, and the difference is a
# hard 400 rather than something we can feature-detect up front:
#   Claude 4.7+            adaptive thinking + output_config.effort
#   Claude 4.5 and earlier manual thinking with budget_tokens (adaptive is rejected)
# Rather than pin one shape and break on the other, start at the newer form and
# step down when the API tells us it is unsupported.
_DOWNGRADE = {"adaptive": "manual", "manual": "off"}
_THINKING_ERROR_MARKERS = ("thinking", "output_config", "effort")


class ModelCaller:
    """Calls the Messages API, negotiating the thinking parameters once."""

    def __init__(self, client: Any, emit: Emit) -> None:
        self.client = client
        self.emit = emit
        mode = CONFIG.thinking_mode
        self.mode = mode if mode in {"adaptive", "manual", "off"} else "adaptive"

    def thinking_kwargs(self) -> dict[str, Any]:
        if self.mode == "adaptive":
            kwargs: dict[str, Any] = {"thinking": {"type": "adaptive"}}
            if CONFIG.effort:
                kwargs["output_config"] = {"effort": CONFIG.effort}
            return kwargs
        if self.mode == "manual" and CONFIG.thinking_budget >= 1024:
            return {"thinking": {"type": "enabled", "budget_tokens": CONFIG.thinking_budget}}
        return {}

    def create(self, **request: Any) -> Any:
        while True:
            try:
                return self.client.messages.create(**request, **self.thinking_kwargs())
            except Exception as exc:  # noqa: BLE001 - inspected, then re-raised if unrelated
                if not self._step_down(exc):
                    raise

    def _step_down(self, exc: Exception) -> bool:
        message = str(exc)
        if "400" not in message and "invalid_request" not in message:
            return False
        if not any(marker in message for marker in _THINKING_ERROR_MARKERS):
            return False
        next_mode = _DOWNGRADE.get(self.mode)
        if next_mode is None:
            return False
        self.emit(
            {
                "type": "status",
                "message": f"This model rejected {self.mode} thinking; retrying with {next_mode}.",
            }
        )
        self.mode = next_mode
        return True


def run_investigation(
    *,
    question: str,
    repo_target: str,
    focus: str = "",
    on_event: Emit | None = None,
    refresh: bool = False,
    max_turns: int | None = None,
) -> Investigation:
    """Run one investigation end to end and return the completed record."""
    emit = on_event or _noop
    max_turns = max_turns or CONFIG.max_turns

    investigation = Investigation(question=question, repo=repo_target, target=focus)

    try:
        import anthropic
    except ImportError:
        investigation.error = "The anthropic package is not installed. Run: pip install -r requirements.txt"
        emit({"type": "error", "message": investigation.error})
        return investigation

    if not CONFIG.anthropic_api_key:
        investigation.error = "ANTHROPIC_API_KEY is not set. Put it in your environment or a .env file."
        emit({"type": "error", "message": investigation.error})
        return investigation

    try:
        emit({"type": "status", "message": f"Resolving {repo_target}"})
        repo = resolve_repo(repo_target, refresh=refresh, progress=lambda m: emit({"type": "status", "message": m}))
    except Exception as exc:  # noqa: BLE001
        investigation.error = f"Could not open the repository: {exc}"
        emit({"type": "error", "message": investigation.error})
        return investigation

    investigation.repo = repo.slug
    ledger = EvidenceLedger()
    toolbox = Toolbox(repo, ledger)
    if not toolbox.github_enabled:
        emit({"type": "status", "message": "No GitHub remote detected — running on git history alone."})
    elif not CONFIG.github_token:
        emit({"type": "status", "message": "No GITHUB_TOKEN set — API limited to 60 requests/hour."})

    caller = ModelCaller(anthropic.Anthropic(api_key=CONFIG.anthropic_api_key), emit)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _initial_prompt(repo, question, focus)}]
    findings: Findings | None = None
    nudges = 0

    emit({"type": "status", "message": f"Investigating {repo.slug}"})

    for turn in range(max_turns):
        request: dict[str, Any] = {
            "model": CONFIG.model,
            "max_tokens": CONFIG.max_output_tokens,
            "system": SYSTEM_PROMPT,
            "tools": toolbox.specs,
            "messages": messages,
        }

        try:
            response = caller.create(**request)
        except Exception as exc:  # noqa: BLE001
            investigation.error = f"Model call failed on turn {turn + 1}: {exc}"
            emit({"type": "error", "message": investigation.error})
            break

        _accumulate_usage(investigation, response)
        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            kind = getattr(block, "type", "")
            if kind == "thinking":
                text = getattr(block, "thinking", "")
                if text:
                    investigation.thinking.append(text)
                    emit({"type": "thinking", "text": text})
            elif kind == "text":
                if block.text.strip():
                    emit({"type": "reasoning", "text": block.text})
            elif kind == "tool_use":
                if block.name == SUBMIT_TOOL:
                    findings = Findings.from_tool_input(dict(block.input))
                    break
                emit({"type": "tool_call", "name": block.name, "arguments": dict(block.input)})
                result, is_error, duration = toolbox.run(block.name, dict(block.input))
                investigation.trace.append(
                    ToolCall(
                        name=block.name,
                        arguments=dict(block.input),
                        result_preview=result[:1500],
                        duration_ms=duration,
                        error=is_error,
                    )
                )
                emit(
                    {
                        "type": "tool_result",
                        "name": block.name,
                        "preview": _preview(result),
                        "error": is_error,
                        "duration_ms": duration,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": is_error,
                    }
                )

        if findings is not None:
            break

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            continue

        # The model stopped without acting. Nudge it, but not forever.
        nudges += 1
        if nudges > 2:
            break
        messages.append(
            {
                "role": "user",
                "content": (
                    "Keep going: either call another evidence tool, or call submit_findings with what you have. "
                    "If the record is thin, submit with low confidence and list the open questions."
                ),
            }
        )

    if findings is None and not investigation.error:
        emit({"type": "status", "message": "Wrapping up — asking for a final answer from the evidence gathered."})
        findings = _force_submit(caller.client, toolbox, messages, investigation, emit)

    if findings is not None:
        check = ledger.check(
            findings.answer,
            *[event.url for event in findings.timeline],
            *[event.ref for event in findings.timeline],
        )
        findings.verified_citations = check.verified
        findings.unverified_urls = check.unverified_urls
        findings.unverified_refs = check.unverified_refs
        findings.support = check.verdict
        if check.verdict == "unsupported":
            emit({"type": "status", "message": "Unsupported answer: it cites nothing that was retrieved during the investigation."})
        elif findings.unverified:
            emit({"type": "status", "message": f"{len(findings.unverified)} citation(s) could not be verified against retrieved evidence."})
        investigation.findings = findings

    investigation.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    emit({"type": "done", "investigation": investigation.to_dict()})
    return investigation


def _force_submit(client, toolbox: Toolbox, messages: list, investigation: Investigation, emit: Emit) -> Findings | None:
    """Out of turns: make one non-thinking call that must produce the answer."""
    try:
        response = client.messages.create(
            model=CONFIG.model,
            max_tokens=CONFIG.max_output_tokens,
            system=SYSTEM_PROMPT,
            tools=toolbox.specs,
            tool_choice={"type": "tool", "name": SUBMIT_TOOL},
            messages=messages
            + [
                {
                    "role": "user",
                    "content": (
                        "You are out of investigation budget. Submit your findings now using only the evidence "
                        "already retrieved. Be honest about confidence and list what remains unresolved."
                    ),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        investigation.error = f"Could not obtain a final answer: {exc}"
        emit({"type": "error", "message": investigation.error})
        return None

    _accumulate_usage(investigation, response)
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == SUBMIT_TOOL:
            return Findings.from_tool_input(dict(block.input))
    return None


def _accumulate_usage(investigation: Investigation, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if not usage:
        return
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, field, None)
        if isinstance(value, int):
            investigation.usage[field] = investigation.usage.get(field, 0) + value


def _preview(result: str, limit: int = 400) -> str:
    text = result.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def describe_arguments(arguments: dict[str, Any]) -> str:
    """Compact one-line rendering of tool arguments, for progress output."""
    if not arguments:
        return ""
    parts = []
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 40:
            value = value[:40] + "…"
        parts.append(f"{key}={json.dumps(value) if not isinstance(value, str) else value}")
    return " ".join(parts)
