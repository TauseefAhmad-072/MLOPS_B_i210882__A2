"""Turns the model's raw findings into the report the assignment specifies.

The model proposes; this module checks what it can check without trusting it:
files that don't exist, line numbers past the end, leaked secret values, duplicates.
"""
import difflib
import json
import os
import re
import tempfile

from tools import redact

PREFIX = {
    "repository_hygiene": "REPO",
    "dependency_management": "DEP",
    "code_quality": "QUAL",
    "testing": "TEST",
    "ci": "CI",
    "security": "SEC",
    "configuration": "CFG",
    "collaboration": "COLLAB",
    "build_release": "BUILD",
}
SEVERITIES = ("critical", "high", "medium", "low")
_SEV_ALIASES = {"moderate": "medium", "warning": "low", "info": "low", "minor": "low",
                "error": "high", "major": "high", "severe": "critical", "blocker": "critical"}
_CAT_HINTS = [
    ("hygiene", "repository_hygiene"), ("dependenc", "dependency_management"),
    ("lint", "code_quality"), ("quality", "code_quality"), ("test", "testing"),
    ("secur", "security"), ("secret", "security"), ("workflow", "ci"), ("action", "ci"),
    ("config", "configuration"), ("collab", "collaboration"), ("review", "collaboration"),
    ("build", "build_release"), ("release", "build_release"), ("deploy", "build_release"),
]
_ABSENCE = re.compile(r"(?i)missing|absent|lack|without|not found|not present|no such|does not exist|doesn't exist|nonexistent|\bno ")
_EMPTY = {"", "null", "none", "n/a", "na", "-"}
FIELDS = ("rule_id", "category", "severity", "file", "line", "description", "evidence", "recommendation")


def _category(raw):
    key = re.sub(r"[^a-z]+", "_", str(raw or "").lower()).strip("_")
    if key in PREFIX:
        return key
    if "ci" in key.split("_"):
        return "ci"
    for hint, cat in _CAT_HINTS:
        if hint in key:
            return cat
    return None


def _text(value, limit):
    return redact(re.sub(r"\s+", " ", str(value or "")).strip())[:limit]


def finalize(repo_url, raw_findings, tools, log=lambda m: None):
    kept = []
    for i, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            log(f"[report] dropped finding {i}: not an object")
            continue
        desc = _text(raw.get("description"), 300)
        evid = _text(raw.get("evidence"), 600)
        rec = _text(raw.get("recommendation"), 400)
        if not (desc and evid and rec):
            log(f"[report] dropped finding {i}: description, evidence or recommendation empty")
            continue
        cat = _category(raw.get("category"))
        if cat is None:
            log(f"[report] dropped finding {i}: unknown category {raw.get('category')!r}")
            continue
        sev = str(raw.get("severity", "")).strip().lower()
        sev = _SEV_ALIASES.get(sev, sev)
        if sev not in SEVERITIES:
            log(f"[report] finding {i}: severity {raw.get('severity')!r} not valid, using medium")
            sev = "medium"

        file = raw.get("file")
        file = None if file is None or str(file).strip().lower() in _EMPTY else re.sub(r"^(\./)+", "", str(file).strip().replace("\\", "/"))
        line = raw.get("line")
        if isinstance(line, bool) or not isinstance(line, (int, float, str)):
            line = None
        try:
            line = int(line) if line is not None and str(line).strip().lower() not in _EMPTY else None
        except ValueError:
            line = None

        if file is not None and not tools.has_file(file):
            # A missing-file finding may legitimately name the path that should exist.
            if _ABSENCE.search(f"{desc} {evid}"):
                line = None
            else:
                log(f"[report] dropped finding {i}: {file!r} does not exist and the finding is not about absence")
                continue
        if file is None or line is None or not (1 <= line <= tools.line_count(file)):
            line = None

        kept.append({"category": cat, "severity": sev, "file": file, "line": line,
                     "description": desc, "evidence": evid, "recommendation": rec})

    kept = _dedupe(kept)
    kept.sort(key=lambda v: (SEVERITIES.index(v["severity"]), v["category"], v["file"] or "", v["line"] or 0))
    counters = {}
    violations = []
    for v in kept:
        p = PREFIX[v["category"]]
        counters[p] = counters.get(p, 0) + 1
        violations.append({"rule_id": f"{p}-{counters[p]:03d}", **v})
    return {"repository": repo_url, "violations": violations}


def _dedupe(findings):
    out = []
    for f in findings:
        dup = None
        for g in out:
            if g["category"] != f["category"] or g["file"] != f["file"]:
                continue
            same_spot = f["line"] is not None and f["line"] == g["line"]
            similar = difflib.SequenceMatcher(None, f["description"].lower(), g["description"].lower()).ratio() > 0.8
            if same_spot or similar:
                dup = g
                break
        if dup is None:
            out.append(f)
        elif SEVERITIES.index(f["severity"]) < SEVERITIES.index(dup["severity"]):
            dup.update(f)
    return out


def validate_report(report):
    """Return a list of problems; empty means the report meets the output contract."""
    problems = []
    if not isinstance(report, dict) or not isinstance(report.get("repository"), str):
        return ["repository must be a string"]
    vs = report.get("violations")
    if not isinstance(vs, list):
        return ["violations must be a list"]
    for i, v in enumerate(vs):
        if not isinstance(v, dict):
            problems.append(f"violations[{i}] is not an object")
            continue
        missing = [k for k in FIELDS if k not in v]
        if missing:
            problems.append(f"violations[{i}] missing {missing}")
            continue
        if v["severity"] not in SEVERITIES:
            problems.append(f"violations[{i}] bad severity {v['severity']!r}")
        if v["file"] is not None and not isinstance(v["file"], str):
            problems.append(f"violations[{i}] file must be a string or null")
        if v["line"] is not None and (not isinstance(v["line"], int) or v["line"] < 1):
            problems.append(f"violations[{i}] line must be a positive integer or null")
    return problems


def write_json(path, data, jsonl=False):
    """Write via a temp file so a crash never leaves a half-written report."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if jsonl:
                for row in data:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
