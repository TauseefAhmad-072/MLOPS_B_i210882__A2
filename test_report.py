import json

from conftest import make_repo
from report import finalize, validate_report, write_json
from tools import RepoTools


def base(**over):
    f = {"category": "security", "severity": "high", "file": "app.py", "line": 2,
         "description": "Hardcoded database password", "evidence": "DB_PASSWORD assigned a string literal",
         "recommendation": "Read it from the environment"}
    f.update(over)
    return f


def fin(repo, findings):
    return finalize("https://github.com/x/y", findings, RepoTools(repo))


def test_valid_finding_passes_through(demo_repo):
    rep = fin(demo_repo, [base()])
    v = rep["violations"][0]
    assert v["rule_id"] == "SEC-001" and v["line"] == 2 and v["file"] == "app.py"
    assert validate_report(rep) == []


def test_empty_report_is_valid(demo_repo):
    rep = fin(demo_repo, [])
    assert rep == {"repository": "https://github.com/x/y", "violations": []}
    assert validate_report(rep) == []


def test_invented_file_is_dropped_but_absence_findings_survive(demo_repo):
    fake = base(file="settings/prod.py", description="Weak SECRET_KEY", evidence="literal key in settings")
    absent = base(category="repository_hygiene", severity="medium", file="CONTRIBUTING.md", line=None,
                  description="CONTRIBUTING.md is missing", evidence="file not found in tracked files")
    rep = fin(demo_repo, [fake, absent])
    assert [v["file"] for v in rep["violations"]] == ["CONTRIBUTING.md"]


def test_bad_line_numbers_become_null(demo_repo):
    assert fin(demo_repo, [base(line=999)])["violations"][0]["line"] is None
    assert fin(demo_repo, [base(line="2")])["violations"][0]["line"] == 2
    assert fin(demo_repo, [base(line="abc")])["violations"][0]["line"] is None
    assert fin(demo_repo, [base(file=None, line=2)])["violations"][0]["line"] is None


def test_null_like_files_and_aliases(demo_repo):
    v = fin(demo_repo, [base(file="N/A", category="Dependency Management", severity="Moderate")])["violations"][0]
    assert v["file"] is None and v["category"] == "dependency_management" and v["severity"] == "medium"


def test_unknown_category_and_empty_fields_dropped(demo_repo):
    rep = fin(demo_repo, [base(category="vibes"), base(evidence=""), "not a dict"])
    assert rep["violations"] == []


def test_secret_values_are_scrubbed(demo_repo):
    rep = fin(demo_repo, [base(evidence='password = "hunter2hunter2" in app.py')])
    assert "hunter2hunter2" not in json.dumps(rep)


def test_duplicates_collapse_keeping_worse_severity(demo_repo):
    rep = fin(demo_repo, [base(severity="medium"), base(severity="critical", description="Hardcoded database password!")])
    assert len(rep["violations"]) == 1 and rep["violations"][0]["severity"] == "critical"


def test_ids_are_unique_per_category_and_sorted_by_severity(demo_repo):
    rep = fin(demo_repo, [base(severity="low", line=1, description="aaa one"),
                          base(severity="critical", line=3, description="zzz other thing entirely"),
                          base(category="ci", file=".github/workflows/ci.yml", line=1, description="CI problem")])
    ids = [v["rule_id"] for v in rep["violations"]]
    assert ids == ["SEC-001", "CI-001", "SEC-002"] and rep["violations"][0]["severity"] == "critical"


def test_validate_report_catches_problems():
    bad = {"repository": "r", "violations": [{"rule_id": "X-1", "category": "ci", "severity": "urgent",
                                              "file": 3, "line": 0, "description": "d", "evidence": "e",
                                              "recommendation": "r"}, {"rule_id": "only"}]}
    problems = validate_report(bad)
    assert len(problems) == 4


def test_write_json_is_atomic_and_creates_dirs(tmp_path):
    out = tmp_path / "a" / "b" / "r.json"
    write_json(str(out), {"repository": "r", "violations": []})
    assert json.loads(out.read_text())["violations"] == []
    assert [p.name for p in out.parent.iterdir()] == ["r.json"]
