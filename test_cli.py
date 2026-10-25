import json

import pytest

import agent
import repo as repo_mod
from conftest import ScriptedLLM, finish, tool
from llm import LLMError, client_from_env, ConfigError, AnthropicClient, OpenAIClient


def run_cli(tmp_path, repo, llm, extra=()):
    out = tmp_path / "out" / "report.json"
    code = agent.main(["--repo", str(repo), "--output", str(out), "--local", "-q", *extra], llm=llm)
    return code, out


def test_end_to_end_writes_valid_report_and_trace(tmp_path, demo_repo):
    v = {"category": "security", "severity": "high", "file": "app.py", "line": 2,
         "description": "Hardcoded database password", "evidence": "literal assigned to DB_PASSWORD",
         "recommendation": "Use environment variables"}
    llm = ScriptedLLM([tool("search_code", pattern="password"), tool("read_file", path="app.py"), finish(v), finish(v)])
    code, out = run_cli(tmp_path, demo_repo, llm)
    assert code == 0
    report = json.loads(out.read_text())
    assert report["violations"][0]["rule_id"] == "SEC-001"
    trace = [json.loads(l) for l in (out.parent / "report.trace.jsonl").read_text().splitlines()]
    assert [t["tool"] for t in trace if "tool" in t] == ["search_code", "read_file"]


def test_empty_result_still_writes_report(tmp_path, demo_repo):
    code, out = run_cli(tmp_path, demo_repo, ScriptedLLM([tool("list_files"), finish(), finish()]))
    assert code == 0 and json.loads(out.read_text())["violations"] == []


@pytest.mark.parametrize("url", ["not a url", "http://github.com/a/b", "https://gitlab.com/a/b",
                                 "git@github.com:a/b.git", "https://github.com/a", "--upload-pack=x",
                                 "https://github.com/a/b/../../c", "file:///etc/passwd"])
def test_rejects_non_github_urls(url):
    with pytest.raises(repo_mod.RepoError):
        repo_mod.parse_github_url(url)


@pytest.mark.parametrize("url", ["https://github.com/a/b", "https://github.com/a/b.git", "https://github.com/a/b/",
                                 "https://www.github.com/some-org/my.repo"])
def test_accepts_github_urls(url):
    assert repo_mod.parse_github_url(url)


def test_bad_url_exit_code(tmp_path, capsys):
    code = agent.main(["--repo", "https://example.com/x/y", "--output", str(tmp_path / "r.json")])
    assert code == agent.EXIT_BAD_INPUT and "not a public GitHub" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_missing_api_key_exit_code(tmp_path, monkeypatch, capsys):
    for k in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # so no stray .env gets loaded
    code = agent.main(["--repo", "https://github.com/a/b", "--output", str(tmp_path / "r.json")])
    assert code == agent.EXIT_CONFIG and "API key" in capsys.readouterr().err


def test_clone_failure_exit_code(tmp_path, monkeypatch, capsys):
    def boom(url, dest, **kw):
        raise repo_mod.RepoError("could not clone: repository not found")
    monkeypatch.setattr(repo_mod, "clone", boom)
    code = agent.main(["--repo", "https://github.com/a/b", "--output", str(tmp_path / "r.json")], llm=ScriptedLLM([]))
    assert code == agent.EXIT_CLONE and "not found" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_llm_failure_exit_code(tmp_path, demo_repo, capsys):
    class Down:
        def complete(self, system, messages):
            raise LLMError("API down")
    code, out = run_cli(tmp_path, demo_repo, Down())
    assert code == agent.EXIT_LLM and not out.exists()
    assert "API down" in capsys.readouterr().err


def test_agent_failure_exit_code(tmp_path, demo_repo):
    code, out = run_cli(tmp_path, demo_repo, ScriptedLLM(["junk"] * 10))
    assert code == agent.EXIT_AGENT and not out.exists()


def test_local_path_must_exist(tmp_path):
    code, _ = run_cli(tmp_path, tmp_path / "nope", ScriptedLLM([]))
    assert code == agent.EXIT_BAD_INPUT


def test_client_from_env():
    c = client_from_env({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k", "LLM_MODEL": "m"})
    assert isinstance(c, AnthropicClient) and c.model == "m"
    c = client_from_env({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "LLM_BASE_URL": "http://localhost:11434/v1/"})
    assert isinstance(c, OpenAIClient) and c.url == "http://localhost:11434/v1/chat/completions"
    with pytest.raises(ConfigError):
        client_from_env({"LLM_PROVIDER": "cohere", "LLM_API_KEY": "k"})
    with pytest.raises(ConfigError):
        client_from_env({"LLM_TEMPERATURE": "hot", "LLM_API_KEY": "k"})


def test_llm_client_retries_then_gives_up(monkeypatch):
    import llm as llm_mod
    calls = []

    class Resp:
        status_code = 503
        text = "busy"

    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: calls.append(1) or Resp())
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    with pytest.raises(LLMError, match="3 attempts"):
        AnthropicClient("k", "m").complete("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 3


def test_llm_client_does_not_retry_auth_errors(monkeypatch):
    import llm as llm_mod

    class Resp:
        status_code = 401
        text = "bad key"

    n = []
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: n.append(1) or Resp())
    with pytest.raises(LLMError, match="401"):
        OpenAIClient("k", "m").complete("s", [{"role": "user", "content": "hi"}])
    assert len(n) == 1
