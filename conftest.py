import copy
import json
import subprocess
from pathlib import Path

import pytest


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True, text=True,
    )


def make_repo(root, files, message="initial commit"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _git(root, "init", "-q")
    commit_more(root, files, message)
    return root


def commit_more(root, files, message):
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(root, "add", "-A", "-f")
    _git(root, "commit", "-q", "-m", message)


class ScriptedLLM:
    """Plays back canned replies and remembers what it was shown."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def complete(self, system, messages):
        self.seen.append(copy.deepcopy(messages))
        if not self.replies:
            raise AssertionError("script ran out of replies")
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


def tool(name, **args):
    return {"thought": "t", "action": "tool", "tool": name, "args": args}


def finish(*violations):
    return {"thought": "done", "action": "finish", "violations": list(violations)}


WORKFLOW = """\
name: CI
on:
  push:
    branches: [dev]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@master
      - name: run tests
        run: pytest || true
        continue-on-error: true
"""


@pytest.fixture
def demo_repo(tmp_path):
    return make_repo(tmp_path / "demo", {
        "README.md": "# demo\n",
        "app.py": 'import os\nDB_PASSWORD = "s3cr3t-value-123"\nOK = os.environ["TOKEN_VALUE"]\n',
        "requirements.txt": "flask\n",
        "__pycache__/app.cpython-311.pyc": "junk",
        ".github/workflows/ci.yml": WORKFLOW,
        "logo.bin": "",
    })
