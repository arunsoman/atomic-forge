"""
Model-agnostic LLM access.

``ChatLLM`` is a plain synchronous ``chat(messages) -> str`` — deliberately
minimal so the agentic loop (agent.py) and repair loop (repair_agent.py)
stay simple and testable with a scripted double, with no framework
dependency of their own.

``default_llm()`` resolves a real OpenAI-compatible endpoint from
environment variables, in this order:

  1. ``FORGE_MOCK=1`` — set your own mock via ``forge.llm.set_mock_factory``
     (see below); useful for demos/tests with no network at all.
  2. ``FORGE_API_KEY`` / ``FORGE_BASE_URL`` / ``FORGE_MODEL`` — point forge
     at any OpenAI-compatible endpoint (OpenAI itself, a local vLLM/
     llama.cpp/Ollama/LiteLLM proxy, OpenRouter, ...).
  3. ``OPENAI_API_KEY`` (+ optional ``OPENAI_BASE_URL``/``OPENAI_MODEL``) —
     the common case: you already have this set for other tools.
  4. Otherwise: raise, with a message naming exactly what to set. Never
     silently falls back to a fake key against real api.openai.com.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

import httpx

Message = Dict[str, str]
logger = logging.getLogger("atomic_forge.llm")

_HTTPX_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=32)
#: A custom http_client (required to size the connection pool above) means
#: the openai SDK does NOT layer its own default timeout on top — it
#: trusts the client as-is. An explicit, generous timeout is required, not
#: optional, once a custom http_client is in the picture.
_HTTPX_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


def _shared_http_client() -> httpx.Client:
    return httpx.Client(limits=_HTTPX_LIMITS, timeout=_HTTPX_TIMEOUT)


class ChatLLM(Protocol):
    def chat(self, messages: List[Message], temperature: float = 0.0, max_tokens: int = 8192) -> str:
        ...


@dataclass
class ToolCall:
    """One model-issued function call, OpenAI tool-calling shape.
    `arguments` is the raw JSON-args STRING exactly as the model produced
    it, unparsed, so a malformed one surfaces as a normal "bad arguments"
    tool error instead of a chat_with_tools() crash."""
    id: str
    name: str
    arguments: str

    def as_message_tool_call(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class ChatTurn:
    """chat_with_tools()'s return shape — `content` (may be "") plus zero
    or more `tool_calls` the model asked to invoke this turn."""
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)


class ToolCallingChatLLM(Protocol):
    """Optional extra capability alongside ChatLLM.chat() — duck-typed
    (checked via hasattr, not part of ChatLLM itself) so every existing
    ChatLLM implementation (including test doubles) keeps working
    unchanged; only a caller that wants real function-calling
    (agent.py::run_agent, given a structured tool_manifest) looks for this
    at all."""
    def chat_with_tools(self, messages: List[Message], tools: List[dict],
                        temperature: float = 0.0, max_tokens: int = 8192) -> ChatTurn:
        ...


@dataclass
class UsageTracker:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: generate_batch_agentic's worker pool shares ONE ChatLLM (and
    #: therefore one UsageTracker) across concurrent threads.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, usage) -> None:
        with self._lock:
            self.calls += 1
            if usage is not None:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    def summary(self) -> str:
        return (f"llm_calls={self.calls} prompt_tokens={self.prompt_tokens} "
                f"completion_tokens={self.completion_tokens}")


def _is_rate_limited(e: Exception) -> bool:
    if getattr(e, "status_code", None) == 429:
        return True
    return "ratelimit" in type(e).__name__.lower()


@dataclass
class OpenAICompatLLM:
    """Thin wrapper over the OpenAI chat-completions API — works against
    OpenAI itself or any OpenAI-compatible endpoint (vLLM, llama.cpp,
    Ollama's OpenAI shim, OpenRouter, ...)."""

    model: str = "gpt-4o-mini"
    base_url: Optional[str] = None
    api_key: str = "not-needed"
    max_retries: int = 4
    usage: UsageTracker = field(default_factory=UsageTracker)
    #: Observability only, never read by the protocol.
    provider: Optional[str] = None
    #: Fired the instant a 429 is seen (before this attempt's backoff
    #: sleep) — lets a caller running several tasks concurrently
    #: (AdaptiveConcurrencyLimiter) react to real rate-limiting immediately
    #: instead of waiting for a whole task to fail. None is a no-op.
    on_rate_limited: Optional[Callable[[], None]] = None
    _cached_client: Any = field(default=None, init=False, repr=False)
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _client(self):
        if self._cached_client is not None:
            return self._cached_client
        with self._client_lock:
            if self._cached_client is not None:
                return self._cached_client
            try:
                from openai import OpenAI
            except ImportError as e:
                raise RuntimeError("pip install openai  # required for live model calls") from e
            kwargs: dict = {"api_key": self.api_key, "http_client": _shared_http_client()}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._cached_client = OpenAI(**kwargs)
        return self._cached_client

    def chat(self, messages: List[Message], temperature: float = 0.0, max_tokens: int = 8192) -> str:
        client = self._client()
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = client.chat.completions.create(
                    model=self.model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                )
                self.usage.record(getattr(resp, "usage", None))
                return resp.choices[0].message.content or ""
            except Exception as e:  # rate limits, transient 5xx, connection errors
                last_err = e
                if self.on_rate_limited is not None and _is_rate_limited(e):
                    self.on_rate_limited()
                time.sleep(min(2 ** attempt * 2, 30))
        raise RuntimeError(f"LLM call failed after {self.max_retries} retries: {last_err}")

    def chat_with_tools(self, messages: List[Message], tools: List[dict],
                        temperature: float = 0.0, max_tokens: int = 8192) -> ChatTurn:
        """Real OpenAI-spec function-calling (tools=/tool_choice="auto"),
        for callers that want the model to invoke tools via the API's own
        structured tool_calls — parsed and validated by the provider/SDK,
        not by a regex-based text grammar."""
        client = self._client()
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = client.chat.completions.create(
                    model=self.model, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, tools=tools, tool_choice="auto",
                )
                self.usage.record(getattr(resp, "usage", None))
                msg = resp.choices[0].message
                calls = [
                    ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
                    for tc in (msg.tool_calls or [])
                ]
                return ChatTurn(content=msg.content or "", tool_calls=calls)
            except Exception as e:
                last_err = e
                if self.on_rate_limited is not None and _is_rate_limited(e):
                    self.on_rate_limited()
                time.sleep(min(2 ** attempt * 2, 30))
        raise RuntimeError(f"LLM call failed after {self.max_retries} retries: {last_err}")


#: Set your own zero-network mock: `atomic_forge.llm.set_mock_factory(lambda: MyMockLLM())`,
#: then `FORGE_MOCK=1` makes `default_llm()` return it.
_mock_factory: Optional[Callable[[], ChatLLM]] = None


def set_mock_factory(factory: Callable[[], ChatLLM]) -> None:
    global _mock_factory
    _mock_factory = factory


class LocalOnlyViolation(RuntimeError):
    """Raised by `default_llm(local_only=True)` when the resolved endpoint
    isn't a private/loopback host — see that function's docstring."""


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_host(base_url: Optional[str]) -> bool:
    """True if `base_url` has no host at all (default resolves to OpenAI's
    own api.openai.com — never local) or resolves to a loopback/private
    address. Conservative on purpose: anything not clearly identifiable as
    local is treated as NOT local, since this backs an explicit privacy
    guarantee (R15) — a false "yes it's local" is the failure mode that
    actually matters here, not a false "no."""
    if not base_url:
        return False
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def default_llm(provider_override: Optional[str] = None, local_only: bool = False) -> ChatLLM:
    """Resolve a real backend, or the mock set via `set_mock_factory`, per
    this module's docstring precedence. `provider_override` is accepted
    for API parity with callers that want to force a specific provider —
    unused beyond logging here since this module only ever resolves ONE
    OpenAI-compatible endpoint at a time; multi-provider routing is your
    integration's job, not forge's.

    local_only (R15): if True, refuse to return an endpoint that isn't
    loopback/private (e.g. a local Ollama/vLLM/llama.cpp server) — raises
    `LocalOnlyViolation` instead of silently proceeding against a hosted
    endpoint (OpenAI, OpenRouter, a cloud proxy). Makes forge's "you CAN
    run fully local, nothing has to leave your machine" claim enforced,
    not just possible — see the wiki page 'Data Privacy / No Training' (R15).
    FORGE_MOCK is always allowed regardless: it's a Python callable, never
    network traffic, so it can't violate this guarantee by construction.
    """
    if os.environ.get("FORGE_MOCK", "").lower() in ("1", "true", "yes"):
        if _mock_factory is None:
            raise RuntimeError(
                "FORGE_MOCK is set but no mock is registered — call "
                "atomic_forge.llm.set_mock_factory(...) before default_llm()."
            )
        return _mock_factory()

    def _check_local(base_url: Optional[str], source: str) -> None:
        if local_only and not _is_local_host(base_url):
            raise LocalOnlyViolation(
                f"--local-only was set, but the resolved endpoint ({source}: "
                f"base_url={base_url!r}) isn't loopback/private. Point it at a "
                "local server (e.g. Ollama's OpenAI-compatible endpoint, "
                "http://localhost:11434/v1) or drop --local-only."
            )

    forge_key = os.environ.get("FORGE_API_KEY")
    forge_base = os.environ.get("FORGE_BASE_URL")
    forge_model = os.environ.get("FORGE_MODEL")
    if forge_key or forge_base or forge_model:
        _check_local(forge_base, "FORGE_BASE_URL")
        logger.info("default_llm: FORGE_* override (base_url=%s, model=%s)", forge_base, forge_model)
        return OpenAICompatLLM(
            model=forge_model or "gpt-4o-mini", base_url=forge_base,
            api_key=forge_key or "not-needed", provider=provider_override or "forge-override",
        )

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        openai_base = os.environ.get("OPENAI_BASE_URL")
        _check_local(openai_base, "OPENAI_BASE_URL")
        return OpenAICompatLLM(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=openai_base,
            api_key=openai_key, provider=provider_override or "openai",
        )

    raise RuntimeError(
        "atomic_forge: no LLM endpoint configured. Set FORGE_API_KEY + FORGE_BASE_URL "
        "+ FORGE_MODEL to point at any OpenAI-compatible endpoint (OpenAI itself, a "
        "local vLLM/llama.cpp/Ollama proxy, OpenRouter, ...), or OPENAI_API_KEY for "
        "the common case. Or set FORGE_MOCK=1 after registering a mock via "
        "atomic_forge.llm.set_mock_factory()."
    )
