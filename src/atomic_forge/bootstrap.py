"""
R16 environment-bootstrap (see the project wiki: 'Environment Bootstrap',
'Plan R6 R11 R16'):

  1. DETERMINISTIC GATE (`run_bootstrap_gate`): prove "at least one test in
     this repo is discoverable and executable" before any repair logic
     (CIE index, testgen, repair) runs against a checkout.
  2. AGENTIC FALLBACK (R16c, `agentic_bootstrap`): when no registered stack
     detects the repo (or its deterministic probe fails), a Repo2Run-style
     external LLM configurator (arXiv:2502.13681) proposes one setup
     command per step inside a Docker sandbox, snapshots each successful
     step (`docker commit`), and rolls failed steps back to the last good
     snapshot — hard-capped on steps, wall clock, and per-step timeouts.

Gate verdicts are checkpointed (""bootstrap"" phase + BootstrapVerdict on
the run record) — a failed gate is durable state, not a lost print().
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from .checkpoint import BootstrapVerdict, RunCheckpointer, new_run_id
from . import docker_env
from .stacks import detect_test_stack
from .sandbox import RunResult, run

#: Exit codes meaning "the test runner ran to its end": 0 = passed,
#: 1 = tests ran and some failed. Anything else (pytest's 2=interrupted/
#: 3=internal/4=usage, a shell's 127=not-found, the sentinel 124=timeout)
#: means the *environment* is broken — which is precisely what the gate
#: exists to catch. A failing ASSERTION is a working environment.
_COMPLETED_EXIT_CODES = {0, 1}

#: Agentic fallback is opt-in by env (it spends real LLM tokens and up to
#: wall_clock_s inside a Docker sandbox): FORGE_ENABLE_AGENTIC_BOOTSTRAP=1.
_AGENTIC_ENV = "FORGE_ENABLE_AGENTIC_BOOTSTRAP"

#: One cheap LLM call picks the sandbox base image from a FIXED menu —
#: no free-form image names a prompt could hallucinate into a pull.
_BASE_IMAGE_MENU = {
    "python": "python:3.12-slim",
    "node": "node:20",
    "java": "eclipse-temurin:17-jdk",
    "go": "golang:1.22",
    "rust": "rust:1-slim",
    "c": "gcc:14",
    "cpp": "gcc:14",
    "c++": "gcc:14",
    "unknown": "ubuntu:24.04",
}

_CONFIGURATOR_SYSTEM = """You are a build-environment configurator. A repository checkout is bind-mounted inside a Debian-based Docker container and currently fails to install, build, or test. Propose ONE shell command per turn to get its tests to run (install system deps, language deps, generate code, set env vars).

Respond with ONLY a JSON object:
  {"cmd": "<one shell command>", "why": "<10 words max>", "done": <bool>, "test_cmd": "<the repo's working test command, or null>"}

Rules:
- ONE command per turn; quiet/assume-yes package-manager flags.
- Always non-interactive (DEBIAN_FRONTEND=noninteractive, CI=true).
- When tests can run (pass or fail both count), set done=true and test_cmd
  to the exact command that runs them.
- Nothing destructive outside the checkout; never edit test expectations."""


def _evidence(result: RunResult) -> str:
    """One-line, human-readable reason this probe counts (or doesn't) as
    "a test actually executed". Evidence reporting, not the verdict — an
    unrecognized runner falls through to the honest exit-code line."""
    out = result.full_output
    for pattern in (
        r"collected (\d+) item",                # pytest
        r"Total Tests:\s*(\d+)",                # ctest
        r"(?:ok|FAIL)\s+\S+\s+[\d.]+s",         # go test
        r"test result:\s*\S+\.(\d+) passed",    # cargo test
        r"Test Files\s+\d+\s+\w+",              # vitest/jest
    ):
        m = re.search(pattern, out, re.IGNORECASE)
        if m:
            return f"runner output matched {pattern[:24]!r} ({m.group(0)[:60]!r})"
    if result.exit_code in _COMPLETED_EXIT_CODES and out.strip():
        first_line = next(iter(out.strip().splitlines()), "")[:80]
        return f"completed exit={result.exit_code}, output began: {first_line!r}"
    return f"no test-runner evidence (exit={result.exit_code}, timed_out={result.timed_out})"


def _agentic_enabled() -> bool:
    return os.environ.get(_AGENTIC_ENV, "0") == "1"


def _head_commit(project_dir: Path) -> Optional[str]:
    res = run(["git", "rev-parse", "HEAD"], cwd=project_dir, timeout=10)
    return res.full_output.strip() or None if res.exit_code == 0 else None


def _tree_listing(project_dir: Path, limit: int = 80) -> str:
    """Top-level entries (name + dir/file) for the configurator's first
    view — read-only, host-side, bounded."""
    try:
        kids = sorted(project_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return "(unreadable directory)"
    lines = []
    for k in kids[:limit]:
        if k.name.startswith("."):
            continue
        lines.append(f"{'d ' if k.is_dir() else 'f '}{k.name}")
    return "\n".join(lines) or "(empty)"


def _extract_json(text: str) -> Optional[dict]:
    """First JSON object in the model's reply; non-JSON replies are a
    skipped turn, not a crash."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def agentic_bootstrap(project_dir, llm, *, max_steps: int = 12,
                      wall_clock_s: int = 1200, per_step_timeout: int = 120,
                      verify_timeout: int = 600) -> dict:
    """R16c: bring an un-detectable/failed checkout to "at least one test
    discoverable and executable" — inside a Docker sandbox ONLY.

    Per Repo2Run (arXiv:2502.13681) but simpler: an external LLM
    configurator proposes ONE setup command per step; each successful step
    is snapshotted via `docker commit` (last-good image); a failed step
    rolls the scratch container back to the last good snapshot by
    re-creating from that image (never replaying commands). Hard caps
    (steps / wall clock / per-step timeout) bound runaway spend. Every
    step lands in `<project>/.forge/bootstrap/transcript.jsonl`; success
    also writes `manifest.json` keyed by HEAD commit, so a later run of
    the same commit skips the loop entirely (bootstrap cache hit).

    Never executes on the host: without Docker this returns
    `unsupported_ecosystem` with a clear detail string.

    Returns {"ok", "verdict", "detail", "cmd", "steps", "image",
             "transcript", "manifest_path"}.
    """
    project_dir = Path(project_dir)
    bdir = project_dir / ".forge" / "bootstrap"
    bdir.mkdir(parents=True, exist_ok=True)
    transcript_path = bdir / "transcript.jsonl"
    manifest_path = bdir / "manifest.json"

    def _fail(verdict: str, detail: str, *, steps: int = 0,
              cmd: Optional[str] = None, image: Optional[str] = None) -> dict:
        return {"ok": False, "verdict": verdict, "detail": detail, "cmd": cmd,
                "steps": steps, "image": image, "transcript": str(transcript_path),
                "manifest_path": str(manifest_path)}

    def _log(step: dict) -> None:
        with transcript_path.open("a") as fh:
            fh.write(json.dumps(step) + "\n")

    if not docker_env.docker_available():
        return _fail(BootstrapVerdict.UNSUPPORTED_ECOSYSTEM.value,
                     "agentic bootstrap requires Docker (host execution is "
                     "deliberately never attempted); install Docker and leave "
                     "FORGE_DISABLE_DOCKER_TESTS unset")

    # ---- bootstrap cache: a prior successful run of this exact commit ----
    head = _head_commit(project_dir)
    if head and manifest_path.exists():
        try:
            cached = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("commit") == head and cached.get("test_cmd"):
            return {"ok": True, "verdict": BootstrapVerdict.BOOTSTRAPPED.value,
                    "detail": f"bootstrap cache hit for commit {head[:8]} "
                              f"({cached.get('steps', '?')} steps previously)",
                    "cmd": cached.get("test_cmd"), "steps": cached.get("steps", 0),
                    "image": cached.get("image"), "transcript": str(transcript_path),
                    "manifest_path": str(manifest_path)}

    base_image = _choose_base_image(llm, project_dir)
    container = docker_env.get_or_create(project_dir, base_image)
    if container is None:
        return _fail(BootstrapVerdict.UNSUPPORTED_ECOSYSTEM.value,
                     f"could not start a Docker sandbox ({base_image}) — is the "
                     "daemon reachable?")

    last_good_image = base_image
    steps = 0
    started = time.monotonic()
    messages = [
        {"role": "system", "content": _CONFIGURATOR_SYSTEM},
        {"role": "user", "content": f"# Repository (top level)\n{_tree_listing(project_dir)}\n\n"
                                    "Propose the first setup command as JSON."},
    ]

    while steps < max_steps and (time.monotonic() - started) < wall_clock_s:
        try:
            reply = llm.chat(messages, temperature=0.0, max_tokens=400)
        except Exception as e:  # noqa: BLE001 - LLM failure ends the loop, capped
            return _fail(BootstrapVerdict.FAILED_AGENTIC.value,
                         f"configurator LLM failed after {steps} steps: {e}",
                         steps=steps, image=last_good_image)
        steps += 1
        proposal = _extract_json(reply)
        if proposal is None or not proposal.get("cmd"):
            messages.append({"role": "assistant", "content": (reply or "")[:500]})
            messages.append({"role": "user",
                             "content": "Malformed reply. Respond with ONLY the JSON "
                                        "object: {\"cmd\": ..., \"why\": ..., \"done\": ..., "
                                        "\"test_cmd\": ...}"})
            continue

        cmd = str(proposal.get("cmd"))
        result = docker_env.exec_in(container, cmd, cwd=project_dir,
                                    timeout=per_step_timeout,
                                    env={"CI": "true",
                                         "DEBIAN_FRONTEND": "noninteractive"})
        _log({"step": steps, "cmd": cmd, "exit_code": result.exit_code,
              "timed_out": result.timed_out,
              "output_tail": (result.full_output or "")[-2000:]})

        # ---- verify the gate from inside the SAME container ---------------
        stack = detect_test_stack(project_dir)
        verify_cmd = stack.cmd if stack else proposal.get("test_cmd")
        probe = None
        if verify_cmd:
            probe = docker_env.exec_in(container, verify_cmd, cwd=project_dir,
                                       timeout=verify_timeout, env={"CI": "true"})
            _log({"step": steps, "verify": verify_cmd, "exit_code": probe.exit_code,
                  "timed_out": probe.timed_out,
                  "output_tail": (probe.full_output or "")[-2000:]})
        verified = (probe is not None and not probe.timed_out
                    and probe.exit_code in _COMPLETED_EXIT_CODES
                    and bool(probe.full_output.strip()))

        if verified:
            manifest = {"commit": head, "base_image": base_image,
                        "image": last_good_image, "test_cmd": verify_cmd,
                        "steps": steps}
            manifest_path.write_text(json.dumps(manifest, indent=2))
            return {"ok": True, "verdict": BootstrapVerdict.BOOTSTRAPPED.value,
                    "detail": (f"agentic bootstrap succeeded in {steps} step(s); "
                               f"verified test command: {verify_cmd}"),
                    "cmd": verify_cmd, "steps": steps, "image": last_good_image,
                    "transcript": str(transcript_path),
                    "manifest_path": str(manifest_path)}

        failed = result.timed_out or result.exit_code not in _COMPLETED_EXIT_CODES
        if failed:
            # rollback: re-create the scratch container from the last good
            # SNAPSHOT IMAGE (no command replay — atomic per Repo2Run).
            docker_env.kill(container)
            container = docker_env.get_or_create(project_dir, last_good_image)
            if container is None:
                return _fail(BootstrapVerdict.FAILED_AGENTIC.value,
                             f"rollback to {last_good_image} failed after step {steps}",
                             steps=steps, image=last_good_image)

        messages.append({"role": "assistant", "content": reply[:800]})
        last_block = (probe or result).full_output or ""
        tail = last_block[-4000:]
        messages.append({"role": "user",
                         "content": (f"# Last command\n{cmd}\n\n# Exit\n"
                                     f"{probe.exit_code if probe else result.exit_code}"
                                     f"{' (timed out)' if (probe and probe.timed_out) or result.timed_out else ''}"
                                     f"\n\n# Output (tail)\n{tail}\n\n"
                                     + ("Setup verified — you may stop." if verified
                                        else "Not bootstrapped yet. Next setup command as JSON."))})
        # bounded conversation: system + first user + last 10 messages
        if len(messages) > 12:
            messages[:] = [messages[0]] + messages[-10:]

    return _fail(BootstrapVerdict.FAILED_AGENTIC.value,
                 f"cap reached without a verified test run "
                 f"(steps={steps}, wall={int(time.monotonic() - started)}s)",
                 steps=steps, image=last_good_image)


def agentic_report_to_gate_report(report: dict) -> dict:
    """Adapt an agentic_bootstrap report into the gate's report shape."""
    return {"ok": bool(report.get("ok")),
            "verdict": report["verdict"],
            "detail": report["detail"],
            "cmd": report.get("cmd"),
            "evidence": report.get("detail", ""),
            "transcript": report.get("transcript"),
            "manifest_path": report.get("manifest_path")}


def run_bootstrap_gate(project_dir, *, timeout: int = 600, on_progress=None,
                       db_path: Optional[Path] = None, llm=None,
                       allow_agentic: bool = False) -> dict:
    """The gate. Returns a report dict:

    {"ok": bool, "verdict": <BootstrapVerdict value>, "detail": str,
     "cmd": str | None, "evidence": str, "checkpoint_run_id": str}

    `ok=True` iff verdict "bootstrapped". `on_progress` is the same
    (phase, status, detail) callback `sandbox.run_test_with_progress`
    takes. `db_path` overrides the checkpoint store (tests). When
    `llm` + `allow_agentic` (+ FORGE_ENABLE_AGENTIC_BOOTSTRAP=1) are set,
    the Repo2Run-style fallback runs after any deterministic failure.
    """
    from .sandbox import run_test_with_progress  # lazy: avoids a docker_env cycle at import

    project_dir = str(Path(project_dir))
    checkpointer = RunCheckpointer(run_id=new_run_id(), project="bootstrap",
                                   project_dir=project_dir, db_path=db_path)
    checkpointer.mark_phase("bootstrap")

    def _record(ok: bool, verdict: BootstrapVerdict, detail: str,
                cmd: Optional[str] = None, evidence: str = "") -> dict:
        checkpointer.mark_phase("bootstrap", status="passed" if ok else "failed")
        checkpointer.mark_bootstrap(verdict.value, detail)
        return {"ok": ok, "verdict": verdict.value, "detail": detail,
                "cmd": cmd, "evidence": evidence,
                "checkpoint_run_id": checkpointer.record.run_id}

    stack = detect_test_stack(project_dir)
    if stack is not None:
        result = run_test_with_progress(stack, project_dir, timeout=timeout,
                                        on_progress=on_progress)
        evidence = _evidence(result)
        if result.timed_out:
            det_detail = f"test probe did not complete within {timeout}s"
        elif result.exit_code not in _COMPLETED_EXIT_CODES:
            det_detail = (f"test command exited {result.exit_code} — a runner "
                          f"crash or misuse, not a completed test run")
        elif not result.full_output.strip():
            det_detail = "probe produced no output — nothing demonstrably ran"
        else:
            return _record(True, BootstrapVerdict.BOOTSTRAPPED,
                           f"{stack.cmd} executed: {evidence}", stack.cmd, evidence)
    else:
        det_detail = ("no registered stack detected testable markers "
                      "(python/node/java/go/rust/c-c++)")

    # ---- deterministic path failed: Repo2Run-style fallback (opt-in) ----
    if llm is None or not allow_agentic or not _agentic_enabled():
        verdict = (BootstrapVerdict.UNSUPPORTED_ECOSYSTEM if stack is None
                   else BootstrapVerdict.FAILED_DETERMINISTIC)
        detail = (det_detail + "; the agentic fallback (R16c) would attempt this "
                  "checkout (enable with FORGE_ENABLE_AGENTIC_BOOTSTRAP=1)")
        return _record(False, verdict, detail, stack.cmd if stack else None)

    agentic = agentic_bootstrap(project_dir, llm)
    if agentic["ok"]:
        evidence = f"agentic: {agentic['cmd']} ({agentic['steps']} steps)"
        return _record(True, BootstrapVerdict.BOOTSTRAPPED, agentic["detail"],
                       agentic.get("cmd"), evidence)
    verdict = BootstrapVerdict(agentic["verdict"])
    detail = (f"deterministic: {det_detail}; agentic: {agentic['detail']} "
              f"[transcript: {agentic['transcript']}]")
    return _record(False, verdict, detail, agentic.get("cmd"))


def _choose_base_image(llm, project_dir: Path) -> str:
    """One cheap constrained call: which sandbox image fits this repo?
    Any failure or nonsense reply degrades to a generic Debian/Ubuntu base
    that the configurator can apt-install from — never a free-form image
    name a prompt could hallucinate."""
    entries = sorted(
        k.name for k in project_dir.iterdir() if not k.name.startswith(".")
    ) if project_dir.is_dir() else []
    prompt = ("Which language ecosystem is this repository? Answer with ONE "
              f"word (python/node/java/go/rust/c/cpp/c++/unknown).\n"
              f"Top-level entries: {', '.join(entries[:60])}")
    try:
        reply = (llm.chat([{"role": "user", "content": prompt}],
                          temperature=0.0, max_tokens=8) or "").lower()
    except Exception:  # noqa: BLE001 - image choice must never raise
        reply = ""
    for key in _BASE_IMAGE_MENU:
        if re.search(rf"\b{re.escape(key)}\b", reply):
            return _BASE_IMAGE_MENU[key]
    return _BASE_IMAGE_MENU["unknown"]


def _tree_listing(project_dir: Path, limit: int = 60) -> str:
    try:
        names = sorted(k.name for k in project_dir.iterdir()
                       if not k.name.startswith("."))
    except OSError:
        return "(unreadable)"
    return "\n".join(f"- {n}" for n in names[:limit]) or "(empty)"