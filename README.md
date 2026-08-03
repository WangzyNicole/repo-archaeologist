# Repo Archaeologist

Point it at a repository, ask *why the code is the way it is*, and get the reasoning back — not a summary of the diff.

```
archaeo ask pallets/click "why does Click use a Context object instead of passing parameters directly?"
```

Every engineer has hit a line of code that makes no sense, run `git blame`, landed on `reformat with black`, and given up. The reasoning is usually in the repository — spread across a commit message, the PR that merged it, a reviewer's objection, and the issue that started it. It just isn't anywhere you can read in one place.

This agent goes and reads all of it.

---

## What it actually does

Given a question, it runs an open-ended investigation using thirteen evidence tools, deciding for itself where to dig:

| | |
|---|---|
| **Code** | `grep_repo`, `read_file` (at any historical revision), `list_dir` |
| **History** | `git_blame`, `git_log_for_path`, `git_show`, `git_search_commits` (pickaxe), `git_releases_containing`, `repo_overview` |
| **Discussion** | `github_pr_for_commit`, `github_pull_request` (including inline review threads), `github_issue`, `github_search` |

A typical run looks like: grep for the symbol → read it → blame it → notice blame landed on a lint sweep → re-blame at `<sha>^` → find the real commit → find its PR → read the review thread where someone asked "why not just use a lock?" → follow `Fixes #331` to the issue with the original bug report → answer.

It returns a narrative answer with the supporting timeline underneath, as a terminal report, a standalone HTML file, and JSON.

## Every claim is checked against retrieved evidence

The failure mode for a tool like this is a fluent answer citing a PR number that doesn't exist. So citations aren't taken on trust:

- Every tool records the canonical URL of everything it surfaces into an **evidence ledger**.
- The agent is instructed to cite only URLs that appeared verbatim in tool output.
- After it submits, **every link in the answer is checked against the ledger.** Anything it never actually retrieved is flagged red in the report and listed in a banner at the top.

A run with an unverified citation tells you something is wrong. A run with none means every link in the narrative came back from a real lookup. The report also ships the full evidence trail — all tool calls, arguments and raw output — so any claim can be traced to the lookup that produced it.

## Install

```bash
git clone <your-fork> && cd repo-archaeologist
pip install -r requirements.txt
cp .env.example .env      # add ANTHROPIC_API_KEY, and ideally GITHUB_TOKEN
```

`GITHUB_TOKEN` is optional but strongly recommended: unauthenticated GitHub allows 60 requests/hour, a token allows 5000. A classic token with no scopes is enough for public repos.

## Use

```bash
# ask about any public repo
archaeo ask psf/requests "why is there a retry with backoff in the adapter?"

# point it at a specific region
archaeo ask ./my-project "why is this guarded twice?" --focus src/pool.py:120-160

# open the HTML report when it finishes
archaeo ask django/django "why does the ORM defer this query?" --open

# watch it think
archaeo ask pallets/flask "why is the app context a stack?" -v
```

Works on local clones too — pass a path instead of a slug. Without a GitHub remote it runs on git history alone and says so.

### Web UI

```bash
archaeo serve
```

Opens `http://127.0.0.1:8765`. You watch the dig happen live over server-sent events — each blame, each PR fetch, each issue — and the narrative appears when it's done, with links to the standalone report and raw JSON. No web framework: it's `http.server` plus 60 lines of JavaScript.

## How it's built

```
archaeo/
  agent.py         the investigation loop and the archaeologist system prompt
  tools/
    git_tools.py     blame, log, show, pickaxe search, release lookup
    github_tools.py  PRs, review threads, issues, search — cached on disk
    code_tools.py    read, grep, list (sandboxed to the repo)
    __init__.py      tool schemas + dispatcher
  evidence.py      the ledger and citation verification
  models.py        Findings, TimelineEvent, ToolCall, Investigation
  ui.py            shared renderer for the report and the web UI
  markdown_lite.py dependency-free markdown, so links can be annotated
  web.py           stdlib SSE server
  cli.py           argparse CLI
```

Design decisions worth knowing about:

**The method is in the prompt, not in a hardcoded pipeline.** A fixed grep → blame → PR chain works on the easy half of questions and falls apart on the rest. The agent gets the tools plus what an experienced archaeologist knows — that blame's first answer is usually reformatting, that odd code is usually a scar from an incident, that the rejected alternative in a review thread is often the actual reasoning, that reverts are strong evidence — and it chooses its own path.

**Blame is treated as unreliable by default.** `git_blame` passes `-w` so whitespace-only changes don't claim authorship, exposes `rev` so the agent can re-blame at `<sha>^` to step past a mass rename or lint sweep, and offers `-C -C` to follow code moved between files. The prompt tells it explicitly to keep stepping back until it lands on a commit that was actually about the behaviour.

**Tool errors are answers, not exceptions.** A bad sha, a rate limit, a missing path — all come back as `ERROR: ...` strings the model can read and route around. Nothing in the tool layer can crash a run.

**GitHub responses are cached on disk for a week.** Historical discussion doesn't change; this saves the rate limit and makes a repeat demo instant.

**Context is defended.** Every tool result is truncated before it reaches the model, with a hint about how to narrow the query. Bodies, comment counts and patches all have caps.

## Tests

Two suites, neither of which needs an API key:

```bash
python smoke_test.py            # all tools against a real clone of pallets/click
python agent_loop_test.py       # the loop, driven by a scripted fake model
```

`smoke_test.py` clones a real repo and exercises every tool against real history, plus path-traversal refusal, ledger verification, markdown escaping and report generation. `agent_loop_test.py` drives the loop with a fake client to cover the parts that are expensive to debug live: tool results being fed back correctly, turn exhaustion forcing a final answer, a stalled model not looping forever, tool errors surviving, and a fabricated citation being caught while a genuine one passes.

## Limits

- It reads what was written down. If nobody explained themselves, it will say so and report what it ruled out rather than inventing a reason — check the confidence level and the open questions.
- Squashed history and force-pushed branches lose the trail.
- Repos migrated from another host have PR discussion the agent can't reach.
- Very large repos are slower on first run; the clone is cached afterward.

## Configuration

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required |
| `GITHUB_TOKEN` | — | 60 → 5000 requests/hour |
| `ARCHAEO_MODEL` | `claude-sonnet-5` | |
| `ARCHAEO_MAX_TURNS` | `40` | investigation loop cap |
| `ARCHAEO_THINKING` | `auto` | `auto` \| `adaptive` \| `manual` \| `off` |
| `ARCHAEO_EFFORT` | `high` | `low` \| `medium` \| `high`, used in adaptive mode |
| `ARCHAEO_THINKING_BUDGET` | `3000` | token budget, used in manual mode only |
| `ARCHAEO_CACHE_DIR` | `~/.cache/repo-archaeologist` | clones, HTTP cache, reports |

Thinking configuration is negotiated at runtime rather than pinned. Claude 4.7 and later take `thinking: {type: "adaptive"}` with `output_config.effort` and reject a manual `budget_tokens`; Claude 4.5 and earlier are the reverse. On `auto` the agent opens with adaptive, and if the model returns a 400 saying it's unsupported it steps down to a manual budget, then to no thinking, reporting each step. Swapping `ARCHAEO_MODEL` across generations needs no other change.

MIT.
