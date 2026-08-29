"""Per-project, per-stack Docker test execution.

Why this exists: `sandbox.run()` executes test commands as a bare host
subprocess, assuming whatever toolchain a target project needs (Maven,
Gradle, a specific Python/Node runtime) is already installed on this
host — often false for a real project whose stack the host was never
provisioned for. This module runs the test command inside a per-project
Docker container instead, so forge never has to touch host tooling.

Lifecycle: one persistent container per project, lazily created on first
use, reused across every `exec_in` call for that project's whole session
(a single repair run can call this many times — re-downloading
Maven/npm/Gradle dependencies on every call would be far too slow), torn
down via `prune()` once the caller is done with that project.

`--user {uid}:{gid}` + a bind-mounted, project-scoped HOME directory: lets
Maven/Gradle/npm write their package caches (`~/.m2`, `~/.gradle`, `~/.npm`)
as the invoking host user, not root, into files that persist on host disk
across `exec_in` calls without polluting the project's own working tree
(which `sandbox.commit()` `git add -A`s wholesale after every repair round).

`/var/run/docker.sock` is bind-mounted into every test container
(Docker-outside-of-Docker, not nested Docker-in-Docker) because real-world
JVM/Node test suites commonly use Testcontainers to launch their own
sibling containers (Postgres/Redis/Kafka) for integration coverage. The
test container only needs the `docker` CLI + socket access to ask the SAME
host daemon for those sibling containers; it never needs its own daemon.

Docker support is optional: `sandbox.run_test()` degrades to a bare host
subprocess whenever Docker isn't installed/reachable, or `FORGE_DISABLE_
DOCKER_TESTS=1` is set.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from .sandbox import RunResult, truncate

_CONTAINER_PREFIX = "forge-test-"
#: Deliberately NOT nested under a project's own workspace dir — see
#: module docstring's git-add-A reasoning.
_HOME_ROOT = Path("./forge_docker_homes")

_docker_available_cache: Optional[bool] = None
_locks_guard = threading.Lock()
_project_locks: dict[str, threading.Lock] = {}

#: SF2: an in-flight-call counter per container, guarded by
#: `_exec_in_flight_guard` — NOT a lock held across the whole exec_in()
#: duration (that would serialize every concurrent exec against the same
#: container, killing the throughput the review flagged as a real risk).
#: Used only to decide, on a client-side timeout, whether it's safe to
#: `docker kill` the shared container: previously that kill ran
#: unconditionally, destroying the container mid-run for any OTHER
#: exec_in call still legitimately executing against it.
_exec_in_flight_guard = threading.Lock()
_exec_in_flight: dict[str, int] = {}


def _enter_exec(container: str) -> None:
    with _exec_in_flight_guard:
        _exec_in_flight[container] = _exec_in_flight.get(container, 0) + 1


def _exit_exec(container: str) -> None:
    with _exec_in_flight_guard:
        remaining = _exec_in_flight.get(container, 1) - 1
        if remaining <= 0:
            _exec_in_flight.pop(container, None)
        else:
            _exec_in_flight[container] = remaining


def _others_still_in_flight(container: str) -> bool:
    """Called from exec_in()'s own timeout branch, before that call's own
    `_exit_exec` has run yet — so this call's own increment is still
    counted and must be subtracted."""
    with _exec_in_flight_guard:
        return _exec_in_flight.get(container, 1) - 1 > 0


def docker_available() -> bool:
    """Cached for the process lifetime — `docker info` is a real daemon
    round trip, not worth repeating on every test-run call. Set
    FORGE_DISABLE_DOCKER_TESTS=1 to force host-subprocess execution
    regardless of what's actually installed (e.g. a deploy host that has
    a `docker` CLI on PATH but no socket access worth attempting)."""
    global _docker_available_cache
    if _docker_available_cache is not None:
        return _docker_available_cache
    if os.environ.get("FORGE_DISABLE_DOCKER_TESTS", "").strip() == "1":
        _docker_available_cache = False
        return False
    if shutil.which("docker") is None:
        _docker_available_cache = False
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10,
        )
        _docker_available_cache = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        _docker_available_cache = False
    return _docker_available_cache


def _container_name(project_id: str) -> str:
    return f"{_CONTAINER_PREFIX}{project_id}"


def _home_dir(project_id: str) -> Path:
    path = _HOME_ROOT / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _project_lock(project_id: str) -> threading.Lock:
    """One lock per project_id, double-checked against a small guard lock
    for the dict itself. Guards against two concurrent callers for the
    same project_id both seeing "no container running yet" and racing
    `docker run --name` for the same name."""
    lock = _project_locks.get(project_id)
    if lock is not None:
        return lock
    with _locks_guard:
        lock = _project_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _project_locks[project_id] = lock
        return lock


def _inspect_state(name: str) -> Optional[str]:
    """"running" / "exited" (or any other docker State.Status value) / None
    if the container doesn't exist at all."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _mount_matches(name: str, project_dir: Path) -> bool:
    """True if `name` has project_dir bind-mounted at the identical path
    (what get_or_create always creates). project_id is only derived from
    project_dir's own BASENAME (see get_or_create's docstring) — safe for
    every real caller following a stable {workspace_root}/{project_id} path,
    but two DIFFERENT absolute paths can share a basename outside that
    convention (confirmed live: pytest's own tmp_path fixture reuses
    generic subdirectory names like "project" across unrelated test
    runs). Without this check, a stale container from an unrelated path
    would get silently reused by name alone, mounting the WRONG
    directory — `docker exec -w {new_path}` then fails outright since
    that path was never mounted into the old container at all."""
    target = str(project_dir)
    template = '{{range .Mounts}}{{if eq .Destination "' + target + '"}}match{{end}}{{end}}'
    result = subprocess.run(
        ["docker", "inspect", "-f", template, name],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode == 0 and "match" in result.stdout


def get_or_create(project_dir: Path, image: str) -> Optional[str]:
    """Returns the running container name for project_dir's project, or
    None if Docker isn't usable at all (caller should fall back to a plain
    host run()). project_id is derived from project_dir's own basename —
    every real caller already follows a stable {workspace_root}/{project_id}
    convention,
    a stable 1:1 mapping to one absolute path; _mount_matches guards the
    general case where that assumption doesn't hold (see its own
    docstring)."""
    if not docker_available():
        return None
    project_dir = Path(project_dir).resolve()
    project_id = project_dir.name
    name = _container_name(project_id)

    with _project_lock(project_id):
        state = _inspect_state(name)
        if state == "running" and _mount_matches(name, project_dir):
            return name
        if state is not None:
            # Either exited/crashed from a prior run (unknown state,
            # don't blindly `docker start` it), or running but bind-
            # mounted at a different path than this project_dir (see
            # _mount_matches) — either way, remove and recreate clean.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)

        home_dir = _home_dir(project_id)
        uid, gid = os.getuid(), os.getgid()
        run_cmd = [
            "docker", "run", "-d", "--name", name,
            "--user", f"{uid}:{gid}",
            "-e", f"HOME={home_dir}",
            "-v", f"{project_dir}:{project_dir}",
            "-v", f"{home_dir}:{home_dir}",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-w", str(project_dir),
            image, "sleep", "infinity",
        ]
        result = subprocess.run(run_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            if "already in use" in (result.stderr or ""):
                # A different process won the race between our inspect and
                # our run (defense in depth — the per-project lock above
                # already closes this for same-process threads). Whatever
                # it created is a correctly-configured container for this
                # same project_id/image; reuse it.
                if _inspect_state(name) == "running":
                    return name
            return None
        return name


def exec_in(container: str, cmd: str, cwd: Path, timeout: int,
            env: Optional[dict] = None) -> RunResult:
    """docker exec -w {cwd} {container} sh -c {cmd}. List-form subprocess
    argv throughout — cmd is never re-embedded into an outer shell string,
    avoiding double-escaping the multi-stack combined command shape
    cre/exec.py::detect_test_stack can produce for a full-stack repo.

    `env` is forwarded as `-e KEY=VAL` pairs (docker exec doesn't inherit
    the *host* subprocess's env — the container has its own — so this is
    the only way to hand a test command CI=true, see cre/exec.py's
    _TEST_ENV docstring for why that matters).

    SF2: no lock is held across the exec itself — that would serialize
    every concurrent exec against a shared container, which is real
    throughput two callers legitimately want at once. Instead, an
    in-flight counter (`_enter_exec`/`_exit_exec`) tracks how many
    exec_in() calls are currently running against THIS container; on a
    timeout, the container is only killed if no OTHER call is still
    in-flight for it — previously the kill ran unconditionally, so one
    call's timeout could destroy the entire container mid-run for any
    other still-in-flight call."""
    env_flags = [f for k, v in (env or {}).items() for f in ("-e", f"{k}={v}")]
    argv = ["docker", "exec", "-w", str(cwd), *env_flags, container, "sh", "-c", cmd]
    _enter_exec(container)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        if not out.strip():
            out = "[command ran successfully and produced no output]"
        return RunResult(exit_code=proc.returncode, output=truncate(out), full_output=out)
    except subprocess.TimeoutExpired as e:
        # Confirmed live: killing the local `docker exec` client on timeout
        # does NOT stop the process running inside the container — it
        # would otherwise keep running and race the NEXT round's exec_in
        # call against a still-live previous one on the same shared cache
        # dir. Best-effort — a failed kill here shouldn't mask the
        # original timeout result. Only kill if no OTHER exec_in is
        # currently in-flight for this container (SF2).
        if not _others_still_in_flight(container):
            kill(container)
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + f"\n[TIMEOUT after {timeout}s]"
        return RunResult(exit_code=124, output=truncate(out), full_output=out, timed_out=True)
    except FileNotFoundError as e:
        return RunResult(exit_code=127, output=f"[command not found: {e}]")
    finally:
        _exit_exec(container)


def commit_image(container: str, tag: str) -> Optional[str]:
    """Snapshot a container's filesystem+state into a reusable image (the
    R16c agentic bootstrap's atomic-configuration synthesis, and Cells'
    baked base image). Returns the tag, or None on failure — callers treat
    None as "no snapshot available" and fall back to the base image.
    Never raises."""
    try:
        result = subprocess.run(
            ["docker", "commit", container, tag], capture_output=True, text=True, timeout=120,
        )
        return tag if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def kill(container: str) -> None:
    subprocess.run(["docker", "kill", container], capture_output=True, timeout=30)


def prune(project_id: str) -> None:
    """Best-effort teardown — never raises. Called once a project's whole
    workspace job is done."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", _container_name(project_id)],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        shutil.rmtree(_HOME_ROOT / project_id, ignore_errors=True)
    except OSError:
        pass
