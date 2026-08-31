"""Tests for llm.py's default_llm() resolution, including R15's
--local-only enforcement (LocalOnlyViolation / _is_local_host), and for
LLMQuotaError — the distinguishable exception `chat`/`chat_with_tools`
raise when every retry was exhausted against a rate-limit/quota
condition, so a caller can tell that apart from a genuine model
failure (see llm.py's LLMQuotaError docstring for the production
incident this fixes)."""
import pytest

from atomic_forge.llm import (LLMQuotaError, LocalOnlyViolation, OpenAICompatLLM,
                              _is_local_host, _is_rate_limited, default_llm)


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


# ----------------------------------------------------- LLMQuotaError / _is_rate_limited
class _StatusCodeError(Exception):
    def __init__(self, status_code):
        super().__init__(f"boom {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("err,expected", [
    (_StatusCodeError(429), True),
    (_StatusCodeError(500), False),
    (RuntimeError("Error code: 429 - {'error': {'message': 'rate limited'}}"), True),
    (RuntimeError("weekly usage limit exceeded — session usage limit reached"), True),
    (RuntimeError("Rate limit exceeded, try again later"), True),
    (RuntimeError("connection reset by peer"), False),
    (RuntimeError("500 Internal Server Error"), False),
    (ValueError("model does not exist"), False),
])
def test_is_rate_limited(err, expected):
    assert _is_rate_limited(err) is expected


class _FakeCompletions:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        raise self.exc


class _FakeChat:
    def __init__(self, exc):
        self.completions = _FakeCompletions(exc)


class _FakeClient:
    def __init__(self, exc):
        self.chat = _FakeChat(exc)


def _llm_with_failing_client(exc, max_retries=2):
    llm = OpenAICompatLLM(max_retries=max_retries)
    llm._cached_client = _FakeClient(exc)
    return llm


def test_chat_raises_llm_quota_error_on_429(monkeypatch):
    import atomic_forge.llm as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)  # skip real backoff
    llm = _llm_with_failing_client(_StatusCodeError(429))
    with pytest.raises(LLMQuotaError, match="rate-limit/quota"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert llm._client().chat.completions.calls == llm.max_retries


def test_chat_raises_llm_quota_error_on_session_usage_limit_text(monkeypatch):
    """The exact production shape: an OpenAI-SDK exception whose message
    carries 'session usage limit' text with no structured status_code
    attribute at all (Ollama Cloud's quota response)."""
    import atomic_forge.llm as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    exc = RuntimeError("Error code: 429 - session usage limit reached, try again later")
    llm = _llm_with_failing_client(exc)
    with pytest.raises(LLMQuotaError):
        llm.chat([{"role": "user", "content": "hi"}])


def test_chat_raises_plain_runtime_error_on_non_quota_failure(monkeypatch):
    """A genuinely non-transient failure (bad model name, auth, a real 5xx
    unrelated to quota) must still raise the original plain RuntimeError,
    not LLMQuotaError — the distinction only fires for an actual quota/
    rate-limit signature, not every exhausted-retries case."""
    import atomic_forge.llm as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    llm = _llm_with_failing_client(ValueError("model 'nope' does not exist"))
    with pytest.raises(RuntimeError) as exc_info:
        llm.chat([{"role": "user", "content": "hi"}])
    assert not isinstance(exc_info.value, LLMQuotaError)


def test_chat_with_tools_raises_llm_quota_error_on_429(monkeypatch):
    import atomic_forge.llm as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    llm = _llm_with_failing_client(_StatusCodeError(429))
    with pytest.raises(LLMQuotaError):
        llm.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])


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
