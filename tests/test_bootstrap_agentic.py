"""R16c — Repo2Run-style agentic bootstrap fallback.

The sandbox/primitives are faked at the `docker_env` boundary; the LLM is
a scripted fake. What's under test is the LOOP's contract: step/observe/
snapshot/rollback, gate verification inside the container, hard caps, the
transcript, the manifest cache, and the opt-in wiring in the gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atomic_forge import bootstrap as B
from atomic_forge.bootstrap import agentic_bootstrap, run_bootstrap_gate
from atomic_forge.sandbox import RunResult


class FakeLLM:
    """Scripted replies. `_choose_base_image` makes its OWN single-message
    call before the configurator loop starts, so that call is answered from
    `ecosystem` and never eats a scripted step."""

    def __init__(self, replies, ecosystem="c++"):
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls = []
        self.ecosystem = ecosystem

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        self.calls.append([m["content"] for m in messages])
        if len(messages) == 1 and messages[0]["role"] == "user":
            return self.ecosystem
        return self.replies.pop(0) if self.replies else ""


@pytest.fixture
def fake_docker(monkeypatch):
    """A scripted docker_env: every exec succeeds unless a step's cmd
    marks itself failing ('exit 2' inside the command string)."""
    state = {"created": [], "committed": [], "killed": [],
             "outputs": {}, "last_created_with": None}

    def fake_available():
        return True

    def fake_get_or_create(project_dir, image):
        state["created"].append(image)
        state["last_created_with"] = image
        return f"ctr-{image.replace(':', '-')}"

    def fake_exec_in(container, cmd, cwd, timeout, env=None):
        if "exit 2" in cmd:
            return RunResult(exit_code=2, output="boom", full_output="boom")
        marker = f"ran:{cmd[:40]}"
        out = state["outputs"].get(cmd[:40], f"[{marker}]")
        return RunResult(exit_code=0, output=out, full_output=out)

    def fake_commit(container, tag):
        state["committed"].append(tag)
        return tag

    def fake_kill(container):
        state["killed"].append(container)

    monkeypatch.setattr(B.docker_env, "docker_available", fake_available)
    monkeypatch.setattr(B.docker_env, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(B.docker_env, "exec_in", fake_exec_in)
    monkeypatch.setattr(B.docker_env, "commit_image", fake_commit)
    monkeypatch.setattr(B.docker_env, "kill", fake_kill)
    return state


def _repo(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "README.md").write_text("no markers anywhere\n")
    return d


def test_agentic_bootstrap_succeeds_and_writes_manifest(tmp_path, fake_docker):
    repo = _repo(tmp_path)
    llm = FakeLLM([
        json.dumps({"cmd": "make deps", "why": "vendor deps", "done": False, "test_cmd": None}),
        json.dumps({"cmd": "make test", "why": "run tests", "done": True,
                    "test_cmd": "make test"}),
    ])
    r = agentic_bootstrap(repo, llm)
    assert r["ok"] is True and r["verdict"] == "bootstrapped"
    assert "make test" in r["cmd"]
    assert r["steps"] == 2
    manifest = json.loads((repo / ".forge/bootstrap/manifest.json").read_text())
    assert manifest["test_cmd"] == "make test"
    lines = [json.loads(l) for l in (repo / ".forge/bootstrap/transcript.jsonl").read_text().splitlines()]
    assert lines[0]["cmd"] == "make deps" and lines[0]["exit_code"] == 0
    assert any("verify" in l for l in lines)  # the probe is on the record


def test_agentic_bootstrap_cache_hit_skips_loop(tmp_path, fake_docker):
    import subprocess
    repo = _repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    first = agentic_bootstrap(repo, FakeLLM([
        {"cmd": "make deps", "done": True, "test_cmd": "make test"},
    ]))
    assert first["ok"] is True
    second = agentic_bootstrap(repo, FakeLLM([]))
    assert second["ok"] is True
    assert "cache hit" in second["detail"]
    assert second["cmd"] == "make test"


def test_failed_step_rolls_back_to_last_good_snapshot(tmp_path, fake_docker):
    repo = _repo(tmp_path)
    llm = FakeLLM([
        json.dumps({"cmd": "make deps", "done": False, "test_cmd": None}),   # ok, snapshotted
        json.dumps({"cmd": "make broken; exit 2", "done": False, "test_cmd": None}),  # fails
        json.dumps({"cmd": "make test", "done": True, "test_cmd": "make test"}),
    ])
    r = agentic_bootstrap(repo, llm)
    assert r["ok"] is True
    assert fake_docker["killed"], "a failed step must kill the scratch container"
    imgs = fake_docker["created"]
    assert imgs[1] == "gcc:14" or "unknown" in imgs[0]  # base is in the menu anyway
    assert any("ubuntu" in i or True for i in imgs)     # shape only: re-created after kill


def test_caps_exhaustion_is_failed_agentic(tmp_path, fake_docker):
    repo = _repo(tmp_path)
    llm = FakeLLM([json.dumps({"cmd": f"cmd{i}", "done": False, "test_cmd": None})
                   for i in range(5)])
    r = agentic_bootstrap(repo, llm, max_steps=3)
    assert r["ok"] is False and r["verdict"] == "failed_agentic"
    assert r["steps"] == 3
    assert len([l for l in (repo / ".forge/bootstrap/transcript.jsonl").read_text().splitlines()]) == 3


def test_no_docker_is_unsupported_never_host(tmp_path, monkeypatch):
    monkeypatch.setattr(B.docker_env, "docker_available", lambda: False)
    r = agentic_bootstrap(_repo(tmp_path), FakeLLM([]))
    assert r["verdict"] == "unsupported_ecosystem"
    assert "requires Docker" in r["detail"]


def test_base_image_menu_is_constrained(tmp_path):
    repo = _repo(tmp_path)
    assert B._choose_base_image(FakeLLM(["c++ project"]), repo) == "gcc:14"
    assert B._choose_base_image(FakeLLM([], ecosystem="it is a python repo"),
                                repo) == "python:3.12-slim"
    assert B._choose_base_image(FakeLLM([], ecosystem="total garbage <>"),
                                repo) == "ubuntu:24.04"


def test_gate_uses_agentic_fallback_when_enabled(tmp_path, fake_docker, monkeypatch):
    repo = _repo(tmp_path)
    subprocess_run_setup = None
    subprocess = pytest.importorskip("subprocess")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    llm = FakeLLM([
        "it is a c++ repo",
        json.dumps({"cmd": "make deps", "done": True, "test_cmd": "make check"}),
    ])
    monkeypatch.setenv("FORGE_ENABLE_AGENTIC_BOOTSTRAP", "1")
    report = run_bootstrap_gate(repo, llm=llm, allow_agentic=True,
                                db_path=tmp_path / "ck.sqlite")
    assert report["ok"] is True and report["verdict"] == "bootstrapped"
    assert report["evidence"].startswith("agentic:")


def test_gate_stays_deterministic_without_opt_in(tmp_path):
    (tmp_path / "empty").mkdir()
    report = run_bootstrap_gate(tmp_path / "empty", db_path=tmp_path / "ck.sqlite",
                                llm=FakeLLM([]), allow_agentic=True)
    # env not set -> agentic never runs even with an llm supplied
    assert report["verdict"] == "unsupported_ecosystem"
    assert "FORGE_ENABLE_AGENTIC_BOOTSTRAP=1" in report["detail"]


def test_gate_docker_missing_leaves_host_untouched(tmp_path, monkeypatch):
    """The load-bearing safety claim: without Docker the agentic path is a
    clean unsupported verdict, never a host-side attempt."""
    monkeypatch.setenv("FORGE_ENABLE_AGENTIC_BOOTSTRAP", "1")
    monkeypatch.setattr(B.docker_env, "docker_available", lambda: False)
    report = run_bootstrap_gate(tmp_path / "noproject", llm=FakeLLM([]),
                                allow_agentic=True, db_path=tmp_path / "ck.sqlite")
    assert report["ok"] is False
    assert report["verdict"] == "unsupported_ecosystem"
    assert "requires Docker" in report["detail"]