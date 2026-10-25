"""Read-only inspection tools the agent can call against a checked-out repository.

Nothing in here writes to the target repo or runs code from it. Tools return plain
text (or JSON text) and raise ToolError for bad input; the agent loop hands both
back to the model as observations.
"""
import fnmatch
import inspect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

MAX_READ_BYTES = 200_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 12_000
LINE_CLIP = 240


class ToolError(Exception):
    """Bad arguments or an unusable result. The message is shown to the model."""


# --------------------------------------------------------------------------- redaction
# Anything the model sees, and anything we log or write, goes through redact().

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)
_KNOWN_TOKENS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY ID]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "[REDACTED API KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "[REDACTED GOOGLE KEY]"),
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}\b"), "[REDACTED JWT]"),
]
_ASSIGNED_SECRET = re.compile(
    r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credential)\w*\\?[\"']?\s*[:=]\s*)(\\?[\"']?)([^\s\"'#,;\\]{6,})"
)
_REFERENCE_START = (
    "os.", "os[", "process.", "env.", "env[", "getenv", "config.", "settings.", "secrets.",
    "${", "$(", "$", "{{", "<", "%", "System.getenv", "ENV[", "vault", "ssm:",
)
_PLACEHOLDER = re.compile(r"(?i)(change.?me|your[_-]|example|dummy|placeholder|xxxx|todo|\*{3,}|redacted)")
_DOTTED_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def _mask_assignment(m):
    prefix, quote, value = m.groups()
    if value.startswith(_REFERENCE_START) or _PLACEHOLDER.search(value):
        return m.group(0)
    if not quote and ("(" in value or "[" in value or _DOTTED_NAME.match(value)):
        return m.group(0)  # looks like code, e.g. token = get_token()
    return f"{prefix}{quote}[REDACTED]"


def _mask_key_block(m):
    return re.sub(
        r"[^\n]+",
        lambda line: line.group(0) if "-----" in line.group(0) else "[REDACTED PRIVATE KEY]",
        m.group(0),
    )


def redact(text):
    if not text:
        return text
    text = _PRIVATE_KEY.sub(_mask_key_block, text)
    for pattern, repl in _KNOWN_TOKENS:
        text = pattern.sub(repl, text)
    return _ASSIGNED_SECRET.sub(_mask_assignment, text)


# --------------------------------------------------------------------------- helpers

def _as_int(value, default, lo, hi):
    if value is None or value == "":
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"expected an integer, got {value!r}")
    return max(lo, min(hi, value))


def _looks_binary(path):
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(4096)
    except OSError:
        return True


def _clip(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output cut at {limit} characters; narrow the request to see more]"


_TEST_CMD = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox|npm (run )?test|yarn test|pnpm test|jest|mocha|vitest|"
    r"go test|cargo test|mvn\b.*\b(test|verify)|(gradlew?|gradle) .*\btest\b|dotnet test|rspec|"
    r"phpunit|make test|ctest)\b"
)
_LINT_CMD = re.compile(
    r"\b(ruff|flake8|pylint|black|isort|mypy|eslint|prettier|golangci-lint|rubocop|shellcheck|"
    r"hadolint|pre-commit|clippy|cargo fmt|checkstyle|stylelint)\b"
)
_INJECTABLE = re.compile(
    r"\$\{\{[^}]*(github\.head_ref|github\.event\.(issue|pull_request|comment|review|"
    r"review_comment|head_commit|discussion)\.[\w.]*(title|body|message|ref|label))[^}]*\}\}"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


class RepoTools:
    def __init__(self, root, git_timeout=30):
        self.root = Path(root).resolve()
        self.git_timeout = git_timeout
        self._files = None

    # ---- plumbing

    def _git(self, *args):
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null",
             "-c", "core.fsmonitor=false", *args],
            capture_output=True, text=True, errors="replace",
            timeout=self.git_timeout, env=env,
        )

    def _resolve(self, rel):
        rel = (rel or "").strip().replace("\\", "/")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError("path is outside the repository")
        parts = target.relative_to(self.root).parts
        if parts and parts[0] == ".git":
            raise ToolError(".git internals are not available; use git_log or git_status")
        return target

    def tracked_files(self):
        if self._files is None:
            proc = self._git("ls-files", "-z")
            if proc.returncode == 0 and proc.stdout:
                self._files = sorted(p for p in proc.stdout.split("\0") if p)
            else:
                found = []
                for dirpath, dirnames, names in os.walk(self.root):
                    dirnames[:] = [d for d in dirnames if d != ".git"]
                    for n in names:
                        found.append(os.path.relpath(os.path.join(dirpath, n), self.root).replace(os.sep, "/"))
                self._files = sorted(found)
        return self._files

    def has_file(self, rel):
        try:
            return self._resolve(rel).is_file()
        except ToolError:
            return False

    def line_count(self, rel):
        try:
            p = self._resolve(rel)
            if not p.is_file() or _looks_binary(p):
                return 0
            with open(p, "rb") as fh:
                return sum(1 for _ in fh)
        except (ToolError, OSError):
            return 0

    # ---- the tools

    def list_files(self, path="", max_entries=300):
        max_entries = _as_int(max_entries, 300, 20, 1000)
        prefix = (path or "").strip().strip("/").replace("\\", "/")
        files = [f for f in self.tracked_files()
                 if not prefix or f == prefix or f.startswith(prefix + "/")]
        if not files:
            raise ToolError(f"no tracked files under {prefix or '.'!r}")

        def size_of(rel):
            try:
                return (self.root / rel).stat().st_size
            except OSError:
                return -1

        header = f"{len(files)} tracked files under {prefix or '.'}"
        if len(files) <= max_entries:
            return header + "\n" + "\n".join(f"{f}  ({size_of(f)} B)" for f in files)

        # Too many to dump: show this level, collapse sub-directories into counts.
        depth = len(prefix.split("/")) if prefix else 0
        direct, groups = [], {}
        for f in files:
            parts = f.split("/")
            if len(parts) == depth + 1:
                direct.append(f)
            else:
                key = "/".join(parts[: depth + 1])
                groups[key] = groups.get(key, 0) + 1
        out = [header + " (collapsed; call list_files with a directory path to open one)"]
        out += [f"{d}/  [{n} files]" for d, n in sorted(groups.items())]
        out += [f"{f}  ({size_of(f)} B)" for f in direct]
        return "\n".join(out[:max_entries])

    def read_file(self, path, start_line=1, max_lines=400):
        start_line = _as_int(start_line, 1, 1, 10**7)
        max_lines = _as_int(max_lines, 400, 1, 1000)
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"{path}: no such file")
        if not p.is_file():
            raise ToolError(f"{path}: is a directory, use list_files")
        size = p.stat().st_size
        if _looks_binary(p):
            return f"{path}: binary file, {size} bytes, contents not shown"
        with open(p, "rb") as fh:
            raw = fh.read(MAX_READ_BYTES)
        text = redact(raw.decode("utf-8", errors="replace"))
        lines = text.splitlines()
        chunk = lines[start_line - 1: start_line - 1 + max_lines]
        body = "\n".join(f"{i:>5}  {ln[:LINE_CLIP * 2]}" for i, ln in enumerate(chunk, start_line))
        notes = []
        if size > MAX_READ_BYTES:
            notes.append(f"file is {size} bytes, only the first {MAX_READ_BYTES} were read")
        if start_line - 1 + max_lines < len(lines):
            notes.append(f"{len(lines)} lines total; continue with start_line={start_line + max_lines}")
        if not chunk:
            return f"{path}: empty or start_line past end ({len(lines)} lines)"
        return f"{path} (lines {start_line}-{start_line + len(chunk) - 1})\n{body}" + (
            "\n[" + "; ".join(notes) + "]" if notes else "")

    def search_code(self, pattern, path_glob="", max_results=40, ignore_case=True):
        max_results = _as_int(max_results, 40, 1, 200)
        try:
            rx = re.compile(pattern, re.I if ignore_case in (True, "true", "True", 1) else 0)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}")
        hits, scanned = [], 0
        for rel in self.tracked_files():
            if path_glob and not (fnmatch.fnmatch(rel, path_glob) or fnmatch.fnmatch(os.path.basename(rel), path_glob)):
                continue
            p = self.root / rel
            try:
                if not p.is_file() or p.stat().st_size > MAX_SEARCH_FILE_BYTES or _looks_binary(p):
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line[:2000]):
                    hits.append(f"{rel}:{n}: {redact(line.strip())[:LINE_CLIP]}")
                    if len(hits) >= max_results:
                        return f"{len(hits)}+ matches (stopped at max_results), {scanned} files scanned\n" + "\n".join(hits)
        if not hits:
            return f"no matches for {pattern!r} in {scanned} files scanned"
        return f"{len(hits)} matches in {scanned} files scanned\n" + "\n".join(hits)

    def git_log(self, max_count=30, path="", names=False):
        max_count = _as_int(max_count, 30, 1, 100)
        total = self._git("rev-list", "--count", "HEAD")
        if total.returncode != 0:
            return "no git history available"
        shallow = self._git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        args = ["log", f"-n{max_count}", "--date=short", "--format=%h %ad %an: %s"]
        if names in (True, "true", "True", 1):
            args.append("--name-status")
        if path:
            self._resolve(path)
            args += ["--", path.strip().replace("\\", "/")]
        proc = self._git(*args)
        if proc.returncode != 0:
            raise ToolError(redact(proc.stderr.strip()[:200]) or "git log failed")
        note = f"{total.stdout.strip()} commits reachable"
        if shallow:
            note += " (shallow clone: older history is not available)"
        out = proc.stdout.strip() or "(no commits touch that path)"
        return redact(f"{note}\n{out}")

    def git_status(self):
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        proc = self._git("status", "--porcelain")
        dirty = proc.stdout.strip()
        return (f"checked-out branch (the remote's default branch): {branch}\n"
                + (f"uncommitted changes:\n{dirty}" if dirty else "working tree clean"))

    def tracked_but_ignored(self):
        proc = self._git("ls-files", "-c", "-i", "--exclude-standard", "-z")
        files = [f for f in proc.stdout.split("\0") if f]
        if not files:
            return "no tracked file matches the current ignore rules"
        return f"{len(files)} tracked files that the ignore rules say should not be tracked:\n" + "\n".join(files[:200])

    def parse_workflow(self, path):
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"{path}: no such file")
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError as exc:
            return f"{path} is not valid YAML: {str(exc)[:300]}"
        if not isinstance(doc, dict):
            return f"{path} does not contain a workflow mapping"

        raw_on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as True
        if isinstance(raw_on, str):
            triggers = {raw_on: {}}
        elif isinstance(raw_on, list):
            triggers = {str(t): {} for t in raw_on}
        elif isinstance(raw_on, dict):
            triggers = {str(k): (v or {}) for k, v in raw_on.items()}
        else:
            triggers = {}

        notes, all_runs, uses_list = [], [], []
        if "pull_request_target" in triggers:
            notes.append("trigger pull_request_target runs with repository secrets and a write token")
        if not triggers:
            notes.append("no triggers found")
        top_perm = doc.get("permissions")
        if top_perm == "write-all":
            notes.append("workflow-level permissions: write-all")

        jobs_out = []
        jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
        for jid, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if job.get("permissions") == "write-all":
                notes.append(f"job {jid}: permissions write-all")
            cond = job.get("if")
            if cond is False or str(cond).strip().lower() in ("false", "${{ false }}"):
                notes.append(f"job {jid}: `if` is always false, the job never runs")
            if re.search(r"deploy|release|publish", str(jid) + str(job.get("name", "")), re.I) and not job.get("needs"):
                notes.append(f"job {jid}: looks like deploy/release but has no `needs` on a test job")
            if str(job.get("continue-on-error")).lower() == "true":
                notes.append(f"job {jid}: continue-on-error is true")
            steps_out = []
            for idx, step in enumerate(job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                label = step.get("name") or step.get("uses") or f"step {idx + 1}"
                info = {"name": step.get("name")}
                uses, run = step.get("uses"), step.get("run")
                if uses:
                    info["uses"] = uses
                    uses_list.append(uses)
                    if "@" in str(uses) and not str(uses).startswith("./"):
                        ref = str(uses).split("@", 1)[1]
                        if ref.lower() in ("main", "master", "latest", "head", "dev", "develop"):
                            notes.append(f"job {jid}: action {uses} is pinned to a moving branch")
                        elif not _SHA.match(ref) and not re.match(r"^v?\d", ref):
                            notes.append(f"job {jid}: action {uses} uses a non-version ref")
                    if str(uses).startswith("actions/checkout") and "pull_request_target" in triggers:
                        ref = str((step.get("with") or {}).get("ref", ""))
                        if "pull_request.head" in ref or "head_ref" in ref:
                            notes.append(f"job {jid}: checks out the PR head under pull_request_target (untrusted code with secrets)")
                if run:
                    run = str(run)
                    all_runs.append(run)
                    info["run"] = " ; ".join(l.strip() for l in run.strip().splitlines())[:240]
                    if re.search(r"\|\|\s*(true|exit 0)\s*$", run.strip(), re.M):
                        notes.append(f"job {jid}, '{label}': command ends in `|| true`, failures are swallowed")
                    if _INJECTABLE.search(run):
                        notes.append(f"job {jid}, '{label}': attacker-controlled github.event text interpolated into a shell command")
                    if "echo" in run and "secrets." in run:
                        notes.append(f"job {jid}, '{label}': a secret is passed to echo")
                if step.get("if") is not None:
                    info["if"] = str(step["if"])
                    if str(step["if"]).strip().lower() in ("false", "${{ false }}"):
                        notes.append(f"job {jid}, '{label}': step `if` is always false")
                if str(step.get("continue-on-error")).lower() == "true":
                    info["continue_on_error"] = True
                    notes.append(f"job {jid}, '{label}': continue-on-error is true")
                steps_out.append({k: v for k, v in info.items() if v is not None})
            jobs_out.append({
                "id": jid, "runs_on": job.get("runs-on"), "needs": job.get("needs"),
                "if": job.get("if"), "permissions": job.get("permissions"), "steps": steps_out,
            })

        joined = "\n".join(all_runs)
        summary = {
            "file": path,
            "name": doc.get("name"),
            "triggers": triggers,
            "workflow_permissions": top_perm,
            "runs_tests": bool(_TEST_CMD.search(joined)),
            "runs_lint": bool(_LINT_CMD.search(joined)) or any("lint" in u.lower() for u in uses_list),
            "jobs": jobs_out,
            "heuristic_notes (hints only, you decide what is a real problem)": notes,
        }
        return json.dumps(summary, indent=1, default=str)

    def run_ruff(self, path=""):
        exe = shutil.which("ruff")
        if not exe:
            return "ruff is not installed in the auditor's environment, so no lint result is available"
        if not any(f.endswith(".py") for f in self.tracked_files()):
            return "no tracked Python files, ruff does not apply"
        target = self._resolve(path) if path else self.root
        proc = subprocess.run(
            [exe, "check", "--no-cache", "--output-format", "concise", str(target)],
            cwd=self.root, capture_output=True, text=True, errors="replace", timeout=90,
        )
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if proc.returncode == 0:
            return "ruff found no issues"
        if proc.returncode != 1:
            return f"ruff failed to run (exit {proc.returncode}): {redact(proc.stderr.strip()[:200])}"
        shown = "\n".join(redact(l)[:LINE_CLIP] for l in lines[:40])
        return (f"ruff reported {max(len(lines) - 1, 1)} diagnostics (the repo's own ruff config is used "
                f"if it has one, otherwise ruff defaults, so treat the count as a rough signal)\n{shown}")

    # ---- dispatch

    def dispatch(self, name, args):
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool {name!r}; available: {', '.join(TOOL_NAMES)}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("args must be a JSON object")
        fn = getattr(self, name)
        sig = inspect.signature(fn)
        extra = sorted(set(args) - set(sig.parameters))
        if extra:
            raise ToolError(f"{name} does not take {extra}; signature is {name}{sig}")
        try:
            result = fn(**args)
        except TypeError as exc:
            raise ToolError(f"bad arguments for {name}{sig}: {exc}")
        except subprocess.TimeoutExpired:
            raise ToolError(f"{name} timed out")
        return _clip(result)

    @staticmethod
    def tool_docs():
        return "\n".join(f"- {sig}\n    {desc}" for sig, desc in TOOL_DOCS)


TOOL_DOCS = [
    ("list_files(path='', max_entries=300)",
     "Tracked files (with sizes) under a directory. Large trees are collapsed into per-directory counts; call again with a directory to drill in."),
    ("read_file(path, start_line=1, max_lines=400)",
     "File contents with line numbers. Secret-looking values are masked as [REDACTED], which still tells you a literal value was there."),
    ("search_code(pattern, path_glob='', max_results=40, ignore_case=true)",
     "Regex search over tracked text files. path_glob like '*.yml' or 'src/*' narrows it. Returns file:line: text."),
    ("git_log(max_count=30, path='', names=false)",
     "Commit history, optionally for one path (also works for files that were since deleted). names=true adds changed files."),
    ("git_status()",
     "Checked-out branch (the remote's default branch) and whether the working tree is clean."),
    ("tracked_but_ignored()",
     "Files that are committed even though the repo's own ignore rules match them (generated or temporary files)."),
    ("parse_workflow(path)",
     "Structured summary of one GitHub Actions workflow: triggers, permissions, jobs, steps, whether tests/lint run, plus heuristic notes."),
    ("run_ruff(path='')",
     "Static lint of Python files with ruff. Never executes repository code. Reports unavailable if ruff is not installed."),
]
TOOL_NAMES = [sig.split("(")[0] for sig, _ in TOOL_DOCS]
