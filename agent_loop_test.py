"""Tests the investigation loop against a scripted model — no API key, no cost.

The loop has the parts most likely to break silently in production: turn
exhaustion, a model that stops without acting, tool errors handed back to the
model, and citation verification. Those are cheap to test with a fake client and
expensive to debug live, so they are tested here.

    python agent_loop_test.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"  [{PASS if condition else FAIL}] {label}" + (f"  — {detail}" if detail and not condition else ""))
    if not condition:
        failures += 1


# -- a fake Anthropic client ------------------------------------------------


class Block:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Usage:
    input_tokens = 100
    output_tokens = 50


class Response:
    def __init__(self, content):
        self.content = content
        self.usage = Usage()
        self.stop_reason = "tool_use"


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # Snapshot the message list: the agent mutates one list across turns,
        # so keeping the reference would show us the final state every time.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if kwargs.get("tool_choice", {}).get("name") == "submit_findings":
            return Response([Block(type="tool_use", id="forced", name="submit_findings", input=FORCED_SUBMIT)])
        if not self.script:
            return Response([Block(type="text", text="I have nothing further.")])
        step = self.script.pop(0)
        return Response(step(self.calls[-1]) if callable(step) else step)


class FakeAnthropic:
    def __init__(self, script):
        self.messages = FakeMessages(script)


class PickyMessages(FakeMessages):
    """A model that rejects thinking configurations it does not support.

    Mirrors the real 400s: Claude 4.7+ rejects manual `type: "enabled"`, and
    Claude 4.5 and earlier reject `type: "adaptive"`.
    """

    def __init__(self, script, accepts: str):
        super().__init__(script)
        self.accepts = accepts
        self.rejected: list[str] = []

    def create(self, **kwargs):
        thinking = (kwargs.get("thinking") or {}).get("type", "off")
        if thinking != self.accepts:
            self.rejected.append(thinking)
            raise RuntimeError(
                f'Error code: 400 - {{"type": "error", "error": {{"type": "invalid_request_error", '
                f'"message": "\\"thinking.type.{thinking}\\" is not supported for this model."}}}}'
            )
        return super().create(**kwargs)


def install_fake(script, messages=None) -> FakeAnthropic:
    client = FakeAnthropic(script)
    if messages is not None:
        client.messages = messages
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda **_kwargs: client  # type: ignore[attr-defined]
    sys.modules["anthropic"] = module
    return client


INVENTED_URL = "https://github.com/pallets/click/pull/999999"

SUBMIT_PAYLOAD = {
    "answer": f"A claim resting on [an invented source]({INVENTED_URL}).",
    "timeline": [],
    "confidence": "high",
    "open_questions": ["did anyone revisit it?"],
}


def submit_citing_real_evidence(call: dict) -> list:
    """Build a submission that cites one URL a tool really returned, plus one
    that was never retrieved — the exact mix the verifier has to separate."""
    import re

    seen = ""
    for message in call["messages"]:
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    match = re.search(r"https://github\.com/\S+/commit/\w+", str(part.get("content", "")))
                    if match:
                        seen = match.group(0)
    payload = {
        "answer": f"Real evidence: [a commit]({seen}). Fabricated: [a PR]({INVENTED_URL}).",
        "timeline": [
            {"date": "2020-02-02", "kind": "commit", "ref": seen.rsplit("/", 1)[-1][:7], "title": "A real commit",
             "url": seen, "why_it_matters": "retrieved during the run"},
        ],
        "confidence": "high",
        "open_questions": [],
    }
    return [Block(type="tool_use", id="t2", name="submit_findings", input=payload)]

FORCED_SUBMIT = {
    "answer": "Out of budget; partial answer.",
    "timeline": [],
    "confidence": "low",
    "open_questions": ["everything"],
}


def make_local_repo() -> str:
    """A throwaway repo with no remote: no URLs exist, so citations are bare shas."""
    import subprocess
    import tempfile

    path = tempfile.mkdtemp(prefix="archaeo-local-")
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "--quiet", path], check=True)
    for n in (1, 2):
        Path(path, "README.md").write_text(f"version {n}\n")
        subprocess.run([*git, "-C", path, "add", "README.md"], check=True)
        subprocess.run([*git, "-C", path, "commit", "--quiet", "-m", f"change {n}"], check=True)
    return path


def last_sha_shown(call: dict) -> str:
    import re

    for message in reversed(call["messages"]):
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    match = re.search(r"^- ([0-9a-f]{10})", str(part.get("content", "")), re.MULTILINE)
                    if match:
                        return match.group(1)
    return ""


def submit(answer: str, timeline: list | None = None) -> list:
    payload = {"answer": answer, "timeline": timeline or [], "confidence": "high", "open_questions": []}
    return [Block(type="tool_use", id="s", name="submit_findings", input=payload)]


def main(repo_path: str) -> int:
    from archaeo.config import CONFIG

    CONFIG.anthropic_api_key = "test-key-not-used"
    CONFIG.thinking_mode = "off"

    from archaeo.agent import run_investigation

    print("\nHappy path: tool call, then submit")
    client = install_fake(
        [
            [
                Block(type="text", text="Let me look at the history."),
                Block(type="tool_use", id="t1", name="git_log_for_path", input={"path": "README.md", "limit": 3}),
            ],
            submit_citing_real_evidence,
        ]
    )
    events: list[dict] = []
    result = run_investigation(question="why 3 retries?", repo_target=repo_path, on_event=events.append)

    check("investigation completed", result.findings is not None, result.error)
    check("tool was executed", len(result.trace) == 1 and result.trace[0].name == "git_log_for_path")
    check("trace captured output", "subject:" in result.trace[0].result_preview)
    check("usage accumulated across turns", result.usage.get("input_tokens") == 200, str(result.usage))
    check("finished_at is set", bool(result.finished_at))

    kinds = [event["type"] for event in events]
    check("emitted tool_call and tool_result", "tool_call" in kinds and "tool_result" in kinds, str(kinds))
    check("emitted done last", kinds[-1] == "done", str(kinds[-1]))

    findings = result.findings
    check("confidence preserved", findings.confidence == "high")
    check(
        "invented citation flagged, retrieved one accepted",
        findings.unverified_urls == [INVENTED_URL],
        str(findings.unverified_urls),
    )
    check("timeline sha ref verified alongside its url", findings.unverified_refs == [], str(findings.unverified_refs))
    check("mixed citations give a partial verdict", findings.support == "partial", findings.support)
    check("verdict is in the JSON record", '"support": "partial"' in result.to_json())

    print("\nTool results are fed back to the model")
    second_call = client.messages.calls[1]
    last_message = second_call["messages"][-1]
    check("tool_result returned as a user turn", last_message["role"] == "user")
    check("tool_result carries the id", last_message["content"][0]["tool_use_id"] == "t1")
    check("assistant turn was preserved", second_call["messages"][1]["role"] == "assistant")

    print("\nTool errors reach the model instead of crashing")
    install_fake(
        [
            [Block(type="tool_use", id="t1", name="git_show", input={"sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"})],
            [Block(type="tool_use", id="t2", name="submit_findings", input=SUBMIT_PAYLOAD)],
        ]
    )
    result = run_investigation(question="?", repo_target=repo_path, on_event=lambda e: None)
    check("errored tool is marked in the trace", result.trace[0].error)
    check("run still produced findings", result.findings is not None)
    check("an answer citing only invented sources is unsupported", result.findings.support == "unsupported")

    print("\nTurn exhaustion forces a final answer")
    install_fake([[Block(type="tool_use", id=f"t{i}", name="repo_overview", input={})] for i in range(10)])
    result = run_investigation(question="?", repo_target=repo_path, max_turns=3, on_event=lambda e: None)
    check("stopped at the turn cap", len(result.trace) == 3, str(len(result.trace)))
    check("forced submit produced findings", result.findings is not None)
    check("forced answer is low confidence", result.findings and result.findings.confidence == "low")
    check("forced answer citing nothing is flagged unsupported", result.findings and result.findings.support == "unsupported")

    print("\nBare references are held to the same standard as links")
    local = make_local_repo()
    log_step = [Block(type="tool_use", id="t1", name="git_log_for_path", input={"path": "README.md"})]
    shown: dict[str, str] = {}

    def cite_bare(call: dict) -> list:
        shown["sha"] = last_sha_shown(call)
        short = shown["sha"][:7]
        return submit(
            f"Commit `{short}` changed it, after the discussion in #4242.",
            [{"date": "2024-01-01", "kind": "commit", "ref": short, "title": "the change", "why_it_matters": "it"}],
        )

    install_fake([log_step, cite_bare])
    result = run_investigation(question="?", repo_target=local, on_event=lambda e: None)
    findings = result.findings
    check("local repo without a GitHub remote still runs", findings is not None, result.error)
    check("a sha a tool showed verifies with no URL", shown["sha"][:7] in findings.verified_citations, str(findings.verified_citations))
    check("a #number no tool returned is flagged", findings.unverified_refs == ["#4242"], str(findings.unverified_refs))
    check("one real, one invented ref is partial", findings.support == "partial", findings.support)

    install_fake([log_step, lambda call: submit(f"Commit {last_sha_shown(call)[:8]} changed it.")])
    result = run_investigation(question="?", repo_target=local, on_event=lambda e: None)
    check("an answer citing only retrieved refs is supported", result.findings.support == "supported", result.findings.support)

    install_fake(
        [
            [Block(type="tool_use", id="t1", name="git_show", input={"sha": "1234567abcdef1234567abcdef1234567abcdef0"})],
            submit("Commit 1234567abc introduced it."),
        ]
    )
    result = run_investigation(question="?", repo_target=local, on_event=lambda e: None)
    check(
        "a sha echoed back by a failed lookup is not citable",
        result.findings.unverified_refs == ["1234567abc"] and result.findings.support == "unsupported",
        str(result.findings.unverified_refs),
    )

    events = []
    install_fake([submit("It is probably like this for performance reasons.")])
    result = run_investigation(question="?", repo_target=local, on_event=events.append)
    check("a confident answer citing nothing is flagged unsupported", result.findings.support == "unsupported")
    check("nothing to list as unverified, yet still flagged", result.findings.unverified == [])
    check("the unsupported verdict was reported live", any("Unsupported answer" in e.get("message", "") for e in events))

    print("\nA model that stalls is nudged, then given up on")
    install_fake([[Block(type="text", text="Hmm.")] for _ in range(6)])
    result = run_investigation(question="?", repo_target=repo_path, max_turns=8, on_event=lambda e: None)
    check("no infinite loop on a stalled model", result.findings is not None or bool(result.error))

    print("\nThinking configuration is negotiated with the model")
    submit_step = [Block(type="tool_use", id="t1", name="submit_findings", input=SUBMIT_PAYLOAD)]

    CONFIG.thinking_mode = "auto"
    modern = PickyMessages([submit_step], accepts="adaptive")
    install_fake([], messages=modern)
    result = run_investigation(question="?", repo_target=repo_path, on_event=lambda e: None)
    check("adaptive-only model works first try", result.findings is not None and modern.rejected == [], str(modern.rejected))
    check("adaptive request carried an effort level", modern.calls[0].get("output_config", {}).get("effort") == CONFIG.effort)

    legacy = PickyMessages([submit_step], accepts="enabled")
    install_fake([], messages=legacy)
    events = []
    result = run_investigation(question="?", repo_target=repo_path, on_event=events.append)
    check("older model falls back to a manual budget", result.findings is not None, str(legacy.rejected))
    check("adaptive was tried first, then stepped down", legacy.rejected == ["adaptive"], str(legacy.rejected))
    check("fallback sent budget_tokens", legacy.calls[-1]["thinking"] == {"type": "enabled", "budget_tokens": CONFIG.thinking_budget})
    check("the step-down was reported to the user", any("thinking" in e.get("message", "") for e in events if e["type"] == "status"))

    stubborn = PickyMessages([submit_step], accepts="off")
    install_fake([], messages=stubborn)
    result = run_investigation(question="?", repo_target=repo_path, on_event=lambda e: None)
    check("a model that refuses all thinking still runs", result.findings is not None, str(stubborn.rejected))
    check("stepped down twice before giving up on thinking", stubborn.rejected == ["adaptive", "enabled"], str(stubborn.rejected))

    unrelated = PickyMessages([submit_step], accepts="adaptive")
    unrelated.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("Error code: 500 - overloaded"))
    install_fake([], messages=unrelated)
    result = run_investigation(question="?", repo_target=repo_path, on_event=lambda e: None)
    check("unrelated API errors are not mistaken for thinking errors", "500" in result.error, result.error)
    CONFIG.thinking_mode = "off"

    print("\nMissing API key is reported cleanly")
    CONFIG.anthropic_api_key = None
    result = run_investigation(question="?", repo_target=repo_path, on_event=lambda e: None)
    check("missing key produces an error, not a traceback", "ANTHROPIC_API_KEY" in result.error)
    CONFIG.anthropic_api_key = "test-key-not-used"

    print("\nA bad repo argument is reported cleanly")
    result = run_investigation(question="?", repo_target="/definitely/not/a/repo", on_event=lambda e: None)
    check("bad repo produces an error, not a traceback", "Could not open the repository" in result.error)

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
