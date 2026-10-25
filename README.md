DevOps Repository Auditing Agent
An AI agent that inspects a public GitHub repository for DevOps best-practice violations. It clones the repo, investigates it with an LLM that chooses its own tool calls, and writes a JSON report with evidence and remediation. Submitted for Assignment 2 (MLOps & Production AI Engineering).

No agent frameworks are used. The loop, tool dispatch, and budgets are all plain Python. The only external dependency is requests, which is used directly for HTTP calls.

Setup
Requires Python 3.10+ and git on PATH.

bash
git clone <this repo>
cd <this repo>
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in LLM_API_KEY
.env variables (values in the real environment are also read):

variable	meaning
LLM_PROVIDER	anthropic (default) or openai. The openai path works with any OpenAI-compatible server
LLM_API_KEY	API key. ANTHROPIC_API_KEY and OPENAI_API_KEY are accepted as fallbacks
LLM_MODEL	model name. Defaults to claude-sonnet-4-5 for Anthropic and gpt-4o for OpenAI
LLM_BASE_URL	optional. Required for Groq, OpenRouter, Ollama, and other non-default endpoints
LLM_TEMPERATURE	optional. Left unset by default because some models reject it
Provider matrix
If you are not using Anthropic, change both LLM_PROVIDER and LLM_BASE_URL:

provider	LLM_PROVIDER	LLM_BASE_URL	example LLM_MODEL
Anthropic (default)	anthropic	(empty)	claude-sonnet-4-5
OpenAI	openai	(empty)	gpt-4o
OpenRouter	openai	https://openrouter.ai/api/v1	meta-llama/llama-3.3-70b-instruct:free
Groq	openai	https://api.groq.com/openai/v1	openai/gpt-oss-20b
Ollama (local)	openai	http://localhost:11434/v1	llama3.2
Free models on OpenRouter and Groq have a daily request limit. Auditing a mid-sized repository like pallets/flask uses roughly 20 to 30 calls, so a single run fits within the free quota if you start fresh.

Running
bash
python agent.py --repo https://github.com/owner/name --output report.json
The agent runs unattended. Progress goes to stderr; -q silences it. Two files are written on success:

report.json — the output contract required by the assignment

report.trace.jsonl — every tool call the model made, with its stated reason and a short result preview

Other flags:

--max-steps — tool-call budget (default 40)

--time-budget — wall-clock seconds (default 600)

--trace PATH — override the trace file location

--local DIR — audit an existing checkout instead of cloning (used by tests)

Exit codes:

code	meaning
0	success
1	unexpected error
2	bad input or invalid repository URL
3	missing or invalid LLM configuration
4	clone failed
5	LLM API failed after retries
6	the model could not complete the audit
On any non-zero exit, no report is written.

Architecture
text
agent.py     CLI, wiring, exit codes
repo.py      URL validation and hardened shallow clone into a temp directory
auditor.py   the agent loop
prompts.py   system prompt, kickoff, review, and budget messages
llm.py       Anthropic and OpenAI-compatible HTTP clients with retry
tools.py     inspection tools and secret redaction
report.py    validates and normalises findings, writes the JSON
The loop
The model receives the system prompt (categories to cover, evidence rules, tool list) and the repository URL.

Each reply is one JSON object: either a tool call with a stated reason, or finish with the list of findings.

The loop runs the tool and returns its output as the next message. The model reads it and decides what to look at next. Nothing in the code fixes the order of checks.

Before the first finish is accepted, the model is asked to review its own draft as a skeptic: is each finding backed by output I actually saw, is there an equivalent solution that makes it a false positive, is it a duplicate of another finding. It may make more tool calls during this pass. This is the main lever for precision.

Budgets: step count and wall-clock time. When one runs out, the model is told to produce the final report immediately.

Bad JSON gets a few retries. Repeated identical tool calls are not re-run. Old tool output is trimmed when the conversation grows large.

A JSON-action protocol is used rather than each vendor's native tool-calling format, so the same loop works with any provider and the trace is easy to read.

Tools
Required four: list_files, read_file, search_code, git_log.

Extras:

git_status — also tells the model the default branch, which matters for judging workflow triggers

tracked_but_ignored — lists files that are committed even though the repo's own .gitignore matches them

parse_workflow — structured view of one GitHub Actions file, plus hints such as || true, continue-on-error, mutable action refs, pull_request_target with PR-head checkout, and shell injection patterns

run_ruff — static lint over Python files. Reports "unavailable" if ruff is not installed rather than guessing

There is no run_pytest by design. Executing a stranger's tests means executing a stranger's code, and a plain subprocess is not adequate isolation. The model is told that it cannot observe test results and must not claim test failures.

Report checks
The model's findings are not written out verbatim. In report.py:

A finding is dropped if its file does not exist in the checkout, unless the finding is explicitly about something being absent

Line numbers outside the file become null

Unknown categories are dropped

Severities are normalised to critical, high, medium, or low

Near-duplicates merge

Secret patterns are scrubbed from every text field

rule_ids are assigned per category (SEC-001, CI-002, and so on)

The final report is validated against the output contract before it is written.

Safety
Clone: only https://github.com/<owner>/<repo> URLs are accepted. Argument injection is blocked, core.hooksPath=/dev/null and core.symlinks=false are set, submodules and credentials are disabled, and the clone has a timeout.

Tools: read-only. Paths are resolved and must stay inside the checkout. .git internals are blocked. Git commands run with GIT_OPTIONAL_LOCKS=0, so even git status does not touch the index. A test hashes every file before and after all tools run.

Untrusted content: the prompt declares repository content untrusted, which covers prompt-injection attempts in READMEs, comments, and config files.

Secrets: secret-looking values are masked before the model sees them, in logs, in the trace, and in the report. Masked values appear as [REDACTED], which still tells the model that a literal value was present.

Cleanup: the cloned repository lives in a temporary directory that is deleted on exit.

Tests
bash
pip install -r requirements-dev.txt
pytest
The tests build throwaway Git repositories and drive the agent with a scripted fake LLM, so they need no network access and no API key. Coverage includes every tool, redaction, path escapes, the loop (tool results reaching the model, bad JSON, duplicate calls, budget exhaustion, review pass), report normalisation, and CLI exit codes.

Limitations
Quality depends on the model. Weaker models need a higher --max-steps.

The clone is shallow (300 commits), so very old history is not visible. git_log says when this applies.

Remote settings such as branch protection, repository secrets, and required reviewers cannot be seen from files and are never reported on.

Very large repositories are inspected through collapsed directory listings and targeted searches, not read in full.

sample_report.json
sample_report.json in this repository is the output of:

bash
python agent.py --repo https://github.com/pallets/flask --output sample_report.json