"""Shared regression: install commands arrive in three shapes and the venv
setup must accept all of them (click hit this: install_cmd as a FULL command
became pip's arguments, so pip tried to install the literal package `install`)."""
from pathlib import Path
from atomic_forge.issue import _install_arglist, _detect_install_cmd


def test_full_command_prefix_is_stripped(tmp_path):
    assert _install_arglist(tmp_path, "pip install -e .[dev]") == ["-e", ".[dev]"]


def test_python_dash_m_variant():
    assert _install_arglist(Path("/x"), "python -m pip install -e .") == ["-e", "."]


def test_plain_args_only():
    assert _install_arglist(Path("/x"), '-e ".[tests,typing]"') == ["-e", ".[tests,typing]"]


def test_skip_empty():
    assert _install_arglist(Path("/x"), "") == []
    assert _install_arglist(Path("/x"), None) == []  # no pyproject/requirements in /x


# ----------------------------------------- _detect_install_cmd extras ----
def test_detect_install_cmd_used_fixed_dev_guess_only(tmp_path):
    # No optional-dependencies/poetry extras table at all: `dev` is the
    # only extra tried — unchanged behavior for the common case.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
    assert _detect_install_cmd(tmp_path) == "pip install -e '.[dev]'"


def test_detect_install_cmd_reads_project_own_declared_extras(tmp_path):
    """`fix.py`'s separate repair-loop venv (setup_python_env ->
    _detect_install_cmd) used to always guess a single fixed extra name,
    `dev` — regardless of what the project itself actually declares. Same
    RCA class as urllib3#5107 (secure/pyOpenSSL, not `dev`) and ipython's
    `testpath`, just hit through this other bootstrap entrypoint instead of
    stacks.py's. Now: every declared extra is unioned in alongside the old
    `dev` guess (pip only warns, never fails, on an extra that doesn't
    exist — confirmed empirically), so real optional integrations the bug
    can live behind actually get installed."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n\n"
        "[project.optional-dependencies]\n"
        "secure = [\"pyopenssl\"]\n"
        "socks = [\"pysocks\"]\n")
    cmd = _detect_install_cmd(tmp_path)
    assert cmd == "pip install -e '.[dev,secure,socks]'"
    # and it still round-trips through the shared arg parser correctly
    assert _install_arglist(tmp_path, cmd) == ["-e", ".[dev,secure,socks]"]


def test_detect_install_cmd_setup_py_only_unaffected(tmp_path):
    # setup.py-only project, no pyproject.toml to read extras from at all
    # — falls back to the old fixed `dev` guess exactly as before.
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='x')\n")
    assert _detect_install_cmd(tmp_path) == "pip install -e '.[dev]'"


def test_pipless_venv_is_recreated(tmp_path):
    """A pip-less .venv (e.g. `uv venv` left by the R16c scratch) must be
    recreated — the installer error the sweep hit ('No module named pip')
    silently Bristol every probe after it."""
    import shutil, subprocess as sp
    import venv
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='dummy'\nversion='0'\n")
    vd = proj / ".venv"
    venv.create(str(vd), with_pip=False, clear=True)  # pip-less, like uv venv
    py = vd / "bin" / "python"
    assert sp.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode != 0
    from atomic_forge.issue import setup_python_env
    got_py = setup_python_env(proj, install_cmd="-e .")   # may skip install errors? no: installs dummy, no deps
    assert Path(got_py).resolve().parent == vd.resolve() or (vd / "bin" / "python").exists()
    assert sp.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode == 0
