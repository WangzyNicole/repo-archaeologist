"""A dependency-free web UI.

Standard-library HTTP server plus server-sent events, so the whole thing runs
with `python -m archaeo serve` and no extra install. The point of the UI is to
show the dig happening: you watch the agent blame a line, jump to the PR, and
follow a linked issue, and only then does the narrative appear. Watching the
evidence accumulate is what makes the answer believable.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent import describe_arguments, run_investigation
from .models import Investigation
from .ui import STYLE, render_findings, standalone_html

JOBS: dict[str, dict[str, Any]] = {}

EXTRA_CSS = """
.shell { max-width: 900px; margin: 0 auto; padding: 40px 24px 96px; }
.brand { font-family: var(--mono); font-size: 11px; letter-spacing: .18em; text-transform: uppercase; color: var(--accent); margin: 0 0 8px; }
.tagline { color: var(--muted); margin: 0 0 28px; font-size: 15px; }
form { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 20px; }
label { display: block; font-family: var(--mono); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
input[type=text] { width: 100%; padding: 11px 13px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: 15px/1.4 inherit; margin-bottom: 16px; }
input[type=text]:focus { outline: none; border-color: var(--accent-dim); }
.row { display: flex; gap: 14px; flex-wrap: wrap; }
.row > div { flex: 1 1 240px; }
button { background: var(--accent); color: #17140c; border: 0; border-radius: 7px; padding: 11px 22px;
  font-weight: 650; font-size: 15px; cursor: pointer; }
button:disabled { opacity: .45; cursor: default; }
.samples { margin: 14px 0 0; font-size: 13px; color: var(--muted); }
.samples button { background: none; color: var(--muted); border: 1px solid var(--line); padding: 5px 10px;
  border-radius: 999px; font-size: 12px; font-weight: 400; margin: 4px 6px 0 0; font-family: var(--mono); }
.samples button:hover { color: var(--ink); border-color: var(--accent-dim); }
#log { margin-top: 24px; font-family: var(--mono); font-size: 12.5px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--panel); padding: 14px 16px; max-height: 380px; overflow-y: auto; display: none; }
#log.on { display: block; }
.line { padding: 3px 0; color: var(--muted); display: flex; gap: 8px; }
.line .tname { color: var(--accent); }
.line .targs { color: var(--muted); opacity: .8; word-break: break-all; }
.line.res { padding-left: 18px; opacity: .75; }
.line.res.err { color: var(--danger); }
.line.status { color: var(--muted); font-style: italic; }
.spin { display: inline-block; animation: pulse 1.1s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: .35 } 50% { opacity: 1 } }
#result { margin-top: 36px; }
#result:empty { display: none; }
.toolbar { margin-top: 20px; display: none; gap: 12px; }
.toolbar.on { display: flex; }
.toolbar a { font-family: var(--mono); font-size: 12px; color: var(--muted); border: 1px solid var(--line);
  padding: 7px 12px; border-radius: 7px; text-decoration: none; }
.toolbar a:hover { color: var(--ink); }
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Repo Archaeologist</title>
<style>__STYLE__</style>
</head><body>
<main class="shell">
  <p class="brand">Repo Archaeologist</p>
  <p class="tagline">Point it at a repository and ask why the code is the way it is. It reads blame, commits,
  pull request review threads and linked issues, then explains the reasoning — with every claim traceable.</p>

  <form id="f">
    <div class="row">
      <div>
        <label for="repo">Repository</label>
        <input type="text" id="repo" placeholder="owner/repo, a GitHub URL, or a local path" required>
      </div>
      <div>
        <label for="focus">Focus (optional)</label>
        <input type="text" id="focus" placeholder="src/pool.py:120-160">
      </div>
    </div>
    <label for="q">Question</label>
    <input type="text" id="q" placeholder="why is there a 50ms sleep in the connection pool?" required>
    <button id="go" type="submit">Start digging</button>
    <div class="samples">
      Try:
      <button type="button" class="sample" data-repo="pallets/click" data-q="why does Click use a Context object instead of passing parameters directly?"></button>
      <button type="button" class="sample" data-repo="psf/requests" data-q="why does requests not support async?"></button>
      <button type="button" class="sample" data-repo="python/cpython" data-q="why is the GIL still there?"></button>
    </div>
  </form>

  <div id="log"></div>
  <div class="toolbar" id="bar"><a id="dl" href="#" target="_blank">Open standalone report</a><a id="raw" href="#" target="_blank">Raw JSON</a></div>
  <div id="result"></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
document.querySelectorAll('.sample').forEach(b => {
  b.textContent = b.dataset.repo;
  b.onclick = () => { $('repo').value = b.dataset.repo; $('q').value = b.dataset.q; };
});

function line(cls, html) {
  const el = document.createElement('div');
  el.className = 'line ' + cls;
  el.innerHTML = html;
  $('log').appendChild(el);
  $('log').scrollTop = $('log').scrollHeight;
  return el;
}
const esc = (s) => (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

$('f').onsubmit = async (e) => {
  e.preventDefault();
  $('go').disabled = true;
  $('result').innerHTML = '';
  $('log').innerHTML = '';
  $('log').classList.add('on');
  $('bar').classList.remove('on');
  let n = 0;

  const body = { repo: $('repo').value, question: $('q').value, focus: $('focus').value };
  const res = await fetch('/api/run', { method: 'POST', body: JSON.stringify(body) });
  const { job } = await res.json();

  const src = new EventSource('/api/events?job=' + job);
  const pending = line('status', '<span class="spin">working…</span>');

  src.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === 'status') {
      $('log').insertBefore(Object.assign(document.createElement('div'),
        { className: 'line status', textContent: ev.message }), pending);
    } else if (ev.type === 'tool_call') {
      n += 1;
      const el = document.createElement('div');
      el.className = 'line';
      el.innerHTML = '<span>' + String(n).padStart(2, '0') + '</span><span class="tname">' +
        esc(ev.name) + '</span><span class="targs">' + esc(ev.args) + '</span>';
      $('log').insertBefore(el, pending);
    } else if (ev.type === 'tool_result') {
      const el = document.createElement('div');
      el.className = 'line res' + (ev.error ? ' err' : '');
      el.textContent = (ev.error ? '✗ ' : '✓ ') + (ev.preview || '').split('\\n')[0].slice(0, 120);
      $('log').insertBefore(el, pending);
    } else if (ev.type === 'render') {
      pending.remove();
      $('result').innerHTML = ev.html;
      $('dl').href = '/api/report?job=' + job;
      $('raw').href = '/api/json?job=' + job;
      $('bar').classList.add('on');
      $('go').disabled = false;
      src.close();
      $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (ev.type === 'error') {
      const el = document.createElement('div');
      el.className = 'line res err';
      el.textContent = '! ' + ev.message;
      $('log').insertBefore(el, pending);
    }
    $('log').scrollTop = $('log').scrollHeight;
  };
  src.onerror = () => { pending.remove(); $('go').disabled = false; src.close(); };
};
</script>
</body></html>"""


def _start_job(payload: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    events: queue.Queue = queue.Queue()
    JOBS[job_id] = {"queue": events, "investigation": None}

    def worker() -> None:
        def emit(event: dict) -> None:
            if event.get("type") == "tool_call":
                event = {"type": "tool_call", "name": event["name"], "args": describe_arguments(event.get("arguments") or {})}
            if event.get("type") in {"thinking", "reasoning", "done"}:
                return
            events.put(event)

        investigation = run_investigation(
            question=payload.get("question", ""),
            repo_target=payload.get("repo", ""),
            focus=payload.get("focus", ""),
            on_event=emit,
        )
        JOBS[job_id]["investigation"] = investigation
        events.put({"type": "render", "html": render_findings(investigation)})
        events.put({"type": "__end__"})

    threading.Thread(target=worker, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep the console clean
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/":
            page = PAGE.replace("__STYLE__", STYLE + EXTRA_CSS)
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/events":
            self._stream(params.get("job", [""])[0])
            return

        job = JOBS.get(params.get("job", [""])[0])
        investigation: Investigation | None = job.get("investigation") if job else None
        if parsed.path == "/api/report" and investigation:
            self._send(200, standalone_html(investigation).encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/json" and investigation:
            self._send(200, investigation.to_json().encode(), "application/json; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        job_id = _start_job(payload)
        self._send(200, json.dumps({"job": job_id}).encode(), "application/json")

    def _stream(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self._send(404, b"unknown job", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        events: queue.Queue = job["queue"]
        while True:
            try:
                event = events.get(timeout=20)
            except queue.Empty:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                continue
            if event.get("type") == "__end__":
                return
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"\n  Repo Archaeologist — {url}\n  ctrl-c to stop\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
    finally:
        server.server_close()
