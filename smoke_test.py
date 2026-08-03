"""Smoke test for everything below the model.

Exercises the whole stack except the Anthropic call: repo resolution, all git
and code tools, the evidence ledger, citation verification, markdown rendering
and report generation. Needs no API key — clones a small public repo and runs
against real history.

    python smoke_test.py [path-or-slug]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from archaeo.evidence import EvidenceLedger  # noqa: E402
from archaeo.markdown_lite import to_html  # noqa: E402
from archaeo.models import Findings, Investigation, ToolCall  # noqa: E402
from archaeo.repo import resolve_repo  # noqa: E402
from archaeo.tools import Toolbox  # noqa: E402
from archaeo.ui import standalone_html  # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"  [{PASS if condition else FAIL}] {label}" + (f"  — {detail}" if detail and not condition else ""))
    if not condition:
        failures += 1


def main(target: str) -> int:
    print(f"\nResolving {target}")
    repo = resolve_repo(target, progress=lambda m: print(f"  · {m}"))
    print(f"  repo: {repo.slug}   github: {repo.has_github}\n")

    ledger = EvidenceLedger()
    box = Toolbox(repo, ledger)

    print("Tool specs")
    names = [spec["name"] for spec in box.specs]
    check("submit_findings is offered", "submit_findings" in names)
    check("git tools are offered", "git_blame" in names and "git_search_commits" in names)
    check("github tools follow the remote", ("github_pull_request" in names) == repo.has_github)
    for spec in box.specs:
        schema = spec["input_schema"]
        ok = schema["type"] == "object" and all(r in schema["properties"] for r in schema.get("required", []))
        check(f"schema valid: {spec['name']}", ok)

    print("\nOrientation tools")
    overview, err, _ = box.run("repo_overview", {})
    check("repo_overview returns history span", "commits on HEAD" in overview and not err, overview[:200])

    listing, err, _ = box.run("list_dir", {"path": ".", "depth": 1})
    check("list_dir lists the root", not err and len(listing.splitlines()) > 2, listing[:200])

    print("\nSearch and read")
    hits, err, _ = box.run("grep_repo", {"pattern": "def ", "glob": "*.py", "max_results": 5})
    check("grep_repo finds code", not err and ":" in hits, hits[:200])

    first_hit = next((line for line in hits.splitlines() if ":" in line), "")
    path = first_hit.split(":")[0] if first_hit else "README.md"
    line_no = int(first_hit.split(":")[1]) if first_hit.count(":") >= 2 and first_hit.split(":")[1].isdigit() else 1

    content, err, _ = box.run("read_file", {"path": path, "start_line": max(1, line_no - 3), "end_line": line_no + 3})
    check(f"read_file reads {path}", not err and "|" in content, content[:200])

    escaped, err, _ = box.run("read_file", {"path": "../../etc/passwd"})
    check("read_file refuses to escape the repo", err, escaped[:120])

    print("\nHistory tools")
    blame, err, _ = box.run("git_blame", {"path": path, "start_line": max(1, line_no - 2), "end_line": line_no + 2})
    check("git_blame attributes lines", not err and "<-" in blame, blame[:300])

    sha = ""
    for row in blame.splitlines():
        if "<-" in row:
            sha = row.split("<-")[1].split()[0]
            break
    check("git_blame yields a sha", bool(sha), blame[:200])

    log, err, _ = box.run("git_log_for_path", {"path": path, "limit": 5})
    check("git_log_for_path returns commits", not err and "subject:" in log, log[:300])

    if sha:
        show, err, _ = box.run("git_show", {"sha": sha})
        check("git_show reads a commit", not err and "commit " in show, show[:300])

        patch, err, _ = box.run("git_show", {"sha": sha, "include_patch": True, "path_filter": path})
        check("git_show can include a patch", not err and ("@@" in patch or "files changed" in patch), patch[:200])

        parent_blame, err, _ = box.run("git_blame", {"path": path, "start_line": max(1, line_no - 2), "rev": f"{sha}^"})
        check("git_blame can step back past a commit", err or "<-" in parent_blame, parent_blame[:200])

        releases, err, _ = box.run("git_releases_containing", {"sha": sha})
        check("git_releases_containing responds", not err, releases[:200])

    pickaxe, err, _ = box.run("git_search_commits", {"query": "import", "mode": "content", "limit": 3})
    check("pickaxe search runs", not err, pickaxe[:200])

    grep_msg, err, _ = box.run("git_search_commits", {"query": "fix", "mode": "message", "limit": 3})
    check("message search runs", not err and "commit(s)" in grep_msg, grep_msg[:200])

    print("\nError handling")
    bad, err, _ = box.run("git_show", {"sha": "0000000000000000000000000000000000000000"})
    check("bad sha degrades to an error string", err and bad.startswith("ERROR"), bad[:120])

    unknown, err, _ = box.run("does_not_exist", {})
    check("unknown tool is reported, not raised", err, unknown[:120])

    bad_args, err, _ = box.run("git_blame", {"nonsense": 1})
    check("bad arguments are reported, not raised", err, bad_args[:120])

    print("\nEvidence ledger")
    if repo.has_github:
        real = repo.commit_url(sha) if sha else ""
        check("blame recorded its commit urls", bool(ledger.seen))
        check("a retrieved url verifies", not real or ledger.is_supported(real))
        check("short sha matches full sha", not real or ledger.is_supported(repo.commit_url(sha[:8])))
    fake = "https://github.com/some/repo/pull/999999"
    check("an invented url does not verify", not ledger.is_supported(fake))
    missing = ledger.unsupported(f"See [#999999]({fake}) for details.")
    check("unsupported() catches the invented citation", missing == [fake], str(missing))

    print("\nRendering")
    html = to_html(
        "# Heading\n\nA claim citing [abc1234](https://github.com/o/r/commit/abc1234) and "
        "[a fake](https://evil.example/nope).\n\n- point one\n- point two\n\n```\ncode <b>\n```",
        verifier=lambda url: "evil.example" not in url,
    )
    check("markdown renders headings", "<h2>Heading</h2>" in html)
    check("markdown renders lists", "<li>point one</li>" in html)
    check("markdown escapes code", "&lt;b&gt;" in html)
    check("verified citations get .cite", 'class="cite"' in html)
    check("unverified citations are flagged", "cite-unverified" in html)
    check("no raw script injection", "<script>" not in to_html("<script>alert(1)</script>"))

    investigation = Investigation(question="why is this here?", repo=repo.slug)
    investigation.trace.append(ToolCall(name="git_blame", arguments={"path": path}, result_preview="lines 1-2 <- abc", duration_ms=12))
    investigation.findings = Findings.from_tool_input(
        {
            "answer": "Because of [#1](https://github.com/o/r/pull/1).",
            "timeline": [
                {"date": "2021-05-02", "kind": "pr", "ref": "#1", "title": "Add the workaround",
                 "url": "https://github.com/o/r/pull/1", "why_it_matters": "introduced the behaviour"},
                {"date": "2019-01-01", "kind": "issue", "ref": "#0", "title": "Original report", "why_it_matters": "the symptom"},
            ],
            "confidence": "high",
            "open_questions": ["was it ever revisited?"],
        }
    )
    check("timeline is sorted oldest first", investigation.findings.timeline[0].date == "2019-01-01")
    investigation.findings.unverified_urls = ["https://github.com/o/r/pull/1"]
    page = standalone_html(investigation)
    check("report is a full html document", page.startswith("<!doctype html>") and "</html>" in page)
    check("report surfaces the unverified banner", "Unverified citations" in page)
    check("report includes the evidence trail", "git_blame" in page and "tool calls" in page)
    check("report round-trips to json", '"confidence": "high"' in investigation.to_json())

    out = Path(tempfile.gettempdir()) / "archaeo-smoke-report.html"
    out.write_text(page, encoding="utf-8")
    print(f"\n  sample report written to {out}")

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "pallets/click"))
