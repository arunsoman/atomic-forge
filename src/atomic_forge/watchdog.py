"""
Production watchdog: detect a live failure -> repair it with the same
SOTA repair machinery `repair_agent` already implements -> deploy the fix
as a canary -> promote or roll back on real health-check evidence.

Two protocols, one real reference implementation each — the same split
`tools.ToolBackend`/`reporter.Reporter` already use in this codebase:

  - `FailureDetector.poll()` -> list[FailureSignal]. Reference impl:
    `LogFailureDetector`, which tails a log file for Python tracebacks,
    reuses `repair_agent.extract_signals` to parse them (no second
    traceback parser to keep in sync), and dedupes by a fingerprint so a
    steadily-repeating crash doesn't refire the loop every poll.
  - `DeployTarget` — `deploy`/`health`/`shift_traffic`/`promote`/
    `rollback`/`teardown`. Reference impl: `LocalProcessCanaryDeployer`,
    which runs stable and canary as real subprocesses on real ports,
    splits real HTTP traffic between them via a small stdlib reverse
    proxy, and health-checks the canary over real HTTP before promoting.
    Bring your own richer target (Kubernetes, a real load balancer) by
    implementing the same protocol.

`WatchdogLoop` wires the two together with the repair loop's own
localize/sample/select machinery (`repair_agent.extract_signals`,
`.localize`, `._attempt_patch`) — a live failure's traceback text is fed
through the exact same signal-extraction and localization the local
repair loop uses against pytest output; the only thing that changes is
the pass/fail oracle: with no local test suite for a production
failure, the canary's own health check IS the oracle.
"""
from __future__ import annotations

import hashlib
import random
import shutil
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional, Protocol
import subprocess

from .llm import ChatLLM
from .repair_agent import _attempt_patch, _blast_radius_violations, extract_signals, localize
from .sandbox import commit
from .tools import ToolBackend
from .trajectory import Trajectory

# ============================================================ detection ====


@dataclass
class FailureSignal:
    source: str                    # e.g. "log:/var/log/app.log"
    message: str                   # short, human-readable summary
    raw: str                       # the full text handed to extract_signals
    severity: str = "error"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = hashlib.sha256(self.raw.encode("utf-8", "replace")).hexdigest()[:16]


class FailureDetector(Protocol):
    def poll(self) -> list[FailureSignal]: ...


_TRACEBACK_RE_START = "Traceback (most recent call last):"


class LogFailureDetector:
    """Tails one log file for Python tracebacks. Real, dependency-free,
    stateful: remembers its read offset (survives across `poll()` calls
    in the same process) and a set of already-handled fingerprints (so
    the same crash logged repeatedly — a request hit in a retry loop, a
    cron job re-running — surfaces once, not once per poll)."""

    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self._offset = 0
        self._seen: set[str] = set()

    def poll(self) -> list[FailureSignal]:
        if not self.log_path.is_file():
            return []
        with self.log_path.open("r", errors="replace") as f:
            f.seek(self._offset)
            new_text = f.read()
            self._offset = f.tell()
        if _TRACEBACK_RE_START not in new_text:
            return []
        signals: list[FailureSignal] = []
        for block in _split_tracebacks(new_text):
            sig = extract_signals(block)
            summary = f"{', '.join(sig.exception_types) or 'error'} in {', '.join(sig.traceback_paths[:1]) or 'unknown file'}"
            fs = FailureSignal(source=f"log:{self.log_path}", message=summary, raw=block)
            if fs.fingerprint in self._seen:
                continue
            self._seen.add(fs.fingerprint)
            signals.append(fs)
        return signals


def _split_tracebacks(text: str) -> list[str]:
    """Split a chunk of log text into individual traceback blocks, each
    starting at "Traceback (most recent call last):" and running to the
    next blank line or the next traceback header."""
    lines = text.split("\n")
    blocks: list[list[str]] = []
    current: Optional[list[str]] = None
    for line in lines:
        if line.strip() == _TRACEBACK_RE_START:
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            if line.strip() == "" and len(current) > 1:
                blocks.append(current)
                current = None
            else:
                current.append(line)
    if current is not None:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


# ============================================================= deploy ====


class DeployTarget(Protocol):
    def deploy(self, source_dir: str, role: str) -> str: ...
    def health(self, deployment_id: str) -> bool: ...
    def shift_traffic(self, canary_id: str, percent: int) -> None: ...
    def promote(self, canary_id: str) -> None: ...
    def rollback(self, canary_id: str) -> None: ...
    def teardown(self, deployment_id: str) -> None: ...


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


@dataclass
class _Deployment:
    id: str
    role: str
    port: int
    proc: subprocess.Popen
    work_dir: Path


class _ProxyHandler(BaseHTTPRequestHandler):
    #: Set by _make_proxy_server per-instance via a closure-captured state
    #: dict (see below) — class attribute placeholders only.
    state: dict = {}

    def _forward(self) -> None:
        state = self.state
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        percent = state.get("canary_percent", 0)
        use_canary = state.get("canary_port") is not None and random.randint(1, 100) <= percent
        target_port = state["canary_port"] if use_canary else state["stable_port"]
        url = f"http://127.0.0.1:{target_port}{self.path}"
        req = urllib.request.Request(url, data=body, method=self.command, headers=dict(self.headers))
        try:
            with urllib.request.urlopen(req, timeout=state.get("timeout", 5.0)) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read() if e.fp else b"")
        except OSError:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"bad gateway")

    def do_GET(self): self._forward()      # noqa: N802 - BaseHTTPRequestHandler API
    def do_POST(self): self._forward()     # noqa: N802
    def do_PUT(self): self._forward()      # noqa: N802
    def do_DELETE(self): self._forward()   # noqa: N802

    def log_message(self, fmt, *args) -> None:  # silence stdlib access logging
        pass


class LocalProcessCanaryDeployer:
    """Reference `DeployTarget`: stable and canary run as real subprocess
    processes on real (auto-assigned) ports; traffic between them is
    split by a small stdlib HTTP reverse proxy on its own listen port,
    updated live by `shift_traffic`; `health()` hits the canary directly
    (not through the proxy) so a bad canary at 1% traffic still fails its
    own health check immediately instead of waiting for a lucky request.

    `start_cmd`: argv list; the literal token `{port}` is replaced with
    the port this deployment must bind. Each deployment gets its own copy
    of `source_dir` (via `shutil.copytree`) so mutating the caller's
    working tree between deploys can never affect an already-running
    deployment."""

    def __init__(self, start_cmd: list[str], health_path: str = "/",
                 startup_timeout: float = 5.0, health_timeout: float = 2.0,
                 workdir_root: Optional[Path] = None):
        self.start_cmd = start_cmd
        self.health_path = health_path
        self.startup_timeout = startup_timeout
        self.health_timeout = health_timeout
        self.workdir_root = Path(workdir_root) if workdir_root else Path.cwd() / ".forge" / "canary_deploys"
        self.workdir_root.mkdir(parents=True, exist_ok=True)
        self._deployments: dict[str, _Deployment] = {}
        #: canary_id -> (proxy_server, proxy_thread, proxy_port, state dict)
        self._proxies: dict[str, tuple] = {}

    def deploy(self, source_dir: str, role: str) -> str:
        dep_id = uuid.uuid4().hex[:12]
        work_dir = self.workdir_root / f"{role}-{dep_id}"
        shutil.copytree(source_dir, work_dir)
        port = _free_port()
        cmd = [tok.replace("{port}", str(port)) for tok in self.start_cmd]
        proc = subprocess.Popen(cmd, cwd=str(work_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_for_port(port, self.startup_timeout):
            proc.terminate()
            raise RuntimeError(f"{role} deployment {dep_id} did not start listening on {port} "
                               f"within {self.startup_timeout}s")
        self._deployments[dep_id] = _Deployment(id=dep_id, role=role, port=port, proc=proc, work_dir=work_dir)
        return dep_id

    def health(self, deployment_id: str) -> bool:
        dep = self._deployments.get(deployment_id)
        if dep is None or dep.proc.poll() is not None:
            return False
        url = f"http://127.0.0.1:{dep.port}{self.health_path}"
        try:
            with urllib.request.urlopen(url, timeout=self.health_timeout) as resp:
                return resp.status < 400
        except (urllib.error.URLError, OSError):
            return False

    def _pair_for(self, canary_id: str) -> tuple:
        if canary_id not in self._proxies:
            raise RuntimeError(f"no traffic split started for canary {canary_id!r} — "
                               "call shift_traffic(canary_id, percent, stable_id=...) first")
        return self._proxies[canary_id]

    def start_split(self, stable_id: str, canary_id: str, initial_percent: int = 0) -> int:
        """Starts the reverse proxy for one (stable, canary) pair. Returns
        the proxy's own listen port — point real traffic at THIS port, not
        directly at either backend, once a split is live."""
        stable = self._deployments[stable_id]
        canary = self._deployments[canary_id]
        state = {"stable_port": stable.port, "canary_port": canary.port,
                 "canary_percent": max(0, min(100, initial_percent)), "timeout": self.health_timeout}
        handler = type("_BoundProxyHandler", (_ProxyHandler,), {"state": state})
        proxy_port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", proxy_port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._proxies[canary_id] = (server, thread, proxy_port, state)
        return proxy_port

    def shift_traffic(self, canary_id: str, percent: int) -> None:
        _server, _thread, _port, state = self._pair_for(canary_id)
        state["canary_percent"] = max(0, min(100, percent))

    def proxy_port(self, canary_id: str) -> int:
        _server, _thread, port, _state = self._pair_for(canary_id)
        return port

    def promote(self, canary_id: str) -> None:
        """Canary takes over: 100% traffic, stable process torn down."""
        self.shift_traffic(canary_id, 100)
        server, _thread, _port, state = self._pair_for(canary_id)
        stable_port = state["stable_port"]
        for dep in list(self._deployments.values()):
            if dep.role == "stable" and dep.port == stable_port:
                self.teardown(dep.id)

    def rollback(self, canary_id: str) -> None:
        """Canary abandoned: 0% traffic, canary process torn down."""
        self.shift_traffic(canary_id, 0)
        self.teardown(canary_id)

    def teardown(self, deployment_id: str) -> None:
        dep = self._deployments.pop(deployment_id, None)
        if dep is None:
            return
        if dep.proc.poll() is None:
            dep.proc.terminate()
            try:
                dep.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                dep.proc.kill()
        shutil.rmtree(dep.work_dir, ignore_errors=True)
        proxy = self._proxies.pop(deployment_id, None)
        if proxy is not None:
            server, thread, _port, _state = proxy
            server.shutdown()
            thread.join(timeout=3)

    def teardown_all(self) -> None:
        for dep_id in list(self._deployments):
            self.teardown(dep_id)


# ============================================================= loop ====


@dataclass
class WatchdogCycleResult:
    signals_seen: int = 0
    repaired: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    unfixed: list[str] = field(default_factory=list)


class WatchdogLoop:
    """detect -> localize -> patch (repair_agent's own evidence-based
    localization + agentic sampling) -> canary -> promote/rollback.

    With no local failing test suite for a production signal, the
    canary's own `deployer.health()` is the pass/fail oracle the repair
    loop's execution-based selection normally gets from running pytest —
    same "select by actually running it, not by asking the model"
    principle, different oracle.

    `deployer` is optional: with none configured, `run_once` still
    detects, localizes, and lands a patched-and-committed fix on disk
    (useful standalone, e.g. wired to a detector watching a staging
    log) — it just skips the canary/promote/rollback phase."""

    def __init__(self, project_dir, llm: ChatLLM, tools: ToolBackend, traj: Trajectory,
                 detector: FailureDetector, deployer: Optional[DeployTarget] = None,
                 reporter=None, canary_percent: int = 10, health_checks: int = 5,
                 health_check_interval: float = 0.5, max_turns_per_attempt: int = 20):
        self.project_dir = Path(project_dir)
        self.llm = llm
        self.tools = tools
        self.traj = traj
        self.detector = detector
        self.deployer = deployer
        self.reporter = reporter
        self.canary_percent = canary_percent
        self.health_checks = health_checks
        self.health_check_interval = health_check_interval
        self.max_turns_per_attempt = max_turns_per_attempt

    def run_once(self) -> WatchdogCycleResult:
        result = WatchdogCycleResult()
        signals = self.detector.poll()
        result.signals_seen = len(signals)
        for signal in signals:
            self.traj.log("watchdog_signal", source=signal.source, message=signal.message,
                          fingerprint=signal.fingerprint)
            handled = self._handle_signal(signal, result)
            if not handled:
                result.unfixed.append(signal.fingerprint)
        return result

    def run_forever(self, poll_interval: float = 5.0, max_cycles: Optional[int] = None,
                    stop_event: Optional[threading.Event] = None) -> None:
        cycles = 0
        while stop_event is None or not stop_event.is_set():
            self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            if stop_event is not None:
                stop_event.wait(poll_interval)
            else:
                time.sleep(poll_interval)

    def _handle_signal(self, signal: FailureSignal, result: WatchdogCycleResult) -> bool:
        sig = extract_signals(signal.raw)
        suspects = localize(sig, self.tools, self.traj, self.project_dir)
        if not suspects:
            self.traj.log("watchdog_repair", result="no suspects localized", fingerprint=signal.fingerprint)
            return False

        top = suspects[0]
        prompt = (
            f"# Live production failure\n{signal.message}\n\n# Raw signal\n```\n{signal.raw[:3500]}\n```\n\n"
            f"Prime suspect: {top.file}. Investigate, find the root cause, PATCH it, verify, SUBMIT."
        )
        candidate = _attempt_patch(top.file, prompt, self.llm, self.tools, self.project_dir, self.traj,
                                   temperature=0.0, max_turns=self.max_turns_per_attempt, sample_no=0)
        if candidate is None:
            self.traj.log("watchdog_repair", result="no candidate produced",
                          fingerprint=signal.fingerprint, file=top.file)
            return False

        target = self.project_dir / top.file
        original = target.read_text(errors="replace") if target.exists() else ""
        violations = _blast_radius_violations(top.file, original, candidate.new_content, self.tools)
        if violations:
            self.traj.log("watchdog_repair", result="blast-radius rejected", violations=violations,
                          fingerprint=signal.fingerprint)
            return False

        # Snapshot "stable" BEFORE the patch lands on disk — deploying it
        # after would hand the canary rollout two copies of the SAME
        # (already-patched) tree, defeating the whole comparison.
        stable_id = self.deployer.deploy(str(self.project_dir), role="stable") if self.deployer else None

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(candidate.new_content)
        commit(self.project_dir, f"forge: watchdog repair {top.file} (signal {signal.fingerprint})")
        self.tools.reindex_file(top.file)
        result.repaired.append(top.file)

        if self.deployer is None:
            self.traj.log("watchdog_repair", result="patched, no deployer configured", file=top.file)
            return True

        return self._canary_rollout(top.file, original, stable_id, signal, result)

    def _canary_rollout(self, file_rel: str, original: str, stable_id: str,
                        signal: FailureSignal, result: WatchdogCycleResult) -> bool:
        target = self.project_dir / file_rel
        # canary = current on-disk state, which already has the patch applied
        canary_id = self.deployer.deploy(str(self.project_dir), role="canary")
        self.deployer.start_split(stable_id, canary_id, initial_percent=0)
        self.deployer.shift_traffic(canary_id, self.canary_percent)
        self.traj.log("watchdog_canary", stable=stable_id, canary=canary_id, file=file_rel,
                      percent=self.canary_percent)

        healthy_streak = 0
        for _ in range(self.health_checks):
            if self.deployer.health(canary_id):
                healthy_streak += 1
            else:
                healthy_streak = 0
                break
            time.sleep(self.health_check_interval)

        if healthy_streak >= self.health_checks:
            self.deployer.promote(canary_id)
            result.promoted.append(file_rel)
            self.traj.log("watchdog_canary", result="promoted", file=file_rel, canary=canary_id)
            if self.reporter is not None:
                self.reporter.status(file_rel, "watchdog_promoted", {"signal": signal.fingerprint})
            return True

        self.deployer.rollback(canary_id)
        self.deployer.teardown(stable_id)
        target.write_text(original)
        commit(self.project_dir, f"forge: watchdog revert {file_rel} (canary unhealthy, signal {signal.fingerprint})")
        self.tools.reindex_file(file_rel)
        result.rolled_back.append(file_rel)
        self.traj.log("watchdog_canary", result="rolled back", file=file_rel, canary=canary_id)
        if self.reporter is not None:
            self.reporter.status(file_rel, "watchdog_rolled_back", {"signal": signal.fingerprint})
        return False
