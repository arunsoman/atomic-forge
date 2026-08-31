"""Tests for llm.py's default_llm() resolution, including R15's
--local-only enforcement (LocalOnlyViolation / _is_local_host), and the
FORGE_MODEL_FALLBACKS quota-exhaustion fallback (found live in the
real-issues campaign, 2026-08-31 — see benchmarks/real_issues/RESULTS.md:
13/34 sampled sweep failures were an exhausted Ollama-cloud session quota
on one model, silently killing the whole attempt with no recourse)."""
import pytest

from atomic_forge.llm import (LocalOnlyViolation, OpenAICompatLLM,
                              _is_local_host, _is_quota_exhausted, default_llm)


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


@pytest.mark.parametrize("msg,expected", [
    ("you (amazing_williams) have reached your session usage limit, "
     "upgrade for higher limits", True),
    ("Error code: 429 - insufficient_quota", True),
    ("You exceeded your current quota, please check your plan", True),
    ("Rate limit reached for requests", False),  # transient, not a hard cap
    ("connection reset by peer", False),
    ("500 internal server error", False),
])
def test_is_quota_exhausted(msg, expected):
    assert _is_quota_exhausted(RuntimeError(msg)) is expected


def test_default_llm_parses_fallback_models(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_API_KEY", "k")
    monkeypatch.setenv("FORGE_MODEL", "glm-5.2:cloud")
    monkeypatch.setenv("FORGE_MODEL_FALLBACKS", "qwen3.5:cloud, kimi-k2.7-code:cloud ,")
    llm = default_llm()
    assert llm.model == "glm-5.2:cloud"
    assert llm.fallback_models == ["qwen3.5:cloud", "kimi-k2.7-code:cloud"]


def test_default_llm_no_fallbacks_by_default(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("FORGE_API_KEY", "k")
    monkeypatch.setenv("FORGE_MODEL", "glm-5.2:cloud")
    llm = default_llm()
    assert llm.fallback_models == []


class _FakeResp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})]
        self.usage = None


class _FakeCompletions:
    """Raises `first_error` once per call to `create`, then answers with
    the calling model's name (so the test can see which model actually
    served the request without needing to inspect internals)."""
    def __init__(self, first_error):
        self.first_error = first_error
        self.seen_models: list[str] = []

    def create(self, model, messages, temperature, max_tokens, **kw):
        self.seen_models.append(model)
        if self.first_error is not None and len(self.seen_models) == 1:
            raise self.first_error
        return _FakeResp(f"served-by-{model}")


def test_chat_falls_back_to_next_model_on_quota_exhaustion(monkeypatch):
    quota_err = RuntimeError("session usage limit reached, upgrade for higher limits")
    fake = _FakeCompletions(quota_err)
    llm = OpenAICompatLLM(model="primary:cloud", fallback_models=["backup:cloud"])
    monkeypatch.setattr(llm, "_client",
                        lambda: type("Client", (), {"chat": type("Chat", (), {
                            "completions": fake})()})())
    monkeypatch.setattr("atomic_forge.llm.time.sleep", lambda *_: None)
    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out == "served-by-backup:cloud"
    assert llm.model == "backup:cloud"          # switch is sticky
    assert fake.seen_models == ["primary:cloud", "backup:cloud"]  # no wasted backoff retry


def test_chat_does_not_fall_back_on_transient_rate_limit(monkeypatch):
    """A transient rate limit (not a hard quota cap) should still use the
    normal backoff-and-retry-the-same-model path, not switch models."""
    transient_err = RuntimeError("Rate limit reached for requests")
    fake = _FakeCompletions(transient_err)
    llm = OpenAICompatLLM(model="primary:cloud", fallback_models=["backup:cloud"])
    monkeypatch.setattr(llm, "_client",
                        lambda: type("Client", (), {"chat": type("Chat", (), {
                            "completions": fake})()})())
    monkeypatch.setattr("atomic_forge.llm.time.sleep", lambda *_: None)
    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out == "served-by-primary:cloud"
    assert llm.model == "primary:cloud"          # never switched
    assert fake.seen_models == ["primary:cloud", "primary:cloud"]  # retried same model


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
