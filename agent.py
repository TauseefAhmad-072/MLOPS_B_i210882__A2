#!/usr/bin/env python3
"""CLI entry point.

    python agent.py --repo https://github.com/owner/name --output report.json
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

import repo as repo_mod
from auditor import AgentError, Auditor
from llm import ConfigError, LLMError, client_from_env
from report import finalize, validate_report, write_json
from tools import RepoTools, redact

EXIT_BAD_INPUT, EXIT_CONFIG, EXIT_CLONE, EXIT_LLM, EXIT_AGENT, EXIT_OTHER = 2, 3, 4, 5, 6, 1


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Audit a public GitHub repository for DevOps best-practice violations.")
    ap.add_argument("--repo", required=True, help="public GitHub repository URL")
    ap.add_argument("--output", required=True, help="where to write the JSON report")
    ap.add_argument("--trace", help="where to write the tool-call trace (default: <output>.trace.jsonl)")
    ap.add_argument("--max-steps", type=int, default=40, help="tool-call budget (default 40)")
    ap.add_argument("--time-budget", type=int, default=600, help="seconds before the agent is told to wrap up (default 600)")
    ap.add_argument("--local", action="store_true",
                    help="treat --repo as a path to an existing checkout instead of cloning (development and tests)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    return ap.parse_args(argv)


def main(argv=None, llm=None):
    args = parse_args(argv)
    load_dotenv()
    log = (lambda m: None) if args.quiet else (lambda m: print(redact(m), file=sys.stderr))

    def fail(code, msg):
        print(f"error: {redact(msg)}", file=sys.stderr)
        return code

    if args.max_steps < 1 or args.time_budget < 1:
        return fail(EXIT_BAD_INPUT, "--max-steps and --time-budget must be positive")
    if not args.local:
        try:
            repo_mod.parse_github_url(args.repo)
        except repo_mod.RepoError as exc:
            return fail(EXIT_BAD_INPUT, str(exc))
    elif not Path(args.repo).is_dir():
        return fail(EXIT_BAD_INPUT, f"--local given but {args.repo!r} is not a directory")

    if llm is None:
        try:
            llm = client_from_env()
        except ConfigError as exc:
            return fail(EXIT_CONFIG, str(exc))

    trace_path = args.trace or os.path.splitext(args.output)[0] + ".trace.jsonl"
    try:
        with tempfile.TemporaryDirectory(prefix="devops-audit-") as tmp:
            if args.local:
                root = Path(args.repo)
            else:
                log(f"[agent] cloning {args.repo}")
                root = Path(tmp) / "target"
                repo_mod.clone(args.repo, root)

            tools = RepoTools(root)
            auditor = Auditor(llm, tools, args.repo, max_steps=args.max_steps,
                              time_budget=args.time_budget, log=log)
            try:
                raw = auditor.run()
            finally:
                if auditor.trace:
                    write_json(trace_path, auditor.trace, jsonl=True)

            report = finalize(args.repo, raw, tools, log=log)
            problems = validate_report(report)
            if problems:  # would be a bug in report.py, never ship a malformed file
                return fail(EXIT_OTHER, "internal error, report failed validation: " + "; ".join(problems[:3]))
            write_json(args.output, report)
            if "working tree clean" not in tools.git_status() and not args.local:
                log("[agent] warning: the inspected checkout reports changes")
    except repo_mod.RepoError as exc:
        return fail(EXIT_CLONE, str(exc))
    except LLMError as exc:
        return fail(EXIT_LLM, str(exc))
    except AgentError as exc:
        return fail(EXIT_AGENT, str(exc))
    except KeyboardInterrupt:
        return fail(130, "interrupted")
    except OSError as exc:
        return fail(EXIT_OTHER, f"file error: {exc}")
    except Exception as exc:
        return fail(EXIT_OTHER, f"unexpected {type(exc).__name__}: {exc}")

    n = len(report["violations"])
    log(f"[agent] done: {n} violation{'s' if n != 1 else ''} written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
