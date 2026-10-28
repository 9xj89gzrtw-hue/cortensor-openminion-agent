"""A deterministic OpenAI-compatible mock server.

It exists for three reasons:

1. CI can exercise the full pipeline without spending tokens.
2. ``canary demo`` works offline, including the OpenMinion agent loop.
3. The demo is reproducible: the same prompt always yields the same answer.

The mock is not a simulator of Cortensor quality. It is a fixture server.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class Profile:
    name: str
    delay: float = 0.03
    broken: bool = False
    verbose: bool = False


PROFILES = {
    "good": Profile("good", delay=0.03),
    "slow": Profile("slow", delay=0.7),
    "degraded": Profile("degraded", delay=0.25, broken=True),
}


def _answer(prompt: str, broken: bool) -> tuple[str, bool]:
    """Return (text, fail_request) for the last user message."""
    text = prompt.lower()

    if "17 * 23" in text:
        return "391", False
    if "80 000" in text and "15%" in text:
        return "68000", False
    if "a-1042" in text:
        return '{"order_id": "A-1042", "total": 12990}', False
    if "primary colors" in text:
        return ("red, blue, yellow, green" if broken else "red, blue, yellow"), False
    if "capital of australia" in text:
        return "Canberra", False
    if "спасибо" in text:
        return "other", False
    if "списали деньги дважды" in text:
        return "billing", False
    if "приложение вылетает" in text:
        return "tech", False
    if "сброса пароля" in text:
        return "account", False
    if "посылка не пришла" in text:
        return ("refund" if broken else "delivery"), False
    if "хочу вернуть деньги" in text:
        return "refund", False
    if "ошибкой 500" in text:
        return "tech", False
    if "#4821" in text:
        return '{"order_id": "4821"}', False
    if "#7788" in text:
        return '{"intent": "delivery", "order_id": "7788"}', False
    if "sentiment" in text and "broke our checkout" in text:
        return '{"sentiment": "negative", "confidence": 0.93}', False
    if "acme gmbh" in text:
        return '{"vendor": "Acme GmbH", "amount": 1290, "currency": "EUR"}', False
    if "berlin" in text and "celsius" in text:
        return '{"city": "Berlin", "unit": "celsius"}', False
    if "payment failed twice" in text:
        return ('{"tags": ["payment"]}' if broken else '{"tags": ["billing", "payment"]}'), False
    if "production api returns 500" in text:
        if broken:
            return "", True
        return '{"escalate": true, "reason": "production outage"}', False

    if "reply with json" in text or "ответь только json" in text or "верни только json" in text:
        return '{"ok": true}', False
    return "ready", False


def _agent_step(tool_messages: list[str], model: str) -> dict[str, Any] | None:
    """Scripted tool-use policy used by the offline agent demo.

    The mock plays the role of a model that follows the canary workflow. It
    reads previous tool results, so ids and paths are real.
    """
    step = len(tool_messages)
    if step == 0:
        return _tool_call("list_targets", {})
    if step == 1:
        return _tool_call("run_eval", {"target": "mock-baseline", "suite": "smoke"})
    if step == 2:
        return _tool_call("run_eval", {"target": "mock-candidate", "suite": "smoke"})

    def load(index: int) -> dict[str, Any]:
        try:
            value = json.loads(tool_messages[index])
        except (ValueError, IndexError):
            return {}
        if isinstance(value, dict) and isinstance(value.get("outputs"), dict):
            data = value["outputs"].get("data")
            if isinstance(data, str):
                try:
                    value = json.loads(data)
                except ValueError:
                    return {"data": data}
            elif isinstance(data, dict):
                value = data
        return value if isinstance(value, dict) else {}

    if step == 3:
        baseline = load(1).get("run_id", "")
        candidate = load(2).get("run_id", "")
        return _tool_call(
            "compare_runs",
            {"baseline_run": str(baseline), "candidate_run": str(candidate)},
        )
    if step == 4:
        path = load(3).get("comparison_path", "")
        return _tool_call("gate_verdict", {"comparison_path": str(path)})
    return None


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha1(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:8]
    return {
        "id": f"call_mock_{name}_{digest}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)


class _Handler(BaseHTTPRequestHandler):
    server_version = "CortensorCanaryMock/0.1"
    profile: Profile = PROFILES["good"]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.profile.verbose:
            print("[mock] " + fmt % args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            return self._send(
                200,
                {
                    "object": "list",
                    "data": [{"id": "mock-good", "object": "model", "owned_by": "canary"}],
                },
            )
        if self.path.rstrip("/").endswith("/health"):
            return self._send(200, {"ok": True, "profile": self.profile.name})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": {"message": "invalid json"}})

        messages = payload.get("messages") or []
        tools = payload.get("tools") or []
        last_user = ""
        tool_messages: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                last_user = str(message.get("content") or "")
            if message.get("role") == "tool":
                tool_messages.append(str(message.get("content") or ""))

        tool_names = []
        for tool in tools:
            if isinstance(tool, dict):
                fn = tool.get("function") or {}
                if isinstance(fn, dict) and fn.get("name"):
                    tool_names.append(str(fn["name"]))

        time.sleep(self.profile.delay)

        if "run_eval" in tool_names:
            call = _agent_step(tool_messages, str(payload.get("model") or "mock-good"))
            if call is not None:
                return self._send(
                    200,
                    {
                        "id": "chatcmpl-mock-tool",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": str(payload.get("model") or "mock-good"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [call],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92},
                    },
                )
            verdict = ""
            if tool_messages:
                try:
                    envelope = json.loads(tool_messages[-1])
                    outputs = envelope.get("outputs") if isinstance(envelope, dict) else None
                    if isinstance(outputs, dict):
                        verdict = str(outputs.get("data") or "")
                    else:
                        verdict = tool_messages[-1]
                except ValueError:
                    verdict = tool_messages[-1]
            return self._text_response(
                "Verdict from gate_verdict:\n" + (verdict or "No verdict produced."),
                payload,
                messages,
            )

        if tools and not tool_messages:
            name = "list_targets" if "list_targets" in tool_names else (tool_names[0] if tool_names else "")
            if name:
                return self._send(
                    200,
                    {
                        "id": "chatcmpl-mock-tool",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": str(payload.get("model") or "mock-good"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [_tool_call(name, {})],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
                    },
                )

        text, fail = _answer(last_user, self.profile.broken)
        if fail:
            return self._send(503, {"error": {"message": "mock upstream overloaded (intentional)"}})

        if tool_messages and text == "ready":
            text = "Tool executed successfully. The canary pipeline is ready."

        return self._text_response(text, payload, messages)

    def _text_response(self, text: str, payload: dict[str, Any], messages: list[Any]) -> None:
        prompt_tokens = sum(_tokens(m.get("content", "")) for m in messages if isinstance(m, dict))
        completion_tokens = _tokens(text)
        self._send(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(payload.get("model") or "mock-good"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )


class MockServer:
    """Threaded mock endpoint bound to an ephemeral port."""

    def __init__(self, profile: str = "good", host: str = "127.0.0.1", port: int = 0):
        if profile not in PROFILES:
            raise ValueError(f"unknown mock profile {profile!r}; choose {sorted(PROFILES)}")
        handler = type(
            "CanaryMockHandler",
            (_Handler,),
            {"profile": PROFILES[profile]},
        )
        self.profile = profile
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "MockServer":
        if self.thread and self.thread.is_alive():
            return self
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="canary-mock", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
