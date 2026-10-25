import hashlib
import json
from pathlib import Path

import pytest

from conftest import commit_more, make_repo
from tools import RepoTools, ToolError, redact


def test_list_files_shows_tracked_files(demo_repo):
    out = RepoTools(demo_repo).list_files()
    assert "README.md" in out and "app.py" in out
    assert "tracked files" in out


def test_list_files_collapses_big_directories(tmp_path):
    root = make_repo(tmp_path / "big", {f"src/mod{i}/f.py": "x=1\n" for i in range(60)} | {"README.md": "x"})
    out = RepoTools(root).list_files(max_entries=20)
    assert "collapsed" in out
    assert "src/  [60 files]" in out


def test_list_files_unknown_dir(demo_repo):
    with pytest.raises(ToolError):
        RepoTools(demo_repo).list_files("nope")


def test_read_file_numbers_lines_and_hides_secret(demo_repo):
    out = RepoTools(demo_repo).read_file("app.py")
    assert "    2  DB_PASSWORD = \"[REDACTED]\"" in out
    assert "s3cr3t-value-123" not in out
    assert 'os.environ["TOKEN_VALUE"]' in out  # env lookups are not secrets


def test_read_file_pagination(tmp_path):
    root = make_repo(tmp_path / "r", {"big.txt": "\n".join(f"line{i}" for i in range(1, 11))})
    out = RepoTools(root).read_file("big.txt", start_line=4, max_lines=3)
    assert "line4" in out and "line6" in out and "line7" not in out
    assert "start_line=7" in out


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/passwd", "a/../../x", ".git/config"])
def test_read_file_refuses_paths_outside_checkout(demo_repo, bad):
    with pytest.raises(ToolError):
        RepoTools(demo_repo).read_file(bad)


def test_read_file_missing_and_binary(tmp_path):
    root = make_repo(tmp_path / "r", {"a.txt": "hi"})
    (root / "blob.dat").write_bytes(b"\x00\x01\x02")
    t = RepoTools(root)
    with pytest.raises(ToolError):
        t.read_file("missing.txt")
    assert "binary" in t.read_file("blob.dat")


def test_search_code(demo_repo):
    t = RepoTools(demo_repo)
    out = t.search_code("password")
    assert "app.py:2:" in out and "s3cr3t" not in out
    assert "no matches" in t.search_code("zzz_nothing_zzz")
    assert "ci.yml" in t.search_code("pytest", path_glob="*.yml")
    with pytest.raises(ToolError):
        t.search_code("(unclosed")


def test_search_code_respects_max_results(tmp_path):
    root = make_repo(tmp_path / "r", {"a.txt": "x\n" * 50})
    out = RepoTools(root).search_code("x", max_results=5)
    assert out.count("a.txt:") == 5 and "stopped" in out


def test_git_log_for_repo_and_deleted_path(tmp_path):
    root = make_repo(tmp_path / "r", {".env": "A=1\n", "a.py": "1"}, "add env file")
    _ = __import__("subprocess").run(["git", "-C", str(root), "rm", "-q", ".env"], check=True)
    commit_more(root, {"b.py": "2"}, "remove env file")
    t = RepoTools(root)
    assert "remove env file" in t.git_log()
    assert "add env file" in t.git_log(path=".env")


def test_git_status(demo_repo):
    out = RepoTools(demo_repo).git_status()
    assert "working tree clean" in out and "branch" in out


def test_tracked_but_ignored(tmp_path):
    root = make_repo(tmp_path / "r", {"dist/app.js": "x", "src/a.py": "y"})
    commit_more(root, {".gitignore": "dist/\n"}, "add ignore")
    out = RepoTools(root).tracked_but_ignored()
    assert "dist/app.js" in out and "src/a.py" not in out


def test_parse_workflow_surfaces_the_awkward_parts(demo_repo):
    data = json.loads(RepoTools(demo_repo).parse_workflow(".github/workflows/ci.yml"))
    assert data["triggers"]["push"]["branches"] == ["dev"]
    assert data["runs_tests"] is True
    notes = " ".join(data["heuristic_notes (hints only, you decide what is a real problem)"])
    assert "master" in notes and "|| true" in notes and "continue-on-error" in notes


def test_parse_workflow_pull_request_target_checkout(tmp_path):
    wf = ("on: pull_request_target\npermissions: write-all\njobs:\n  b:\n    runs-on: x\n    steps:\n"
          "      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
          "      - run: echo \"${{ github.event.pull_request.title }}\"\n")
    root = make_repo(tmp_path / "r", {".github/workflows/p.yml": wf})
    notes = " ".join(json.loads(RepoTools(root).parse_workflow(".github/workflows/p.yml"))
                     ["heuristic_notes (hints only, you decide what is a real problem)"])
    assert "PR head" in notes and "write-all" in notes and "attacker-controlled" in notes


def test_parse_workflow_bad_yaml(tmp_path):
    root = make_repo(tmp_path / "r", {"w.yml": "a: [unclosed\n"})
    assert "not valid YAML" in RepoTools(root).parse_workflow("w.yml")


def test_run_ruff_reports_findings_or_unavailable(tmp_path):
    root = make_repo(tmp_path / "r", {"a.py": "import os\n"})
    out = RepoTools(root).run_ruff()
    assert "ruff" in out.lower()


def test_dispatch_rejects_unknown_tool_and_args(demo_repo):
    t = RepoTools(demo_repo)
    with pytest.raises(ToolError, match="unknown tool"):
        t.dispatch("rm_rf", {})
    with pytest.raises(ToolError, match="does not take"):
        t.dispatch("read_file", {"path": "app.py", "mode": "w"})
    with pytest.raises(ToolError):
        t.dispatch("read_file", {})


def test_dispatch_caps_output(tmp_path):
    root = make_repo(tmp_path / "r", {"a.txt": ("y" * 200 + "\n") * 500})
    out = RepoTools(root).dispatch("read_file", {"path": "a.txt", "max_lines": 500})
    assert len(out) < 13_000 and "output cut" in out


def test_tools_do_not_touch_the_repo(demo_repo):
    def snapshot():
        files = sorted(p for p in Path(demo_repo).rglob("*") if p.is_file())
        return {str(p): hashlib.sha1(p.read_bytes()).hexdigest() for p in files}
    before = snapshot()
    t = RepoTools(demo_repo)
    for name, args in [("list_files", {}), ("read_file", {"path": "app.py"}), ("search_code", {"pattern": "a"}),
                       ("git_log", {}), ("git_status", {}), ("tracked_but_ignored", {}),
                       ("parse_workflow", {"path": ".github/workflows/ci.yml"}), ("run_ruff", {})]:
        t.dispatch(name, args)
    assert snapshot() == before


@pytest.mark.parametrize("text,leaked", [
    ('password = "hunter2hunter2"', "hunter2hunter2"),
    ("API_KEY=abcdef123456789", "abcdef123456789"),
    ("aws = AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("t = ghp_" + "a1B2" * 10, "a1B2a1B2"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END RSA PRIVATE KEY-----", "MIIEvQIBADANBg"),
    ('{"secret": "longsecretvalue"}', "longsecretvalue"),
])
def test_redact_hides_values(text, leaked):
    assert leaked not in redact(text)


@pytest.mark.parametrize("text", [
    "password = os.environ['DB_PASSWORD']",
    "token: ${{ secrets.DEPLOY_TOKEN }}",
    "token = get_token()",
    "api_key = config.api_key",
    "password: changeme-please",
])
def test_redact_leaves_references_alone(text):
    assert redact(text) == text
