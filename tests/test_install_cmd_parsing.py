"""Shared regression: install commands arrive in three shapes and the venv
setup must accept all of them (click hit this: install_cmd as a FULL command
became pip's arguments, so pip tried to install the literal package `install`)."""
from pathlib import Path
from atomic_forge.issue import _install_arglist


def test_full_command_prefix_is_stripped(tmp_path):
    assert _install_arglist(tmp_path, "pip install -e .[dev]") == ["-e", ".[dev]"]


def test_python_dash_m_variant():
    assert _install_arglist(Path("/x"), "python -m pip install -e .") == ["-e", "."]


def test_plain_args_only():
    assert _install_arglist(Path("/x"), '-e ".[tests,typing]"') == ["-e", ".[tests,typing]"]


def test_skip_empty():
    assert _install_arglist(Path("/x"), "") == []
    assert _install_arglist(Path("/x"), None) == []  # no pyproject/requirements in /x


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
