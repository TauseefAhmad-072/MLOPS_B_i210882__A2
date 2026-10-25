"""Getting the target repository onto disk, safely."""
import os
import re
import subprocess

from tools import redact

_GITHUB_URL = re.compile(
    r"^https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class RepoError(Exception):
    pass


def parse_github_url(url):
    """Return (owner, name) or raise. Only plain https github.com URLs are accepted."""
    m = _GITHUB_URL.match((url or "").strip())
    if not m:
        raise RepoError(
            f"not a public GitHub repository URL: {url!r} "
            "(expected https://github.com/<owner>/<repo>)"
        )
    owner, name = m.groups()
    if owner.startswith("-") or name.startswith("-") or name in (".", ".."):
        raise RepoError(f"suspicious repository URL: {url!r}")
    return owner, name


def clone(url, dest, depth=300, timeout=180):
    owner, name = parse_github_url(url)
    clean_url = f"https://github.com/{owner}/{name}.git"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    cmd = [
        "git",
        "-c", "core.hooksPath=/dev/null",   # nothing from the repo gets to run
        "-c", "core.symlinks=false",        # symlinks become plain files
        "-c", "credential.helper=",
        "clone", "--quiet", "--depth", str(depth), "--no-tags", "--single-branch",
        "--", clean_url, str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RepoError(f"cloning {clean_url} timed out after {timeout}s")
    except FileNotFoundError:
        raise RepoError("git is not installed or not on PATH")
    if proc.returncode != 0:
        msg = redact((proc.stderr or proc.stdout or "").strip())[:400]
        raise RepoError(f"could not clone {clean_url} (private or missing?): {msg}")
    return dest
