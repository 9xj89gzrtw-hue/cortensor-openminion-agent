"""HTTP client for OpenAI-compatible inference endpoints.

Cortensor Portal exposes ``https://api.cortensor.app/v1`` with the OpenAI chat
completions shape, so the same client talks to Cortensor, a local Router proxy,
Ollama, vLLM or any other compatible endpoint.

Only the standard library is used: the canary must run in a bare CI container
without installing SDKs just to measure latency.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_TIMEOUT = 120.0
USER_AGENT = "cortensor-canary/0.1 (+https://github.com/9xj89gzrtw-hue/cortensor-openminion-agent)"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class InferenceError(RuntimeError):
    """A non-retryable or exhausted inference failure."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Target:
    """One inference endpoint under test."""

    name: str
    base_url: str
    model: str
    label: str = ""
    api_key_env: str = ""
    timeout: float = DEFAULT_TIMEOUT
    headers: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "Target":
        base_url = str(data.get("base_url", "")).strip()
        model = str(data.get("model", "")).strip()
        if not base_url:
            raise ValueError(f"target '{name}': base_url is required")
        if not model:
            raise ValueError(f"target '{name}': model is required")
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            label=str(data.get("label") or name),
            api_key_env=str(data.get("api_key_env") or ""),
            timeout=float(data.get("timeout") or DEFAULT_TIMEOUT),
            headers={str(k): str(v) for k, v in dict(data.get("headers") or {}).items()},
            notes=str(data.get("notes") or ""),
        )

    def public_dict(self) -> dict[str, Any]:
        """Serializable view with no credential material."""
        return {
            "name": self.name,
            "label": self.label,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "timeout": self.timeout,
            "notes": self.notes,
        }

    def resolve_api_key(self) -> str:
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip()
        return ""


@dataclass
class ChatResult:
    text: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    model: str
    finish_reason: str = ""
    attempts: int = 1
    status: int = 200

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def load_targets(path: str | os.PathLike[str]) -> dict[str, Target]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw = payload.get("targets", payload)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a non-empty 'targets' object")
    targets: dict[str, Target] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"{path}: target '{name}' must be an object")
        targets[name] = Target.from_mapping(name, data)
    return targets


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions") and suffix == "/chat/completions":
        return base
    return base + suffix


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


class OpenAIClient:
    """Minimal chat-completions client with bounded retries."""

    def __init__(
        self,
        target: Target,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        retries: int = 2,
        backoff: float = 0.8,
    ):
        self.target = target
        self.api_key = target.resolve_api_key() if api_key is None else api_key
        self.timeout = timeout if timeout is not None else target.timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.target.headers)
        return headers

    def _request(self, url: str, payload: dict | None, method: str = "POST") -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            message = body.decode("utf-8", "replace")[:400]
            raise InferenceError(
                f"HTTP {status}: {message}",
                status=status,
                retryable=status in RETRYABLE_STATUS,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise InferenceError(f"network error: {exc}", retryable=True) from exc

        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise InferenceError(
                f"invalid JSON from {url}: {body[:200]!r}", status=status
            ) from exc
        if not isinstance(parsed, dict):
            raise InferenceError(f"unexpected response type from {url}: {type(parsed).__name__}")
        return status, parsed

    # ------------------------------------------------------------------- API
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.target.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)

        url = _endpoint(self.target.base_url, "/chat/completions")
        started = time.perf_counter()
        attempt = 0
        last_error: InferenceError | None = None
        while attempt <= self.retries:
            attempt += 1
            try:
                status, body = self._request(url, payload)
            except InferenceError as exc:
                last_error = exc
                if exc.retryable and attempt <= self.retries:
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                raise
            latency_ms = (time.perf_counter() - started) * 1000

            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise InferenceError(f"no choices in response: {str(body)[:200]}", status=status)
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            text = _content_to_text(message.get("content"))
            if not text:
                text = _content_to_text(first.get("text"))
            if not text and not message.get("tool_calls"):
                raise InferenceError("empty completion text", status=status)

            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            if not prompt_tokens:
                prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
            if not completion_tokens:
                completion_tokens = max(1, len(text) // 4)

            return ChatResult(
                text=text,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=str(body.get("model") or self.target.model),
                finish_reason=str(first.get("finish_reason") or ""),
                attempts=attempt,
                status=status,
            )

        assert last_error is not None
        raise last_error

    def list_models(self) -> list[str]:
        url = _endpoint(self.target.base_url, "/models")
        status, body = self._request(url, None, method="GET")
        data = body.get("data")
        if not isinstance(data, list):
            return []
        names = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                names.append(str(item["id"]))
            elif isinstance(item, str):
                names.append(item)
        return names

    def probe(self) -> dict[str, Any]:
        """Small round-trip used by ``canary targets --probe``."""
        started = time.perf_counter()
        try:
            result = self.chat(
                [{"role": "user", "content": "Reply with the single word: ready"}],
                max_tokens=8,
            )
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "reply": result.text.strip()[:80],
                "model": result.model,
            }
        except InferenceError as exc:
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(exc)[:300],
            }


def quoted_url(url: str) -> str:
    """Redact query strings that may contain tokens before logging."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))
