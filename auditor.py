"""The agent loop: observe, reason, pick a tool, run it, read the result, repeat, report.

The model decides every tool call. This module only parses its reply, runs the tool,
feeds the output back, and enforces budgets.
"""
import json
import sys
import time

import prompts
from tools import ToolError, redact

MAX_BAD_REPLIES = 3
MAX_CONTEXT_CHARS = 140_000


class AgentError(Exception):
    pass


def _scrub(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def extract_json(text):
    """Pull the first complete {...} object out of a model reply (it may wrap it in fences or prose)."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no valid JSON object found in the reply")


def parse_action(reply):
    obj = extract_json(reply)
    kind = obj.get("action")
    if kind == "tool":
        if not isinstance(obj.get("tool"), str):
            raise ValueError('"tool" must be a tool name string')
        if obj.get("args") is not None and not isinstance(obj["args"], dict):
            raise ValueError('"args" must be a JSON object')
        return obj
    if kind == "finish":
        if not isinstance(obj.get("violations"), list):
            raise ValueError('"violations" must be a list (use [] if you found nothing)')
        return obj
    raise ValueError('"action" must be "tool" or "finish"')


class Auditor:
    def __init__(self, llm, tools, repo_url, max_steps=40, time_budget=600, log=None):
        self.llm = llm
        self.tools = tools
        self.repo_url = repo_url
        self.max_steps = max_steps
        self.time_budget = time_budget
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.trace = []

    # -- small helpers

    @staticmethod
    def _add_user(messages, text):
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": "user", "content": text})

    @staticmethod
    def _shrink(messages):
        if sum(len(m["content"]) for m in messages) <= MAX_CONTEXT_CHARS:
            return
        for m in messages[1:-4]:
            if m["role"] == "user" and len(m["content"]) > 1500:
                m["content"] = m["content"][:700] + "\n[... older output trimmed to save context ...]"

    def _note(self, **kw):
        self.trace.append({"t": round(time.time(), 2), **kw})

    # -- the loop

    def run(self):
        system = prompts.system_prompt(self.tools.tool_docs())
        messages = [{"role": "user", "content": prompts.kickoff(self.repo_url, self.max_steps)}]
        started = time.monotonic()
        calls = 0
        seen = {}
        reviewed = False
        forced = False
        bad = 0
        forced_misses = 0

        while True:
            out_of_time = time.monotonic() - started > self.time_budget
            if not forced and (calls >= self.max_steps or out_of_time):
                forced = True
                self.log(f"[agent] budget reached after {calls} calls, asking for the final report")
                self._note(event="budget_reached", calls=calls)
                self._add_user(messages, prompts.BUDGET)

            self._shrink(messages)
            reply = redact(self.llm.complete(system, messages))

            try:
                action = parse_action(reply)
            except ValueError as exc:
                bad += 1
                self._note(event="bad_reply", problem=str(exc), raw=reply[:300])
                if bad > MAX_BAD_REPLIES:
                    raise AgentError("the model kept sending replies that are not valid action JSON")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"That reply could not be used: {exc}. Send exactly one JSON object as described."})
                continue
            bad = 0
            messages.append({"role": "assistant", "content": reply})
            thought = redact(str(action.get("thought", "")))[:500]

            if action["action"] == "finish":
                if calls == 0:
                    if forced:
                        raise AgentError("ran out of budget before inspecting anything")
                    self._note(event="finish_rejected", reason="no tool calls yet")
                    messages.append({"role": "user", "content": "You have not inspected the repository yet. Use the tools first."})
                    continue
                if not reviewed and not forced and calls < self.max_steps - 1:
                    reviewed = True
                    self._note(event="review_requested", draft_findings=len(action["violations"]), thought=thought)
                    self.log(f"[agent] draft has {len(action['violations'])} findings, running review pass")
                    messages.append({"role": "user", "content": prompts.REVIEW})
                    continue
                self._note(event="finish", findings=len(action["violations"]), thought=thought)
                return action["violations"]

            # tool call
            if forced:
                forced_misses += 1
                if forced_misses > 2:
                    raise AgentError("the model would not produce a final report after the budget ran out")
                messages.append({"role": "user", "content": prompts.BUDGET})
                continue

            name = action["tool"]
            args = action.get("args") or {}
            key = name + json.dumps(args, sort_keys=True, default=str)
            failed = False
            if key in seen:
                obs = f"Same call as step {seen[key]}; output not repeated. Use what you already have, or call something different."
            else:
                try:
                    obs = self.tools.dispatch(name, args)
                    seen[key] = calls + 1
                except ToolError as exc:
                    obs, failed = f"ERROR: {exc}", True
                except Exception as exc:  # a tool bug should not kill the audit
                    obs, failed = f"ERROR: {type(exc).__name__}: {exc}", True
            calls += 1
            self.log(f"[step {calls}] {name}({', '.join(f'{k}={redact(str(v))[:50]}' for k, v in args.items())})")
            self._note(step=calls, tool=name, args=_scrub(args), thought=thought,
                       error=failed, result_chars=len(obs), result_preview=redact(obs)[:300])
            messages.append({"role": "user", "content": f"[result of call {calls}/{self.max_steps}: {name}]\n{obs}"})
