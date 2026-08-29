"""
Execution primitives: run a command, run a test suite, git version control,
and a pre-write syntax gate.

- run(): shell out with a timeout, sentinel-free, bounded output — one of
  the core "agent-computer interface" lessons: never dump unbounded tool
  output into a prompt.
- git helpers: every accepted file write is committed, so any repair step
  can be undone and every attempt is auditable (git-native undo).
- lint_gate(): a syntax check BEFORE an edit is allowed to land — free and
  exact for Python (compile()); best-effort for JS/TS via node/tsc if
  present; otherwise a visible "skipped", never a silent, indistinguishable
  pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .stacks import TestStack, detect_test_stack  # noqa: F401 - re-exported

MAX_OUTPUT_CHARS = 6000  # bound tool feedback; previews, not dumps

#: Every mainstream JS test runner (Vitest, Jest, CRA) treats CI=true as
#: "run once and exit" instead of defaulting to an interactive watch mode
#: that never returns.
_TEST_ENV = {**os.environ, "CI": "true", "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False
    #: The UNTRUNCATED output `output` was derived from. `output` itself is
    #: capped at MAX_OUTPUT_CHARS (head+tail, dropping the middle) for
    #: tool-feedback/display — a caller doing TEXT ANALYSIS (not display),
    #: e.g. counting failures out of a combined multi-stack test run, must
    #: parse this instead, or a summary line landing in the truncated
    #: middle silently undercounts real failures.
    full_output: str = ""

    def __post_init__(self) -> None:
        if not self.full_output:
            self.full_output = self.output

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n... [truncated {len(text) - limit} chars] ...\n{tail}"


def run(cmd: list[str] | str, cwd: str | Path, timeout: int = 300,
        env: Optional[dict] = None) -> RunResult:
    """Run a command; capture combined output, truncated. Never raises."""
    shell = isinstance(cmd, str)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), shell=shell, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if not out.strip():
            out = "[command ran successfully and produced no output]"
        return RunResult(exit_code=proc.returncode, output=truncate(out), full_output=out)
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + f"\n[TIMEOUT after {timeout}s]"
        return RunResult(exit_code=124, output=truncate(out), full_output=out, timed_out=True)
    except FileNotFoundError as e:
        return RunResult(exit_code=127, output=f"[command not found: {e}]")


def _purge_pycache(project_dir: str | Path) -> None:
    """Delete every `__pycache__` directory under `project_dir` before a
    test run. Confirmed live (2026-08-29) as a real, intermittent
    execution-based-selection bug, not just test flakiness: the repair
    loop writes a candidate's fixed content to a target .py file, then
    spawns a FRESH `python -m pytest` subprocess to test it — sometimes
    several times (K candidates, repair rounds) well under a second apart.
    Reproduced directly, isolated from all of forge's own code: rewriting
    a module and immediately re-running `python -m pytest` in a new
    subprocess intermittently (~30-40% of the time under this exact
    write-retest-write-retest pattern) still evaluates the PREVIOUS
    content — pytest's own assertion-rewrite import hook caches a `.pyc`
    under `__pycache__` keyed by the source file's mtime, and on
    filesystems/containers with coarse mtime resolution two writes inside
    the same resolution window can land on an identical mtime despite
    different content, so the cached (stale) bytecode is judged "still
    valid" and reused. `PYTHONDONTWRITEBYTECODE=1` (see `_TEST_ENV`) alone
    does NOT fix this — it only suppresses writing NEW .pyc files, not
    reading/trusting ones that already exist from an earlier run in the
    same project_dir. Deleting the cache outright before every test run
    is the only fix that closes the read side too; verified clean across
    20/20 repeated write→purge→test cycles in the minimal repro, versus
    ~30-40% failure without it. The directory walk itself is negligible
    cost next to spawning a test-runner subprocess."""
    for cache_dir in Path(project_dir).rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def run_test(cmd: str, image: Optional[str], project_dir: str | Path, timeout: int = 300) -> RunResult:
    """Like run(), but for a TestStack's command: routes through a
    per-project Docker container (see docker_env.py) when `image` is set
    and Docker is actually usable, falling back to a bare host run()
    otherwise."""
    _purge_pycache(project_dir)
    if image is None:
        return run(cmd, cwd=project_dir, timeout=timeout, env=_TEST_ENV)
    from . import docker_env
    if not docker_env.docker_available():
        return run(cmd, cwd=project_dir, timeout=timeout, env=_TEST_ENV)
    container = docker_env.get_or_create(Path(project_dir), image)
    if container is None:
        return run(cmd, cwd=project_dir, timeout=timeout, env=_TEST_ENV)
    return docker_env.exec_in(container, cmd, cwd=Path(project_dir), timeout=timeout,
                              env={"CI": "true", "PYTHONDONTWRITEBYTECODE": "1"})


#: (phase, status, detail) -> None. Callers pass None (the default) to opt
#: out entirely; every emit() below is wrapped so a broken callback can
#: never break the test run it's reporting on.
ProgressCallback = Callable[[str, str, str], None]


def run_test_with_progress(
    stack: TestStack, project_dir: str | Path, timeout: int = 300,
    on_progress: Optional[ProgressCallback] = None, test_phase: str = "running_tests",
) -> RunResult:
    """Like run_test(), but reports each real sub-step as a distinct phase
    event via `on_progress` — useful for showing "starting container" /
    "installing dependencies" / "running tests" live instead of one opaque
    multi-minute black box."""
    def emit(phase: str, status: str, detail: str = "") -> None:
        if on_progress is None:
            return
        try:
            on_progress(phase, status, detail)
        except Exception:  # noqa: BLE001 - reporting must never break the run
            pass

    project_dir = Path(project_dir)
    container: Optional[str] = None
    if stack.image is not None:
        from . import docker_env
        emit("docker_setup", "running", f"starting container ({stack.image})")
        if docker_env.docker_available():
            container = docker_env.get_or_create(project_dir, stack.image)
        if container is not None:
            emit("docker_setup", "done", f"container ready ({stack.image})")
        else:
            emit("docker_setup", "skipped", "Docker unavailable — running on host instead")
    else:
        emit("docker_setup", "skipped", "this stack runs directly on host")

    def _exec(cmd: str) -> RunResult:
        _purge_pycache(project_dir)  # see _purge_pycache's docstring — same staleness race applies here
        if container is not None:
            from . import docker_env
            return docker_env.exec_in(container, cmd, cwd=project_dir, timeout=timeout,
                                       env={"CI": "true", "PYTHONDONTWRITEBYTECODE": "1"})
        return run(cmd, cwd=project_dir, timeout=timeout, env=_TEST_ENV)

    emit(test_phase, "running", stack.cmd)
    result = _exec(stack.cmd)
    emit(test_phase, "done" if result.ok else "failed", result.output[-500:])
    return result


def detect_test_command(project_dir: str | Path) -> str | None:
    """Thin backward-compatible wrapper over detect_test_stack, returning
    a bare command string."""
    stack = detect_test_stack(project_dir)
    return stack.cmd if stack else None


# ---------------------------------------------------------------- git ----

def git_available() -> bool:
    return run(["git", "--version"], cwd=".").ok


def _repo_toplevel(project_dir: str | Path) -> Optional[Path]:
    """Resolves the git work-tree `project_dir` would act on — the
    project's own repo if it has one, or an ENCLOSING repo's toplevel if
    project_dir has no `.git` of its own but sits inside one. Returns
    None if git can't find any repo at all from there."""
    res = run(["git", "rev-parse", "--show-toplevel"], cwd=project_dir)
    if not res.ok:
        return None
    try:
        return Path(res.full_output.strip()).resolve()
    except OSError:
        return None


def _is_own_repo(project_dir: str | Path) -> bool:
    """True only if project_dir IS the toplevel of its own git work tree
    — i.e. git commands run there act on project_dir, not on some
    ancestor directory's repo. `git add -A` / `git commit` with no
    pathspec operate repo-wide regardless of cwd, so treating "a repo
    exists somewhere above here" as good enough would let a caller with
    no `.git` of its own silently stage and commit into whatever repo
    happens to contain it (confirmed live: this is how a `--project-dir`
    nested in this very repo, with no `ensure_repo()` call first, ended
    up committing this repo's own unrelated working-tree changes)."""
    project_dir = Path(project_dir).resolve()
    toplevel = _repo_toplevel(project_dir)
    return toplevel is not None and toplevel == project_dir


def ensure_repo(project_dir: str | Path) -> bool:
    """git-init the project dir if needed. Returns True when a repo
    ISOLATED to project_dir is usable — never inits into (or otherwise
    claims) an enclosing repo project_dir merely happens to sit inside."""
    project_dir = Path(project_dir)
    if not git_available():
        return False
    if (project_dir / ".git").exists():
        return True
    enclosing = _repo_toplevel(project_dir)
    if enclosing is not None and enclosing != project_dir.resolve():
        print(f"[forge] WARNING: {project_dir} has no .git of its own and sits inside "
              f"an existing repo at {enclosing} — refusing to git-init or commit here "
              f"to avoid touching that repo's history. Pass a project_dir outside any "
              f"existing repo (or pre-init one at project_dir) for git tracking.",
              file=sys.stderr)
        return False
    if not run(["git", "init", "-q"], cwd=project_dir).ok:
        return False
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "__pycache__/\n*.pyc\n.venv/\n.forge_venv/\nnode_modules/\n"
            ".pytest_cache/\ntarget/\n.gradle/\n.angular/\nbuild/\n.forge/\n"
        )
    cfg = run('git config user.email "forge@local" && git config user.name "atomic-forge"',
              cwd=project_dir)
    if not cfg.ok:
        return False
    run(["git", "add", "-A"], cwd=project_dir)
    run(["git", "commit", "-q", "-m", "forge: init"], cwd=project_dir)
    return True


def commit(project_dir: str | Path, message: str) -> bool:
    """Stage everything and commit. Returns False (loudly, on stderr) if
    git fails, OR if project_dir isn't the toplevel of its own repo — the
    pipeline keeps working without VCS, but you lose undo/audit. Refusing
    to commit here (rather than letting `git add -A` resolve upward into
    an ancestor repo) is the fix for a real bug found live: a project_dir
    with no `.git` of its own, nested inside another repo, previously got
    its enclosing repo's ENTIRE unrelated working tree staged and
    committed under a misleading "forge: generate ..." message."""
    if not _is_own_repo(project_dir):
        print(f"[forge] WARNING: git commit skipped ({message}): {project_dir} is not "
              f"the toplevel of its own git repo — call ensure_repo(project_dir) first, "
              f"or this project_dir sits inside another repo and committing here would "
              f"touch that repo instead.", file=sys.stderr)
        return False
    add = run(["git", "add", "-A"], cwd=project_dir)
    ci = run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=project_dir)
    if not (add.ok and ci.ok):
        print(f"[forge] WARNING: git commit failed ({message}): {ci.output[:200]}", file=sys.stderr)
        return False
    return True


def revert_file(project_dir: str | Path, rel_path: str) -> RunResult:
    """Undo uncommitted/last-committed changes to one file (git-native /undo)."""
    return run(["git", "checkout", "HEAD~1", "--", rel_path], cwd=project_dir)


# ------------------------------------------------------------ lint gate ----

_JS_SUFFIXES = (".js", ".jsx")
_TS_SUFFIXES = (".ts", ".tsx")


def lint_gate(project_dir: str | Path, rel_path: str, content: str) -> tuple[bool, str]:
    """Syntax check BEFORE an edit is allowed to land.

    Python: compile() — free and exact, always runs.
    Any language: an external linter via FORGE_LINT_CMD (a {path}
    placeholder, e.g. 'npx tsc --noEmit') takes priority when configured.
    JS/JSX: `node --check` — pure syntax check, no installed deps needed.
    TS/TSX: best-effort `tsc --noEmit` via the project's own installed
    `node_modules/.bin/tsc`, if present.
    Anything else reports a visible "lint skipped: <reason>" rather than a
    bare, indistinguishable pass.
    """
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".py":
        try:
            compile(content, rel_path, "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"Python syntax error in {rel_path}: {e}"

    custom = os.environ.get("FORGE_LINT_CMD")
    if custom:
        tmp = Path(project_dir) / rel_path
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content)
        res = run(custom.replace("{path}", rel_path), cwd=project_dir)
        if not res.ok:
            return False, f"lint failed ({custom}):\n{res.output}"
        return True, ""

    if suffix in _JS_SUFFIXES:
        return _node_check(project_dir, rel_path, content)
    if suffix in _TS_SUFFIXES:
        return _tsc_check(project_dir, rel_path, content)

    return True, f"lint skipped: no checker configured for {suffix or 'files with no extension'}"


def _node_check(project_dir: str | Path, rel_path: str, content: str) -> tuple[bool, str]:
    node = shutil.which("node")
    if node is None:
        return True, "lint skipped: node not found on PATH"
    project_dir = Path(project_dir)
    fd, tmp_name = tempfile.mkstemp(suffix=Path(rel_path).suffix, dir=str(project_dir))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        res = run([node, "--check", str(tmp_path)], cwd=project_dir, timeout=30)
    finally:
        tmp_path.unlink(missing_ok=True)
    if res.exit_code == 127:
        return True, "lint skipped: node not available"
    if not res.ok:
        return False, f"node --check failed for {rel_path}:\n{res.output}"
    return True, ""


_TSC_DEFAULT_TARGET = "ES2020"
_TSC_DEFAULT_LIB = ["ES2020", "DOM", "DOM.Iterable"]
_TSC_DEFAULT_JSX = "react-jsx"
_TSC_DEFAULT_MODULE = "ESNext"
_TSC_DEFAULT_MODULE_RESOLUTION = "bundler"


def _tsc_compiler_flags(project_dir: Path) -> list[str]:
    """Forward the project's own tsconfig.json compilerOptions relevant to
    single-file checking (a bare `tsc <file>` with no `-p` ignores
    tsconfig.json entirely and falls back to old, incompatible defaults —
    too-old lib, skipLibCheck off, classic module resolution)."""
    target, lib, jsx = _TSC_DEFAULT_TARGET, _TSC_DEFAULT_LIB, _TSC_DEFAULT_JSX
    module, module_resolution = _TSC_DEFAULT_MODULE, _TSC_DEFAULT_MODULE_RESOLUTION
    tsconfig_path = project_dir / "tsconfig.json"
    if tsconfig_path.exists():
        try:
            opts = json.loads(tsconfig_path.read_text()).get("compilerOptions", {})
        except (json.JSONDecodeError, OSError):
            opts = {}
        target = opts.get("target", target)
        lib = opts.get("lib", lib)
        jsx = opts.get("jsx", jsx)
        module = opts.get("module", module)
        module_resolution = opts.get("moduleResolution", module_resolution)
    return ["--target", target, "--lib", ",".join(lib), "--jsx", jsx,
            "--module", module, "--moduleResolution", module_resolution, "--skipLibCheck"]


def _tsc_check(project_dir: str | Path, rel_path: str, content: str) -> tuple[bool, str]:
    """Best-effort TypeScript check via the project's own installed tsc.
    Deliberately does NOT fall back to `npx tsc` (which would silently try
    to download typescript over the network mid-repair-loop)."""
    project_dir = Path(project_dir)
    if not (project_dir / "node_modules").exists():
        return True, "lint skipped: node_modules not installed"
    tsc = project_dir / "node_modules" / ".bin" / "tsc"
    if not tsc.exists():
        return True, "lint skipped: typescript not installed in node_modules"

    target = project_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    had_original = target.exists()
    original = target.read_text(errors="replace") if had_original else None
    target.write_text(content)
    try:
        res = run([str(tsc), "--noEmit", *_tsc_compiler_flags(project_dir), str(target)],
                  cwd=project_dir, timeout=60)
    finally:
        if had_original:
            target.write_text(original)
        else:
            target.unlink(missing_ok=True)
    if res.exit_code == 127:
        return True, "lint skipped: tsc not available"
    if not res.ok:
        return False, f"tsc --noEmit failed for {rel_path}:\n{res.output}"
    return True, ""
