"""Command line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys
import webbrowser
from pathlib import Path

from .agent import describe_arguments, run_investigation
from .config import CONFIG
from .models import Investigation
from .ui import standalone_html, timestamp_slug

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


DIM, BOLD = "2", "1"
AMBER, BLUE, GREEN, RED, GREY = "33", "36", "32", "31", "90"

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")


def _plain(markdown: str) -> str:
    """Terminal-friendly markdown: keep the URL, drop the syntax."""
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} <{m.group(2)}>", markdown)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _wrap(text: str, width: int = 88, indent: str = "") -> str:
    import textwrap

    out = []
    for block in text.split("\n"):
        if not block.strip():
            out.append("")
        else:
            out.extend(textwrap.wrap(block, width=width, initial_indent=indent, subsequent_indent=indent) or [""])
    return "\n".join(out)


def _make_reporter(verbose: bool):
    step = {"n": 0}

    def report(event: dict) -> None:
        kind = event.get("type")
        if kind == "status":
            print(c(f"  · {event['message']}", GREY))
        elif kind == "thinking" and verbose:
            first = event["text"].strip().split("\n")[0][:100]
            print(c(f"    ~ {first}…", GREY))
        elif kind == "reasoning" and verbose:
            print(c(_wrap(event["text"].strip(), indent="    "), GREY))
        elif kind == "tool_call":
            step["n"] += 1
            args = describe_arguments(event.get("arguments") or {})
            index = c("%02d" % step["n"], DIM)
            print(f"  {index} {c(event['name'], AMBER)} {c(args, GREY)}")
        elif kind == "tool_result":
            marker = c("✗", RED) if event.get("error") else c("✓", GREEN)
            first = (event.get("preview") or "").strip().split("\n")[0][:96]
            print(f"     {marker} {c(first, GREY)}")
        elif kind == "error":
            print(c(f"  ! {event['message']}", RED))

    return report


def _print_findings(investigation: Investigation) -> None:
    findings = investigation.findings
    if findings is None:
        print(c("\nNo findings produced. " + (investigation.error or ""), RED))
        return

    print()
    print(c("─" * 88, GREY))
    print(c("  WHAT THE RECORD SAYS", BOLD))
    print(c("─" * 88, GREY))
    print()
    print(_wrap(_plain(findings.answer), indent="  "))

    if findings.timeline:
        print()
        print(c("─" * 88, GREY))
        print(c("  HOW IT GOT THIS WAY", BOLD))
        print(c("─" * 88, GREY))
        print()
        for event in findings.timeline:
            head = f"  {c(event.date or '????-??-??', BLUE)}  {c(event.kind.upper(), DIM)}  {c(event.ref, AMBER)}"
            if event.actor:
                head += c(f"  @{event.actor}", GREY)
            print(head)
            print(_wrap(event.title, indent="      "))
            if event.why_it_matters:
                print(c(_wrap(event.why_it_matters, indent="      "), GREY))
            if event.url:
                print(c(f"      {event.url}", GREY))
            print()

    if findings.open_questions:
        print(c("  STILL UNRESOLVED", BOLD))
        for question in findings.open_questions:
            print(_wrap(f"- {question}", indent="  "))
        print()

    badge = {"high": GREEN, "medium": AMBER, "low": RED}.get(findings.confidence, GREY)
    summary = f"  confidence: {c(findings.confidence, badge)}   evidence lookups: {len(investigation.trace)}"
    if findings.support == "unsupported":
        summary += c("   UNSUPPORTED: cites no retrieved evidence", RED)
    elif findings.unverified:
        summary += c(f"   unverified citations: {len(findings.unverified)}", RED)
    else:
        summary += c(f"   all citations verified ({len(findings.verified_citations)})", GREEN)
    print(summary)
    for item in findings.unverified:
        print(c(f"    ? {item}  (never returned by a tool)", RED))


def cmd_ask(args: argparse.Namespace) -> int:
    if args.model:
        CONFIG.model = args.model
    if args.max_turns:
        CONFIG.max_turns = args.max_turns
    if args.effort:
        CONFIG.effort = args.effort
    if args.no_thinking:
        CONFIG.thinking_mode = "off"

    print()
    print(c(f"  Repo Archaeologist", BOLD) + c(f"  ·  {args.repo}", GREY))
    print(c(f"  {args.question}", AMBER))
    if args.focus:
        print(c(f"  focus: {args.focus}", GREY))
    print()

    investigation = run_investigation(
        question=args.question,
        repo_target=args.repo,
        focus=args.focus or "",
        on_event=_make_reporter(args.verbose),
        refresh=args.refresh,
    )

    _print_findings(investigation)

    if investigation.findings is None and investigation.error:
        return 1

    CONFIG.ensure_dirs()
    slug = re.sub(r"[^a-z0-9]+", "-", investigation.repo.lower()).strip("-")
    out_path = Path(args.out) if args.out else CONFIG.run_dir / f"{slug}-{timestamp_slug()}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(standalone_html(investigation), encoding="utf-8")
    print()
    print(c(f"  report: {out_path}", GREY))

    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(investigation.to_json(), encoding="utf-8")
        print(c(f"  json:   {json_path}", GREY))

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())
    print()
    if args.strict and investigation.findings and investigation.findings.support != "supported":
        return 2
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archaeo",
        description="Reconstruct why code is the way it is, from git history and GitHub discussion.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="Investigate a question about a repository")
    ask.add_argument("repo", help="GitHub URL, owner/repo slug, or path to a local clone")
    ask.add_argument("question", help='e.g. "why does the retry count default to 3?"')
    ask.add_argument("--focus", "-f", default="", help="Point the agent at a file or region, e.g. 'src/pool.py:120-160'")
    ask.add_argument("--out", "-o", default="", help="Where to write the HTML report")
    ask.add_argument("--json", default="", help="Also write the raw investigation record as JSON")
    ask.add_argument("--open", action="store_true", help="Open the report in a browser when done")
    ask.add_argument("--verbose", "-v", action="store_true", help="Show the agent's reasoning as it works")
    ask.add_argument("--refresh", action="store_true", help="Fetch the latest history before investigating")
    ask.add_argument("--model", default="", help=f"Override the model (default {CONFIG.model})")
    ask.add_argument("--max-turns", type=int, default=0, help="Cap the investigation loop")
    ask.add_argument(
        "--effort",
        default="",
        choices=["low", "medium", "high"],
        help=f"How hard the model thinks on newer models (default {CONFIG.effort})",
    )
    ask.add_argument("--no-thinking", action="store_true", help="Disable thinking entirely")
    ask.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 2 unless every citation in the answer was verified against retrieved evidence",
    )
    ask.set_defaults(func=cmd_ask)

    serve = sub.add_parser("serve", help="Run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="Do not open a browser automatically")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(c("\n  interrupted", GREY))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
