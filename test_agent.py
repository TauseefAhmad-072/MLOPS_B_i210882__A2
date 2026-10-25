import pytest

import auditor as auditor_mod
from auditor import AgentError, Auditor, extract_json, parse_action
from conftest import ScriptedLLM, finish, tool
from tools import RepoTools

VIOLATION = {"category": "ci", "severity": "high", "file": ".github/workflows/ci.yml", "line": 4,
             "description": "CI only runs on dev", "evidence": "push trigger lists only dev",
             "recommendation": "Trigger on the default branch and PRs"}


def run(repo, replies, **kw):
    llm = ScriptedLLM(replies)
    a = Auditor(llm, RepoTools(repo), "https://github.com/x/y", log=lambda m: None, **kw)
    return a, llm, a.run()


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        extract_json("no json here")


@pytest.mark.parametrize("bad", ['{"action": "dance"}', '{"action": "tool"}',
                                 '{"action": "finish", "violations": "none"}',
                                 '{"action": "tool", "tool": "read_file", "args": []}'])
def test_parse_action_rejects(bad):
    with pytest.raises(ValueError):
        parse_action(bad)


def test_loop_feeds_tool_output_back_and_returns_findings(demo_repo):
    a, llm, out = run(demo_repo, [tool("list_files"), tool("read_file", path=".github/workflows/ci.yml"),
                                  finish(VIOLATION), finish(VIOLATION)])
    assert out == [VIOLATION]
    assert [t["tool"] for t in a.trace if "tool" in t] == ["list_files", "read_file"]
    # the second request already contains the file listing the first tool produced
    assert "README.md" in llm.seen[1][-1]["content"]
    # and the third contains the workflow text
    assert "branches: [dev]" in llm.seen[2][-1]["content"]


def test_next_tool_depends_on_what_the_model_saw(demo_repo):
    # two different scripts, two different investigations: the loop does not impose an order
    a1, _, _ = run(demo_repo, [tool("search_code", pattern="password"), finish(), finish()])
    a2, _, _ = run(demo_repo, [tool("git_log"), tool("parse_workflow", path=".github/workflows/ci.yml"), finish(), finish()])
    assert [t["tool"] for t in a1.trace if "tool" in t] == ["search_code"]
    assert [t["tool"] for t in a2.trace if "tool" in t] == ["git_log", "parse_workflow"]


def test_finish_before_inspecting_is_pushed_back(demo_repo):
    a, llm, out = run(demo_repo, [finish(), tool("list_files"), finish(), finish()])
    assert "have not inspected" in llm.seen[1][-1]["content"]
    assert out == []


def test_review_pass_can_drop_findings(demo_repo):
    weak = dict(VIOLATION, description="something else entirely", file="README.md", line=None)
    a, llm, out = run(demo_repo, [tool("list_files"), finish(VIOLATION, weak), finish(VIOLATION)])
    assert out == [VIOLATION]
    assert "skeptical reviewer" in llm.seen[2][-1]["content"]
    assert any(t.get("event") == "review_requested" for t in a.trace)


def test_review_pass_can_trigger_more_tool_calls(demo_repo):
    a, _, out = run(demo_repo, [tool("list_files"), finish(VIOLATION), tool("read_file", path="app.py"), finish()])
    assert out == []
    assert [t["tool"] for t in a.trace if "tool" in t] == ["list_files", "read_file"]


def test_invalid_reply_gets_a_second_chance(demo_repo):
    a, llm, out = run(demo_repo, ["I think the repo is fine", tool("list_files"), finish(), finish()])
    assert "could not be used" in llm.seen[1][-1]["content"]
    assert out == []


def test_gives_up_after_repeated_garbage(demo_repo):
    with pytest.raises(AgentError):
        run(demo_repo, ["nope"] * 10)


def test_tool_errors_become_observations(demo_repo):
    a, llm, _ = run(demo_repo, [tool("read_file", path="../../etc/passwd"), tool("no_such_tool"), finish(), finish()])
    assert "ERROR: path is outside" in llm.seen[1][-1]["content"]
    assert "ERROR: unknown tool" in llm.seen[2][-1]["content"]
    assert all(t["error"] for t in a.trace if "tool" in t)


def test_repeated_call_is_not_executed_twice(demo_repo):
    a, llm, _ = run(demo_repo, [tool("list_files"), tool("list_files"), finish(), finish()])
    assert "output not repeated" in llm.seen[2][-1]["content"]


def test_budget_forces_a_final_answer(demo_repo):
    a, llm, out = run(demo_repo, [tool("list_files"), finish(VIOLATION)], max_steps=1)
    assert out == [VIOLATION]
    assert "budget" in llm.seen[1][-1]["content"].lower()


def test_budget_refusal_is_fatal(demo_repo):
    with pytest.raises(AgentError):
        run(demo_repo, [tool("list_files"), tool("git_log"), tool("git_status"), tool("git_log", max_count=3)], max_steps=1)


def test_secrets_in_model_output_never_reach_the_trace(demo_repo):
    leaky = tool("search_code", pattern="x")
    leaky["thought"] = 'found password = "hunter2hunter2"'
    a, _, _ = run(demo_repo, [leaky, finish(), finish()])
    assert "hunter2hunter2" not in str(a.trace)


def test_old_observations_are_trimmed(demo_repo, monkeypatch):
    monkeypatch.setattr(auditor_mod, "MAX_CONTEXT_CHARS", 3000)
    big = tool("read_file", path="big.txt", max_lines=1000)
    (demo_repo / "big.txt").write_text("z" * 150 + "\n" * 1 + ("z" * 150 + "\n") * 40)
    a, llm, _ = run(demo_repo, [big, tool("list_files"), tool("git_log"), tool("git_status"),
                                tool("tracked_but_ignored"), tool("search_code", pattern="q"), finish(), finish()])
    assert any("trimmed" in m["content"] for m in llm.seen[-1])
