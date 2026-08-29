"""Tests for llm.py's default_llm() resolution, including R15's
--local-only enforcement (LocalOnlyViolation / _is_local_host)."""
import pytest

from atomic_forge.llm import LocalOnlyViolation, _is_local_host, default_llm


@pytest.mark.parametrize("base_url,expected", [
    (None, False),
    ("", False),
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:11434/v1", True),
    ("http://[::1]:11434/v1", True),
    ("http://192.168.1.50:8000/v1", True),   # private LAN
    ("http://10.0.0.5:8000/v1", True),       # private LAN
    ("https://api.openai.com/v1", False),
    ("https://openrouter.ai/api/v1", False),
    ("https://8.8.8.8/v1", False),           # public IP
])
def test_is_local_host(base_url, expected):
    assert _is_local_host(base_url) is expected


def _clear_llm_env(monkeypatch):
    for var in ("FORGE_MOCK", "FORGE_API_KEY", "FORGE_BASE_URL", "FORGE_MODEL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_local_only_rejects_hosted_forge_endpoint(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_API_KEY", "k")
    monkeypatch.setenv("FORGE_BASE_URL", "https://api.openai.com/v1")
    with pytest.raises(LocalOnlyViolation):
        default_llm(local_only=True)


def test_local_only_allows_loopback_forge_endpoint(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_API_KEY", "k")
    monkeypatch.setenv("FORGE_BASE_URL", "http://localhost:11434/v1")
    llm = default_llm(local_only=True)
    assert llm.base_url == "http://localhost:11434/v1"


def test_local_only_rejects_openai_key_with_no_base_url(monkeypatch):
    """No FORGE_BASE_URL at all defaults to OpenAI's own api.openai.com —
    not local, and must be rejected under --local-only, not silently
    allowed just because no explicit hosted URL was typed."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(LocalOnlyViolation):
        default_llm(local_only=True)


def test_local_only_does_not_affect_default_behavior(monkeypatch):
    """local_only=False (the default) must not change any existing
    resolution behavior."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_API_KEY", "k")
    monkeypatch.setenv("FORGE_BASE_URL", "https://api.openai.com/v1")
    llm = default_llm()  # local_only defaults False
    assert llm.base_url == "https://api.openai.com/v1"


def test_forge_mock_bypasses_local_only(monkeypatch):
    """FORGE_MOCK is a Python callable, never network traffic — it can't
    violate the local-only guarantee by construction, so it's exempt."""
    import atomic_forge.llm as L
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_MOCK", "1")

    class _Mock:
        def chat(self, messages, temperature=0.0, max_tokens=8192):
            return "ok"
    L.set_mock_factory(lambda: _Mock())
    llm = default_llm(local_only=True)
    assert llm.chat([]) == "ok"
