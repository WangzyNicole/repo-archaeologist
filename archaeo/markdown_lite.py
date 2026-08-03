"""A small, dependency-free markdown renderer.

Deliberately hand-rolled rather than pulled from PyPI: every link in the
narrative has to be annotated as verified or unverified against the evidence
ledger, which means we need control over link rendering anyway.
"""

from __future__ import annotations

import html
import re
from typing import Callable

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_BARE_URL = re.compile(r"(?<!href=\")(?<!\">)\bhttps?://[^\s<\)]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*\d+[.)]\s+(.*)$")

Verifier = Callable[[str], bool]


def _link_html(text: str, url: str, verifier: Verifier | None) -> str:
    safe_url = html.escape(url, quote=True)
    label = html.escape(text)
    if verifier is None or verifier(url):
        return f'<a href="{safe_url}" target="_blank" rel="noopener" class="cite">{label}</a>'
    return (
        f'<a href="{safe_url}" target="_blank" rel="noopener" class="cite cite-unverified" '
        f'title="This link was not returned by any tool during the investigation — treat it as unverified.">'
        f'{label}<span class="cite-flag">?</span></a>'
    )


def _inline(text: str, verifier: Verifier | None) -> str:
    placeholders: list[str] = []

    def stash(markup: str) -> str:
        placeholders.append(markup)
        return f"\x00{len(placeholders) - 1}\x00"

    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(lambda m: stash(_link_html(m.group(1), m.group(2), verifier)), text)
    text = _BARE_URL.sub(lambda m: stash(_link_html(m.group(0), m.group(0), verifier)), text)

    text = html.escape(text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    for index, markup in enumerate(placeholders):
        text = text.replace(f"\x00{index}\x00", markup)
    return text


def to_html(markdown: str, verifier: Verifier | None = None) -> str:
    """Render a markdown subset: headings, lists, quotes, code, emphasis, links."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_stack: list[str] = []
    paragraph: list[str] = []
    in_code = False
    code_buffer: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph).strip(), verifier)}</p>")
            paragraph.clear()

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    for raw in lines:
        line = raw.rstrip()

        if line.strip().startswith("```"):
            if in_code:
                out.append(f"<pre><code>{html.escape(chr(10).join(code_buffer))}</code></pre>")
                code_buffer.clear()
                in_code = False
            else:
                flush_paragraph()
                close_lists()
                in_code = True
            continue
        if in_code:
            code_buffer.append(raw)
            continue

        if not line.strip():
            flush_paragraph()
            close_lists()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            close_lists()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2), verifier)}</h{level}>")
            continue

        if line.strip().startswith(">"):
            flush_paragraph()
            close_lists()
            out.append(f"<blockquote>{_inline(line.strip().lstrip('>').strip(), verifier)}</blockquote>")
            continue

        if set(line.strip()) in ({"-"}, {"_"}, {"*"}) and len(line.strip()) >= 3:
            flush_paragraph()
            close_lists()
            out.append("<hr>")
            continue

        unordered = _UL.match(line)
        ordered = _OL.match(line)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            if not list_stack or list_stack[-1] != tag:
                close_lists()
                out.append(f"<{tag}>")
                list_stack.append(tag)
            content = (unordered or ordered).group(1)
            out.append(f"<li>{_inline(content, verifier)}</li>")
            continue

        close_lists()
        paragraph.append(line.strip())

    if in_code and code_buffer:
        out.append(f"<pre><code>{html.escape(chr(10).join(code_buffer))}</code></pre>")
    flush_paragraph()
    close_lists()
    return "\n".join(out)
