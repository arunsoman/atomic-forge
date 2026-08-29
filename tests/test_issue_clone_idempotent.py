"""clone_repo must be idempotent: a stale clone from an aborted attempt
must be reused (freshened), never a hard failure — sweeps and resumed
campaigns re-invoke it constantly."""
import subprocess
from pathlib import Path

import pytest

from atomic_forge.issue import clone_repo


@pytest.fixture(scope="module")
def live_repo(tmp_path_factory):
    """A real public shallow clone (network + gh-independent, https)."""
    d = tmp_path_factory.mktemp("clones")
    dest = clone_repo("mahmoud", "boltons", d / "boltons")
    yield dest
    import shutil
    shutil.rmtree(dest.parent, ignore_errors=True)


def test_fresh_clone_writes_project(live_repo):
    assert (live_repo / ".git").exists()
    assert any(live_repo.rglob("*.py"))  # boltons keeps .py under boltons/


def test_existing_clone_is_freshened(tmp_path):
    """Second invocation over the SAME dir succeeds and HEAD is sane."""
    dest = clone_repo("mahmoud", "boltons", tmp_path / "boltons")
    (dest / "leftover_artifact.txt").write_text("stale")  # dirty it
    dest = clone_repo("mahmoud", "boltons", dest)
    assert not (dest / "leftover_artifact.txt").exists()   # clean -fdx ran
    r = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    assert len(r.stdout.strip()) == 40


def test_corrupt_clone_falls_back_to_reclone(tmp_path):
    dest = Path(tmp_path / "boltons")
    dest.mkdir()
    (dest / ".git").mkdir()
    (dest / ".git" / "HEAD").write_text("garbage")  # corrupt
    dest2 = clone_repo("mahmoud", "boltons", dest)
    assert (dest2 / ".git").exists()
    assert any(dest2.rglob("*.py"))
