"""
Issue parsing + repo setup for `atomic-forge fix <github_issue_url>`.

Pure plumbing: turn an issue URL into (owner, repo, number), fetch the issue
text with `gh`, shallow-clone the repo, and stand up a Python venv so the
generated regression test can import the project and run under pytest.
No LLM, no CIE — just git/gh/venv.
"""
from __future__ import annotations

import re
import subprocess
import sys
import venv as _venv
from pathlib import Path
from typing import Optional

_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<num>\d+).*$",
    re.IGNORECASE,
)


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """github.com/{owner}/{repo}/issues/{N} -> (owner, repo, N). Raises
    ValueError with a clear message on anything that isn't an issue URL."""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"not a GitHub issue URL: {url!r}  "
            "(expected https://github.com/<owner>/<repo>/issues/<number>)")
    return m["owner"], m["repo"], int(m["num"])


def upstream_slug(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def fetch_issue(owner: str, repo: str, number: int) -> dict:
    """Fetch issue title + body via `gh issue view --json`. Returns
    {"title", "body", "url", "number", "owner", "repo"}."""
    r = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", "title,body,url"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh issue view {owner}/{repo}#{number} failed: "
                           f"{(r.stderr or r.stdout).strip()}")
    import json
    data = json.loads(r.stdout)
    return {"title": data.get("title", ""), "body": data.get("body", ""),
            "url": data.get("url", ""), "number": number, "owner": owner, "repo": repo}


def issue_to_bug_description(issue: dict) -> str:
    """The text fed to the test-gen agent: the issue title + body."""
    title = issue.get("title", "").strip()
    body = issue.get("body", "").strip()
    if body:
        return f"{title}\n\n{body}"
    return title or "(no issue body)"


def clone_repo(owner: str, repo: str, dest: Path, depth: int = 1) -> Path:
    """Shallow-clone the repo's default branch into `dest`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    r = subprocess.run(["git", "clone", "--depth", str(depth), url, str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git clone {url} failed: {(r.stderr or r.stdout).strip()}")
    return dest


def _detect_install_cmd(project_dir: Path) -> Optional[str]:
    if (project_dir / "pyproject.toml").exists() or (project_dir / "setup.py").exists():
        return "pip install -e .[dev]"
    if (project_dir / "requirements.txt").exists():
        return "pip install -r requirements.txt"
    return None


def setup_python_env(project_dir: Path, install_cmd: Optional[str] = None,
                     timeout: int = 600) -> str:
    """Create a venv at `<project_dir>/.venv`, install the project (so its
    modules are importable by the generated test), and ensure pytest is
    present. Returns the absolute path to the venv's python interpreter.

    `install_cmd` overrides the auto-detected install command; pass "" to
    skip installation entirely (e.g. for repos whose deps you've already
    installed)."""
    project_dir = Path(project_dir)
    venv_dir = project_dir / ".venv"
    if not venv_dir.exists():
        _venv.create(str(venv_dir), with_pip=True, clear=True)
    py = str(venv_dir / "bin" / "python")
    # Upgrade pip quietly (best-effort) — some repos need a recent pip for PEP 660.
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"],
                   capture_output=True, text=True, timeout=timeout)
    cmd = install_cmd if install_cmd is not None else _detect_install_cmd(project_dir)
    if cmd:  # "" means skip
        r = subprocess.run([py, "-m", "pip", "install", *cmd.split()],
                           cwd=str(project_dir), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(
                f"project install failed ({cmd}): {r.stderr[-800:].strip()}\n"
                "  — re-run with --install-cmd '' to skip, or --project-dir pointing "
                "at a checkout you've already set up.")
    # The repair loop + testgen run the generated test under this venv's pytest.
    subprocess.run([py, "-m", "pip", "install", "pytest", "-q"],
                   capture_output=True, text=True, timeout=timeout)
    return py


def make_test_cmd(venv_py: str, test_rel: str) -> str:
    """The repair loop's test command: run JUST the generated regression
    test under the project venv, fast and focused."""
    return f"{venv_py} -m pytest {test_rel} -q --tb=short -p no:cacheprovider"