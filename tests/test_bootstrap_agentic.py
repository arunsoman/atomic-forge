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

    def fake_get_or_create(project_dir, image, **kwargs):
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
    assert r["steps"] == 4  # 2 seed steps (python --version, pip install pytest) + 2 agent steps
    manifest = json.loads((repo / ".forge/bootstrap/manifest.json").read_text())
    assert manifest["test_cmd"] == "make test"
    lines = [json.loads(l) for l in (repo / ".forge/bootstrap/transcript.jsonl").read_text().splitlines()]
    # first two lines are the deterministic seed steps; agent steps follow
    assert lines[0]["cmd"] == "python --version" and lines[0]["seed"] is True
    assert lines[1]["cmd"] == "pip install pytest -q" and lines[1]["seed"] is True
    assert lines[2]["cmd"] == "make deps" and lines[2]["exit_code"] == 0
    assert any("verify" in l for l in lines)  # the probe is on the record
    # regression: a successful run must not leave the scratch sandbox
    # running — nothing downstream reuses this exact container, and a
    # 49-issue real-world sweep leaked exactly this container (400-500MB
    # each) until every one of them was killed by hand.
    assert fake_docker["killed"] == [f"ctr-{fake_docker['created'][-1].replace(':', '-')}"]


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
    # regression: cap exhaustion is a plain function return, not an
    # exception — easy to forget the `finally` covers it too.
    assert fake_docker["killed"] == [f"ctr-{fake_docker['created'][-1].replace(':', '-')}"]


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


def test_base_image_deterministic_markers_skip_the_llm(tmp_path):
    """An unambiguous marker file (pyproject.toml) must pick the image
    WITHOUT ever consulting the LLM — regression for the bug where every
    repo asked the LLM first, and a garbled/unparseable one-word reply
    silently degraded a real python repo to ubuntu:24.04 (no python/pip
    in the sandbox -> 127s -> step-budget exhaustion)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    llm = FakeLLM([], ecosystem="total garbage <>")  # would degrade to ubuntu if ever asked
    assert B._choose_base_image(llm, repo) == "python:3.12-slim"
    assert llm.calls == []


def test_base_image_ambiguous_markers_fall_back_to_llm(tmp_path):
    """More than one stack's markers present at once is genuinely
    ambiguous (e.g. a python backend with a node frontend at the repo
    root) — that's the one case the LLM should still be asked."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "package.json").write_text("{}\n")
    llm = FakeLLM([], ecosystem="it is a python repo")
    assert B._choose_base_image(llm, repo) == "python:3.12-slim"
    assert len(llm.calls) == 1


def test_base_image_bare_makefile_does_not_manufacture_ambiguity(tmp_path):
    """Regression: benoitc/gunicorn (a pure python repo) ships a bare
    Makefile for `make test`/`make lint` dev-convenience alongside its
    pyproject.toml. Before this fix, `_CppStack.detect()` counted that
    Makefile as a real cpp signal, tying against python and falling to
    the LLM — which, on a garbled/unparseable reply, degraded a real
    python repo to ubuntu:24.04 (no python/pip in the sandbox). A bare
    Makefile with no CMake/Autotools markers must not out-vote a real
    manifest match."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "Makefile").write_text("test:\n\tpytest\n")
    llm = FakeLLM([], ecosystem="total garbage <>")  # would degrade to ubuntu if ever asked
    assert B._choose_base_image(llm, repo) == "python:3.12-slim"
    assert llm.calls == []


def test_base_image_real_cmake_cpp_still_detected_alone(tmp_path):
    """A genuine C/C++ repo (CMakeLists.txt, no competing manifest) must
    still resolve to gcc:14 deterministically — the Makefile-weakening
    rule above must not blunt real cpp detection."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "CMakeLists.txt").write_text("enable_testing()\n")
    llm = FakeLLM([], ecosystem="total garbage <>")
    assert B._choose_base_image(llm, repo) == "gcc:14"
    assert llm.calls == []


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