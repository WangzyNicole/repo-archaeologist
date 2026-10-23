"""Shared presentation layer for the CLI report and the web UI."""

from __future__ import annotations

import html
import json
from datetime import datetime

from .models import Investigation
from .markdown_lite import to_html

STYLE = """
:root {
  --bg: #10100e;
  --panel: #17171a;
  --panel-2: #1d1d21;
  --line: #2c2c31;
  --ink: #e8e6e1;
  --muted: #9a968d;
  --accent: #d9a441;
  --accent-dim: #7a5f26;
  --commit: #7aa2f7;
  --pr: #9ece6a;
  --issue: #e0af68;
  --danger: #f7768e;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 900px; margin: 0 auto; padding: 40px 24px 96px; }
a { color: var(--accent); }
code { font-family: var(--mono); font-size: 0.88em; background: var(--panel-2); padding: 1px 5px; border-radius: 4px; }
pre { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 14px; overflow-x: auto; }
pre code { background: none; padding: 0; }
hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }
blockquote { margin: 12px 0; padding: 8px 16px; border-left: 3px solid var(--accent-dim); color: var(--muted); }

header.masthead { border-bottom: 1px solid var(--line); padding-bottom: 22px; margin-bottom: 30px; }
.eyebrow { font-family: var(--mono); font-size: 11px; letter-spacing: .18em; text-transform: uppercase; color: var(--accent); margin: 0 0 12px; }
h1.question { font-size: 25px; line-height: 1.35; margin: 0 0 14px; font-weight: 600; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 12px; color: var(--muted); font-family: var(--mono); }
.chip { border: 1px solid var(--line); background: var(--panel); border-radius: 999px; padding: 3px 10px; }
.chip.high { color: #9ece6a; border-color: #38502c; }
.chip.medium { color: var(--issue); border-color: #5a4520; }
.chip.low { color: var(--danger); border-color: #5c2c35; }
.chip.warn { color: var(--danger); border-color: #5c2c35; }

h2.section { font-size: 12px; font-family: var(--mono); letter-spacing: .16em; text-transform: uppercase;
  color: var(--muted); margin: 44px 0 16px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
.answer > *:first-child { margin-top: 0; }
.answer p { margin: 0 0 15px; }
.answer h3, .answer h4 { font-size: 17px; margin: 26px 0 10px; }
.answer li { margin-bottom: 6px; }

a.cite { text-decoration: none; border-bottom: 1px solid var(--accent-dim); font-family: var(--mono); font-size: .9em; }
a.cite:hover { border-bottom-color: var(--accent); }
a.cite-unverified { color: var(--danger); border-bottom-color: var(--danger); }
.cite-flag { font-size: .8em; vertical-align: super; margin-left: 1px; }

.banner { border: 1px solid #5c2c35; background: #1e1416; border-radius: 8px; padding: 12px 16px; margin: 20px 0; font-size: 14px; }
.banner strong { color: var(--danger); }
.banner ul { margin: 8px 0 0; padding-left: 20px; font-family: var(--mono); font-size: 12px; word-break: break-all; }

.timeline { position: relative; margin: 0; padding: 0 0 0 26px; list-style: none; }
.timeline::before { content: ""; position: absolute; left: 5px; top: 6px; bottom: 6px; width: 1px; background: var(--line); }
.timeline li { position: relative; margin-bottom: 26px; }
.timeline li::before { content: ""; position: absolute; left: -25px; top: 7px; width: 9px; height: 9px; border-radius: 50%;
  background: var(--bg); border: 2px solid var(--muted); }
.timeline li.commit::before { border-color: var(--commit); }
.timeline li.pr::before { border-color: var(--pr); }
.timeline li.issue::before { border-color: var(--issue); }
.timeline li.release::before { border-color: var(--accent); }
.tl-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; }
.tl-date { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.tl-kind { font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: .1em; padding: 2px 7px;
  border-radius: 4px; border: 1px solid var(--line); color: var(--muted); }
.tl-kind.commit { color: var(--commit); } .tl-kind.pr { color: var(--pr); } .tl-kind.issue { color: var(--issue); }
.tl-title { font-weight: 600; margin: 4px 0 4px; }
.tl-why { color: var(--muted); font-size: 14.5px; }
.tl-actor { font-family: var(--mono); font-size: 12px; color: var(--muted); }

details.trace { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); margin-top: 12px; }
details.trace > summary { cursor: pointer; padding: 12px 16px; font-family: var(--mono); font-size: 13px; color: var(--muted); }
details.trace > summary:hover { color: var(--ink); }
.step { border-top: 1px solid var(--line); padding: 12px 16px; }
.step-head { font-family: var(--mono); font-size: 13px; display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; }
.step-name { color: var(--accent); }
.step-args { color: var(--muted); word-break: break-word; }
.step-ms { margin-left: auto; color: var(--muted); font-size: 11px; }
.step-out { margin: 8px 0 0; font-family: var(--mono); font-size: 11.5px; line-height: 1.5; color: var(--muted);
  white-space: pre-wrap; max-height: 220px; overflow: auto; background: var(--panel-2); border-radius: 6px; padding: 10px; }
.step.err .step-name { color: var(--danger); }
.empty { color: var(--muted); font-style: italic; }
footer.colophon { margin-top: 56px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; font-family: var(--mono); }
"""


def _esc(text: str) -> str:
    return html.escape(text or "")


def _format_args(arguments: dict) -> str:
    parts = []
    for key, value in (arguments or {}).items():
        rendered = value if isinstance(value, str) else json.dumps(value)
        if len(rendered) > 60:
            rendered = rendered[:60] + "…"
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def render_findings(investigation: Investigation) -> str:
    """The body fragment shared by the standalone report and the web UI."""
    findings = investigation.findings
    if findings is None:
        message = investigation.error or "No findings were produced."
        return f'<div class="banner"><strong>Investigation failed.</strong><br>{_esc(message)}</div>'

    unverified = {u.rstrip("/").split("#")[0] for u in findings.unverified_urls}

    def verifier(url: str) -> bool:
        return url.rstrip("/").split("#")[0] not in unverified

    parts: list[str] = []

    flagged = sorted(unverified) + list(findings.unverified_refs)
    meta = [
        f'<span class="chip {findings.confidence}">confidence: {findings.confidence}</span>',
        f'<span class="chip">{len(investigation.trace)} evidence lookups</span>',
        f'<span class="chip">{len(findings.timeline)} timeline events</span>',
    ]
    if findings.support == "unsupported":
        meta.append('<span class="chip warn">unsupported answer</span>')
    if flagged:
        meta.append(f'<span class="chip warn">{len(flagged)} unverified citation(s)</span>')
    elif findings.support == "supported":
        meta.append(f'<span class="chip">all citations verified ({len(findings.verified_citations)})</span>')

    parts.append(
        f"""<header class="masthead">
  <p class="eyebrow">Repo Archaeologist · {_esc(investigation.repo)}</p>
  <h1 class="question">{_esc(investigation.question)}</h1>
  <div class="meta">{''.join(meta)}</div>
</header>"""
    )

    if findings.support == "unsupported":
        why = (
            "Every source it cites is unverified, so"
            if flagged
            else "It cites no commit, pull request, issue or link at all, so"
        )
        parts.append(
            f'<div class="banner"><strong>Unsupported answer.</strong> Nothing in this answer is backed by evidence '
            f"retrieved during the investigation. {why} treat it as unsourced until checked by hand.</div>"
        )
    if flagged:
        items = "".join(f"<li>{_esc(item)}</li>" for item in flagged)
        parts.append(
            f'<div class="banner"><strong>Unverified citations.</strong> These links and references appear in the '
            f"answer but were never returned by a tool during this investigation, so they are not backed by "
            f"retrieved evidence:<ul>{items}</ul></div>"
        )

    parts.append('<h2 class="section">What the record says</h2>')
    parts.append(f'<div class="answer">{to_html(findings.answer, verifier)}</div>')

    if findings.timeline:
        parts.append('<h2 class="section">How it got this way</h2>')
        events = []
        for event in findings.timeline:
            ref = _esc(event.ref)
            if event.url:
                link_class = "cite" if verifier(event.url) else "cite cite-unverified"
                ref_html = f'<a class="{link_class}" href="{_esc(event.url)}" target="_blank" rel="noopener">{ref}</a>'
            else:
                ref_html = f"<code>{ref}</code>" if ref else ""
            actor = f'<span class="tl-actor">@{_esc(event.actor)}</span>' if event.actor else ""
            events.append(
                f"""<li class="{_esc(event.kind)}">
  <div class="tl-head">
    <span class="tl-date">{_esc(event.date)}</span>
    <span class="tl-kind {_esc(event.kind)}">{_esc(event.kind)}</span>
    {ref_html}{actor}
  </div>
  <div class="tl-title">{_esc(event.title)}</div>
  <div class="tl-why">{_esc(event.why_it_matters)}</div>
</li>"""
            )
        parts.append(f'<ul class="timeline">{"".join(events)}</ul>')

    if findings.open_questions:
        parts.append('<h2 class="section">Still unresolved</h2>')
        items = "".join(f"<li>{_esc(q)}</li>" for q in findings.open_questions)
        parts.append(f"<ul>{items}</ul>")

    parts.append('<h2 class="section">Evidence trail</h2>')
    if investigation.trace:
        steps = []
        for index, call in enumerate(investigation.trace, start=1):
            steps.append(
                f"""<div class="step {'err' if call.error else ''}">
  <div class="step-head">
    <span class="step-index">{index:02d}</span>
    <span class="step-name">{_esc(call.name)}</span>
    <span class="step-args">{_esc(_format_args(call.arguments))}</span>
    <span class="step-ms">{call.duration_ms} ms</span>
  </div>
  <pre class="step-out">{_esc(call.result_preview)}</pre>
</div>"""
            )
        parts.append(
            f'<details class="trace"><summary>{len(investigation.trace)} tool calls — every claim above traces back to one of these</summary>'
            f'{"".join(steps)}</details>'
        )
    else:
        parts.append('<p class="empty">No tools were called.</p>')

    usage = investigation.usage
    tokens = f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens" if usage else ""
    parts.append(
        f'<footer class="colophon">Investigated {_esc(investigation.started_at)} · {_esc(tokens)}</footer>'
    )
    return "\n".join(parts)


def standalone_html(investigation: Investigation) -> str:
    title = f"Why: {investigation.question[:70]}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{STYLE}</style>
</head>
<body><main class="wrap">{render_findings(investigation)}</main></body>
</html>"""


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
