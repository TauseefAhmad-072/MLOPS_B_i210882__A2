# DevOps repository auditing agent

Give it a public GitHub URL, it clones the repo, investigates it with an LLM that
chooses its own tool calls, and writes a JSON report of DevOps best-practice
violations with evidence and fixes. Built for Assignment 2 (MLOps & Production AI
Engineering). No agent frameworks: the loop, tool dispatch and budgets are plain Python.

## Setup

Needs Python 3.10+ and `git` on the PATH.

```bash
git clone <this repo> && cd <this repo>
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then fill in LLM_API_KEY
```

`.env` variables (also read from the real environment):

| variable | meaning |
|---|---|
| `LLM_PROVIDER` | `anthropic` (default) or `openai`. `openai` works with any OpenAI-compatible server |
| `LLM_API_KEY` | the key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are accepted as fallbacks) |
| `LLM_MODEL` | model name. Defaults to `claude-sonnet-5-5` / `gpt-4o`, set it to whatever you have access to |
| `LLM_BASE_URL` | optional, for Groq, OpenRouter, Ollama etc. |
| `LLM_TEMPERATURE` | optional, left unset by default because some models reject it |

## Running

```bash
python agent.py --repo https://github.com/owner/name --output report.json
```

Runs unattended. Progress goes to stderr (`-q` silences it). Two files are written:
`report.json` (the contract from the assignment) and `report.trace.jsonl`
(every tool call the model made, with its stated reason and a short result preview).

Other flags: `--max-steps` (tool-call budget, default 40), `--time-budget` (seconds, default 600),
`--trace PATH`, and `--local DIR` which audits an existing checkout instead of cloning
(used by the tests).

Exit codes: `0` ok, `1` unexpected, `2` bad input/URL, `3` missing/invalid LLM config,
`4` clone failed, `5` LLM API failed after retries, `6` the model could not complete the audit.
On any nonzero exit no report is written.

## How it works

```
agent.py     CLI, wiring, exit codes
repo.py      URL validation + hardened shallow clone into a temp dir
auditor.py   the agent loop
prompts.py   system prompt, kickoff, review and budget messages
llm.py       Anthropic / OpenAI-compatible HTTP clients with retry
tools.py     the inspection tools + secret redaction
report.py    checks and normalises findings, writes the JSON
```

The loop (`auditor.py`):

1. The model gets the system prompt (areas to cover, evidence rules, tool list) and the repo URL.
2. Each reply is one JSON action: either a tool call with a stated reason, or `finish`.
3. The loop runs the tool and hands the output back as the next message. The model reads it
   and decides what to look at next. Nothing in the code fixes the order of checks.
4. Before the first `finish` is accepted the model is asked to review its own draft as a skeptic
   (is it backed by output I saw, is there an equivalent solution, is it a duplicate...) and may
   make more tool calls while doing so. This is the main precision lever.
5. Budgets: step count and wall-clock. When one runs out the model is told to report now.
   Bad JSON gets a few retries, repeated identical tool calls are not re-run, old tool output is
   trimmed when the conversation gets large.

I used a JSON-action protocol rather than each vendor's native tool-calling so the same loop works
with any provider and the trace is easy to read.

### Tools

`list_files`, `read_file`, `search_code`, `git_log` are the required four. Extras:
`git_status` (also tells the model the default branch, useful for judging workflow triggers),
`tracked_but_ignored` (committed files the repo's own `.gitignore` matches),
`parse_workflow` (structured view of a GitHub Actions file plus hints such as `|| true`,
`continue-on-error`, mutable action refs, `pull_request_target` + PR checkout, script injection),
and `run_ruff` (static lint, reports "unavailable" if ruff is missing rather than guessing).

There is deliberately no `run_pytest`. Running a stranger's tests means running a stranger's code,
and the assignment asks for isolation I would not trust a plain subprocess to give. The model is
told it cannot observe test results and must not claim failures.

### Report checks (`report.py`)

The model's findings are not written out as-is. A finding is dropped if the file it names does not
exist (unless the finding is about something being missing), line numbers outside the file become
`null`, unknown categories are dropped, severities are normalised, near-duplicates merge, secret
patterns are scrubbed from all text, and `rule_id`s are assigned per category (`SEC-001`, `CI-002`...).
The result is validated against the output contract before it is written.

## Safety

- Clone: https GitHub URLs only, argument-injection-proof, `core.hooksPath=/dev/null`,
  `core.symlinks=false`, no submodules, no credentials, no prompts, timeout.
- Tools only read. Paths are resolved and must stay inside the checkout; `.git` internals are blocked;
  git runs with `GIT_OPTIONAL_LOCKS=0` so even `git status` does not touch the index.
  A test hashes every file before and after all tools run.
- Repo contents are declared untrusted in the prompt (prompt-injection in READMEs or comments).
- Secret-looking values are masked before the model sees them, in logs, in the trace and in the report.
  Masked values show as `[REDACTED]`, which still tells the model a literal was there.
- The clone lives in a temp dir that is deleted on exit.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests build throwaway git repos and drive the agent with a scripted fake LLM, so they need no
network or API key. They cover every tool, redaction, path escapes, the loop (tool results reach the
model, bad JSON, duplicates, budget, review pass), report normalisation, and CLI exit codes.

## Limits

- Quality depends on the model you plug in. Weaker models need a higher `--max-steps`.
- Shallow clone (300 commits), so very old history is not visible; `git_log` says when this applies.
- Remote settings (branch protection, repo secrets, required reviews) cannot be seen from files
  and are never reported on.
- Very large repositories are inspected through collapsed listings and targeted searches, not read in full.

## sample_report.json

Generate it with a real model against any public repo and commit the result:

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output sample_report.json
```
