"""get_or_create(fresh=True): the agentic scratch must be recreated from
the REQUESTED base image even when a stale, mount-compatible container
for the project is already running (the bug that burned the first sweep
run: R16c kept exec'ing inside an old ubuntu container while the LLM's
commands hit 127s because `python` wasn't installed in it)."""
import subprocess
from pathlib import Path

import pytest

from atomic_forge import docker_env


@pytest.fixture
def fake_docker(monkeypatch):
    """A docker shim: `inspect` reports a running mount-compatible stale
    container whose image is ubuntu:24.04 (NOT what callers ask for)."""
    removed: list[str] = []
    state = {"status": "running"}

    def fake_run(cmd, *args, **kwargs):
        cmd = list(map(str, cmd))
        if cmd[:2] == ["docker", "inspect"]:
            fmt = cmd[cmd.index("-f") + 1]
            if "{{.State.Status}}" in fmt:
                out = state["status"]
            elif "Config.Image" in fmt:
                out = "ubuntu:24.04"
            elif "Home" in fmt or "Mounts" in fmt or "WorkingDir" in fmt:
                out = str(cmd[-1]) if "Home" in fmt else ("" if "Mounts" in fmt else str(Path("/tmp")))
            else:
                out = ""
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:3] == ["docker", "rm", "-f"]:
            removed.append(cmd[-1])
            state["status"] = ""
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["docker", "run", "-d"]:
            freshened["run_argv"].append(cmd)
            name = cmd[cmd.index("--name") + 1]
            state["status"] = "running"
            return subprocess.CompletedProcess(cmd, 0, name, "")
        if cmd[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    freshened = {"removed": removed, "run_argv": []}
    monkeypatch.setattr(docker_env, "docker_available", lambda: True)
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    return freshened


@pytest.fixture
def stale_running(monkeypatch):
    """The project has a mount-compatible RUNNING container."""
    monkeypatch.setattr(docker_env, "_inspect_state", lambda name: "running")
    monkeypatch.setattr(docker_env, "_mount_matches", lambda name, p: True)


def test_default_reuse_leaves_running_container(tmp_path, fake_docker, stale_running):
    d = tmp_path / "proj"
    d.mkdir()
    name = docker_env.get_or_create(d, "python:3.12-slim")
    assert name
    assert fake_docker["removed"] == []


def test_fresh_tears_down_stale_container(tmp_path, fake_docker, stale_running):
    d = tmp_path / "proj"
    d.mkdir()
    name = docker_env.get_or_create(d, "python:3.12-slim", fresh=True)
    assert name
    assert fake_docker["removed"], "a stale running container must be torn down"
    assert [c for c in fake_docker["removed"]][0] == name

def test_user_root_scratch_runs_root_without_dood(tmp_path, fake_docker, stale_running):
    """R16c scratch: root (apt/pip need it) and NO host docker socket
    (root + DooD inside an LLM-driven scratch = host control leak)."""
    d = tmp_path / "proj3"
    d.mkdir()
    docker_env.get_or_create(d, "python:3.12-slim", fresh=True, user_root=True)
    run = fake_docker["run_argv"][-1]
    assert "--user" not in run
    assert "/var/run/docker.sock" not in " ".join(run)


def test_default_container_keeps_privilege_and_dood(tmp_path, fake_docker, monkeypatch):
    d = tmp_path / "proj4"
    d.mkdir()
    monkeypatch.setattr(docker_env, "_inspect_state", lambda name: None)  # nothing exists yet
    docker_env.get_or_create(d, "python:3.12-slim")   # repair-loop default
    run = fake_docker["run_argv"][-1]
    assert "--user" in " ".join(run)
    assert "/var/run/docker.sock" in " ".join(run)
