"""
backends — Language-specific coverage collection backends.

Each backend implements the CoverageBackend protocol for one language:
  - test discovery
  - failure detection
  - per-test line coverage collection
  - function-name mapping

Auto-detection: detect_language() inspects project_root and returns
the best-matching backend.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import CoverageBackend

# Lazy imports — only load what's needed
_BACKEND_REGISTRY: dict[str, str] = {
    "python": "python_backend",
    "javascript": "javascript_backend",
    "go": "go_backend",
    "rust": "rust_backend",
    "java": "java_backend",
}


def detect_language(project_root: str) -> str | None:
    """
    Auto-detect the project language by inspecting project_root.

    Returns a language key like 'python', 'javascript', 'go', 'rust', 'java',
    or None if nothing matches.
    """
    project_root = os.path.abspath(project_root)
    if not os.path.isdir(project_root):
        return None

    # Check for language-specific markers (ordered by specificity)
    entries = set(os.listdir(project_root))

    # Rust: Cargo.toml
    if "Cargo.toml" in entries:
        return "rust"

    # Go: go.mod
    if "go.mod" in entries:
        return "go"

    # Java: pom.xml or build.gradle / build.gradle.kts
    java_markers = {"pom.xml", "build.gradle", "build.gradle.kts", ".gradle"}
    if entries & java_markers:
        return "java"

    # JavaScript/Node: package.json
    if "package.json" in entries:
        return "javascript"

    # Python: pyproject.toml, setup.py, setup.cfg, or __init__.py
    python_markers = {"pyproject.toml", "setup.py", "setup.cfg"}
    if entries & python_markers:
        return "python"

    # Fallback: check for any .py files (not just at root)
    for entry in entries:
        full = os.path.join(project_root, entry)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
            return "python"

    # Check for tsconfig.json
    if "tsconfig.json" in entries:
        return "javascript"

    return None


def get_backend(language: str | None, project_root: str) -> CoverageBackend | None:
    """
    Get a CoverageBackend instance.

    If language is None, auto-detect from project_root.
    Returns None if no backend matches.
    """
    if language is None:
        language = detect_language(project_root)
    if language is None:
        return None

    module_name = _BACKEND_REGISTRY.get(language)
    if module_name is None:
        return None

    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)

    try:
        mod = __import__(module_name, fromlist=["create_backend"])
        backend = mod.create_backend(project_root)
        return backend
    except Exception:
        return None


def list_supported_languages() -> list[str]:
    """Return list of language keys with available backends."""
    return list(_BACKEND_REGISTRY.keys())
