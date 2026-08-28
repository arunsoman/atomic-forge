import sys
import time
import urllib.request

from atomic_forge.sandbox import ensure_repo
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory
from atomic_forge.watchdog import (
    LocalProcessCanaryDeployer, LogFailureDetector, WatchdogLoop,
)

from _helpers import ScriptedChatLLM


# ==================================================== LogFailureDetector ====

_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "src/app.py", line 3, in bar\n'
    "    raise ValueError('boom')\n"
    "ValueError: boom\n"
)


def test_log_detector_returns_no_signals_for_empty_file(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    detector = LogFailureDetector(log)
    assert detector.poll() == []


def test_log_detector_finds_traceback(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)
    signals = detector.poll()
    assert len(signals) == 1
    assert "ValueError" in signals[0].message


def test_log_detector_only_reads_new_content(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)
    detector.poll()
    assert detector.poll() == []  # nothing new appended


def test_log_detector_dedupes_identical_recurring_crash(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)
    first = detector.poll()
    assert len(first) == 1
    with log.open("a") as f:
        f.write(_TRACEBACK)  # the exact same crash happens again
    second = detector.poll()
    assert second == []  # deduped by fingerprint, not just offset


def test_log_detector_surfaces_distinct_new_crash(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)
    detector.poll()
    other = _TRACEBACK.replace("ValueError", "TypeError").replace("boom", "different")
    with log.open("a") as f:
        f.write(other)
    second = detector.poll()
    assert len(second) == 1
    assert "TypeError" in second[0].message


# =============================================== LocalProcessCanaryDeployer ====

def _write_marker_app(dir_, marker: str):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "marker.txt").write_text(marker)
    (dir_ / "app.py").write_text(
        "import sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(sys.argv[1])\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(open('marker.txt', 'rb').read())\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )


def _make_deployer(tmp_path):
    return LocalProcessCanaryDeployer(
        start_cmd=[sys.executable, "app.py", "{port}"],
        health_path="/", startup_timeout=5.0, health_timeout=2.0,
        workdir_root=tmp_path / "deploys",
    )


def test_deploy_and_health_check(tmp_path):
    stable_src = tmp_path / "stable_src"
    _write_marker_app(stable_src, "STABLE")
    deployer = _make_deployer(tmp_path)
    try:
        dep_id = deployer.deploy(str(stable_src), role="stable")
        assert deployer.health(dep_id) is True
    finally:
        deployer.teardown_all()


def test_health_false_for_unknown_deployment(tmp_path):
    deployer = _make_deployer(tmp_path)
    assert deployer.health("nonexistent") is False


def test_traffic_split_routes_to_canary_at_100_percent(tmp_path):
    stable_src, canary_src = tmp_path / "stable_src", tmp_path / "canary_src"
    _write_marker_app(stable_src, "STABLE")
    _write_marker_app(canary_src, "CANARY")
    deployer = _make_deployer(tmp_path)
    try:
        stable_id = deployer.deploy(str(stable_src), role="stable")
        canary_id = deployer.deploy(str(canary_src), role="canary")
        proxy_port = deployer.start_split(stable_id, canary_id, initial_percent=0)
        deployer.shift_traffic(canary_id, 100)
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/", timeout=3) as resp:
            assert resp.read() == b"CANARY"
    finally:
        deployer.teardown_all()


def test_traffic_split_routes_to_stable_at_0_percent(tmp_path):
    stable_src, canary_src = tmp_path / "stable_src", tmp_path / "canary_src"
    _write_marker_app(stable_src, "STABLE")
    _write_marker_app(canary_src, "CANARY")
    deployer = _make_deployer(tmp_path)
    try:
        stable_id = deployer.deploy(str(stable_src), role="stable")
        canary_id = deployer.deploy(str(canary_src), role="canary")
        proxy_port = deployer.start_split(stable_id, canary_id, initial_percent=0)
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/", timeout=3) as resp:
            assert resp.read() == b"STABLE"
    finally:
        deployer.teardown_all()


def test_promote_tears_down_stable_and_serves_canary_fully(tmp_path):
    stable_src, canary_src = tmp_path / "stable_src", tmp_path / "canary_src"
    _write_marker_app(stable_src, "STABLE")
    _write_marker_app(canary_src, "CANARY")
    deployer = _make_deployer(tmp_path)
    try:
        stable_id = deployer.deploy(str(stable_src), role="stable")
        canary_id = deployer.deploy(str(canary_src), role="canary")
        deployer.start_split(stable_id, canary_id, initial_percent=10)
        deployer.promote(canary_id)
        assert deployer.health(stable_id) is False  # torn down
        assert deployer.health(canary_id) is True
    finally:
        deployer.teardown_all()


def test_rollback_tears_down_canary_and_keeps_stable(tmp_path):
    stable_src, canary_src = tmp_path / "stable_src", tmp_path / "canary_src"
    _write_marker_app(stable_src, "STABLE")
    _write_marker_app(canary_src, "CANARY")
    deployer = _make_deployer(tmp_path)
    try:
        stable_id = deployer.deploy(str(stable_src), role="stable")
        canary_id = deployer.deploy(str(canary_src), role="canary")
        deployer.start_split(stable_id, canary_id, initial_percent=10)
        deployer.rollback(canary_id)
        assert deployer.health(canary_id) is False  # torn down
        assert deployer.health(stable_id) is True
    finally:
        deployer.teardown_all()


# ============================================================ WatchdogLoop ====

def _project_with_bug(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def bar():\n    raise ValueError('boom')\n"
    )
    ensure_repo(tmp_path)
    return tmp_path


def test_watchdog_run_once_patches_and_commits_without_deployer(tmp_path):
    project_dir = _project_with_bug(tmp_path)
    tools = LocalToolBackend(project_dir)
    traj = Trajectory(project_dir)
    llm = ScriptedChatLLM([
        "PATCH\n```python\ndef bar():\n    return 2\n```",
        "SUBMIT",
    ])
    log = project_dir / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)

    loop = WatchdogLoop(project_dir, llm, tools, traj, detector, deployer=None)
    result = loop.run_once()

    assert result.signals_seen == 1
    assert "src/app.py" in result.repaired
    assert (project_dir / "src" / "app.py").read_text().strip() == "def bar():\n    return 2".strip()


def test_watchdog_run_once_promotes_on_healthy_canary(tmp_path):
    project_dir = _project_with_bug(tmp_path)
    # Not `python -m http.server`: it calls socket.getfqdn() at startup,
    # which can hang for many seconds in a DNS-less sandbox — the same
    # trivial marker server used above avoids that dependency.
    _write_marker_app(project_dir, "OK")
    tools = LocalToolBackend(project_dir)
    traj = Trajectory(project_dir)
    llm = ScriptedChatLLM([
        "PATCH\n```python\ndef bar():\n    return 2\n```",
        "SUBMIT",
    ])
    log = project_dir / "app.log"
    log.write_text(_TRACEBACK)
    detector = LogFailureDetector(log)
    deployer = LocalProcessCanaryDeployer(
        start_cmd=[sys.executable, "app.py", "{port}"],
        health_path="/", startup_timeout=5.0, health_timeout=2.0,
        workdir_root=tmp_path.parent / f"{tmp_path.name}-deploys",
    )

    loop = WatchdogLoop(project_dir, llm, tools, traj, detector, deployer=deployer,
                        canary_percent=10, health_checks=2, health_check_interval=0.1)
    try:
        result = loop.run_once()
        assert "src/app.py" in result.promoted
        assert result.rolled_back == []
    finally:
        deployer.teardown_all()
