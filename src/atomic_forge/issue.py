"""
Issue parsing + repo setup for `atomic-forge fix <github_issue_url>`.

Pure plumbing: turn an issue URL into (owner, repo, number), fetch the issue
text with `gh`, shallow-clone the repo, and stand up a Python venv so the
generated regression test can import the project and run under pytest.
No LLM, no CIE — just git/gh/venv.
"""
from __future__ import annotations

import re
import shutil
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


#: Cap on how much comment text feeds the test-gen agent — comments on a
#: popular repo's issue can run to dozens of back-and-forths; the bug-report
#: text just needs enough of the thread to catch a maintainer's repro steps
#: or a clarifying traceback, not the whole history.
_MAX_COMMENT_CHARS = 6000


def _fetch_issue_ghql(owner: str, repo: str, number: int) -> dict:
    """gh's GraphQL-backed `issue view` — richest single-call fetch (title,
    body, comments in one query) but it spends GraphQL quota, which fresh
    accounts and bursty campaigns exhaust long before core REST."""
    r = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", "title,body,url,comments,state"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh issue view {owner}/{repo}#{number} failed: "
                           f"{(r.stderr or r.stdout).strip()}")
    import json
    data = json.loads(r.stdout)
    return {"title": data.get("title", ""), "body": data.get("body", ""),
            "comments": data.get("comments", []), "state": data.get("state", ""),
            "url": data.get("url", ""), "number": number, "owner": owner, "repo": repo}


def _rest_comments_to_gh_shape(comments: list) -> list:
    """REST /issues/{n}/comments entries ({user:{login}, body}) -> gh's
    comments shape ({author:{login}, body}), so every fetch channel feeds
    issue_to_bug_description() the same structure."""
    return [{"author": {"login": (c.get("user") or {}).get("login", "")},
             "body": c.get("body", "")}
            for c in (comments or []) if c.get("body")]


def _fetch_issue_rest(owner: str, repo: str, number: int, *, anon: bool = False) -> dict:
    """Core REST fetch — separate (and much larger) quota pool than GraphQL.
    `anon=False` goes through authenticated `gh api`; `anon=True` drops to
    unauthenticated curl, which works for public repos (60/hr per IP) — the
    last-resort channel when even the gh token can't move (invalid token,
    gh CLI broken, etc.). Comments come from a second call and are reshaped
    into gh's shape. Raises on rate-limit / any non-JSON response."""
    import json
    base = (f"https://api.github.com/repos/{owner}/{repo}/issues/{number}" if anon
            else f"repos/{owner}/{repo}/issues/{number}")
    if anon:
        run = lambda path: subprocess.run(["curl", "-sfL", path],
                                          capture_output=True, text=True, timeout=60)
    else:
        run = lambda path: subprocess.run(["gh", "api", path],
                                          capture_output=True, text=True, timeout=60)
    r = run(base)
    if r.returncode != 0:
        raise RuntimeError(f"REST issue fetch failed: {(r.stderr or r.stdout).strip()[:300]}")
    data = json.loads(r.stdout)
    if not isinstance(data, dict) or ("message" in data and "title" not in data):
        raise RuntimeError(f"REST issue fetch returned an API error: {str(data)[:300]}")
    comments = []
    rc = run(f"{base}/comments?per_page=50")
    if rc.returncode == 0:  # comments are best-effort: body alone beats nothing
        try:
            comments = _rest_comments_to_gh_shape(json.loads(rc.stdout))
        except Exception:
            comments = []
    return {"title": data.get("title", ""), "body": data.get("body", ""),
            "comments": comments, "state": data.get("state", ""),
            "url": data.get("html_url", ""), "number": number, "owner": owner, "repo": repo}


def fetch_issue(owner: str, repo: str, number: int) -> dict:
    """Fetch issue title + body + comments, falling back across channels:

    1. `gh issue view` (GraphQL) — richest single call, first quota to die
    2. `gh api` (authenticated core REST) — separate quota pool
    3. unauthenticated REST via curl — public repos, 60/hr per IP

    Returns {"title", "body", "comments", "state", "url", "number",
    "owner", "repo"} — `comments` is gh's own list of {"author", "body",
    ...} dicts (reshaped on the REST paths), oldest first. Comments matter
    for testgen: the original report is often thin, and a maintainer's
    repro steps or a "same thing happens when ..." from another user often
    land as a follow-up comment, not an edit to the body. "state" rides
    along so callers can cheaply re-verify the issue is still open.
    Private repos fail on the anonymous leg (as they should)."""
    errors = []
    for fetcher in (_fetch_issue_ghql,
                    lambda o, r, n: _fetch_issue_rest(o, r, n, anon=False),
                    lambda o, r, n: _fetch_issue_rest(o, r, n, anon=True)):
        try:
            return fetcher(owner, repo, number)
        except FileNotFoundError as e:  # gh / curl binary missing
            errors.append(f"{type(e).__name__}: {e}")
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    raise RuntimeError(f"could not fetch {owner}/{repo}#{number} on any channel:\n  "
                       + "\n  ".join(errors))


def issue_to_bug_description(issue: dict) -> str:
    """The text fed to the test-gen agent: title + body + comment thread
    (bounded — see _MAX_COMMENT_CHARS). Backward compatible: an `issue` dict
    with no "comments" key (or an empty one) behaves exactly as before."""
    title = issue.get("title", "").strip()
    body = issue.get("body", "").strip()
    text = f"{title}\n\n{body}" if body else (title or "(no issue body)")
    comments = issue.get("comments") or []
    if not comments:
        return text
    blocks = []
    total = 0
    for c in comments:
        cbody = (c.get("body") or "").strip()
        if not cbody:
            continue
        author = (c.get("author") or {}).get("login") if isinstance(c.get("author"), dict) else c.get("author")
        block = f"@{author or 'unknown'}: {cbody}"
        if total + len(block) > _MAX_COMMENT_CHARS:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return text
    return text + "\n\n## Comments\n" + "\n\n".join(blocks)


def _clone_head_ok(dest: Path) -> bool:
    """True iff the checkout has a resolvable HEAD. `git clone` can exit 0
    and still leave a zero-commit worktree (real case: sphinx's
    tests/roots/test-warnings/wrongenc.inc encoding failure), which then
    detonates much later as `git checkout -b: branch yet to be born`."""
    r = subprocess.run(["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def clone_repo(owner: str, repo: str, dest: Path, depth: int = 1) -> Path:
    """Shallow-clone the repo's default branch into `dest`.

    Idempotent: an existing clone is reused (fetched + reset to the
    remote default branch) rather than failing — sweep runs and resumed
    campaigns re-invoke this against dirs from aborted attempts, so a
    stale clone must never be a hard error. An existing checkout that
    *looks* clonable but has no HEAD is wiped and re-cloned (same class
    as a corrupt clone). A fresh clone is verified to have a resolvable
    HEAD and retried once — a zero-commit checkout is never returned."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    if dest.exists() and (dest / ".git").exists():
        for cmd in (["git", "-C", str(dest), "fetch", "--depth", str(depth),
                     "origin", "HEAD"],
                    ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
                    ["git", "-C", str(dest), "clean", "-fdxq"]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                # unrepairable clone (corrupt/remotes changed): wipe + reclone
                shutil.rmtree(dest, ignore_errors=True)
                break
        else:
            if _clone_head_ok(dest):
                return dest
            shutil.rmtree(dest, ignore_errors=True)  # zero-commit fake-clone: rebuild
    last_err = ""
    for _attempt in (1, 2):  # broken-partial clones get exactly one retry
        r = subprocess.run(["git", "clone", "--depth", str(depth), url, str(dest)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            last_err = (r.stderr or r.stdout).strip()
        elif not _clone_head_ok(dest):
            last_err = ("git exited 0 but the checkout has no resolvable HEAD "
                        "(partial/broken clone — see git's own output above)")
        else:
            return dest
        shutil.rmtree(dest, ignore_errors=True)
    raise RuntimeError(f"git clone {url} failed (one retry spent): {last_err}")


def _detect_install_cmd(project_dir: Path) -> Optional[str]:
    if (project_dir / "pyproject.toml").exists() or (project_dir / "setup.py").exists():
        return "pip install -e .[dev]"
    if (project_dir / "requirements.txt").exists():
        return "pip install -r requirements.txt"
    return None


def _install_arglist(project_dir: Path, install_cmd: Optional[str]) -> list[str]:
    """Normalize an install command into pip *argument* tokens.

    Sources disagree on shape: `_detect_install_cmd` returns a full command
    ("pip install -e .[dev]"), callers of --install-cmd pass either shape.
    Accept both, plus quoting/bracket extras (".[dev]" style), which naive
    cmd.split() mangles into bogus requirement names."""
    import shlex
    if install_cmd is not None:
        cmd = install_cmd.strip()
    else:
        cmd = (_detect_install_cmd(project_dir) or "").strip()
    if not cmd:
        return []
    # tolerate a full "pip install ..." / "python -m pip install ..." string
    toks = shlex.split(cmd)
    prefixes = (("pip", "install"), ("python", "-m", "pip", "install"),
                ("python3", "-m", "pip", "install"))
    for pre in prefixes:
        if toks[: len(pre)] == list(pre):
            toks = toks[len(pre):]
            break
    return toks


def _venv_has_pip(py: str) -> bool:
    r = subprocess.run([py, "-m", "pip", "--version"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


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
    py = str(venv_dir / "bin" / "python")
    if venv_dir.exists() and not _venv_has_pip(py):
        # e.g. an R16c scratch handed us a `uv venv` (pip-less) or a
        # half-dead venv — recreate rather than fail mid-install
        _venv.create(str(venv_dir), with_pip=True, clear=True)
    if not venv_dir.exists():
        _venv.create(str(venv_dir), with_pip=True, clear=True)
    # Upgrade pip quietly (best-effort) — some repos need a recent pip for PEP 660.
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"],
                   capture_output=True, text=True, timeout=timeout)
    cmd = install_cmd if install_cmd is not None else _detect_install_cmd(project_dir)
    if cmd:  # "" means skip
        args = _install_arglist(project_dir, install_cmd)
        r = subprocess.run([py, "-m", "pip", "install", *args],
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