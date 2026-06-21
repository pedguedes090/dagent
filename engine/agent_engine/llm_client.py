from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from . import telemetry
from .durable_execution import cached_tool_call


def _chat_url(server_url: str) -> str:
    base = str(server_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing model server URL.")
    return f"{base}/chat/completions"


def _extract_text(payload: dict[str, Any]) -> str:
    message = payload.get("choices", [{}])[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return ""


def _extract_sse_text(raw: str) -> str:
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        choice = payload.get("choices", [{}])[0]
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        parts.append(delta.get("content") or message.get("content") or "")
    return "".join(parts)


def _decode_json_string_fragment(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")


def _extract_loose_content(raw: str) -> str:
    match = re.search(r'"content"\s*:\s*"', raw)
    if not match:
        return ""

    index = match.end()
    chars: list[str] = []
    escaped = False
    while index < len(raw):
        char = raw[index]
        if escaped:
            if char == "u" and index + 4 < len(raw):
                chunk = raw[index + 1 : index + 5]
                try:
                    chars.append(chr(int(chunk, 16)))
                    index += 5
                    escaped = False
                    continue
                except ValueError:
                    pass
            chars.append(_decode_json_string_fragment(f"\\{char}"))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            tail = raw[index + 1 : index + 40]
            if re.match(r"\s*[,}]", tail):
                return "".join(chars)
            chars.append(char)
        else:
            chars.append(char)
        index += 1
    return "".join(chars).strip()


class ChatClient:
    _circuit_state: dict[str, dict[str, Any]] = {}

    def __init__(self, server_url: str, model: str, api_key: str = "") -> None:
        self.server_url = server_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = float(os.getenv("AGENT_LLM_TIMEOUT_SECONDS", "180"))
        self.max_retries = max(0, int(os.getenv("AGENT_LLM_MAX_RETRIES", "2")))
        self.backoff_seconds = max(0.0, float(os.getenv("AGENT_LLM_BACKOFF_SECONDS", "0.5")))
        self.circuit_threshold = max(1, int(os.getenv("AGENT_LLM_CIRCUIT_THRESHOLD", "3")))
        self.circuit_cooldown_seconds = max(1.0, float(os.getenv("AGENT_LLM_CIRCUIT_COOLDOWN_SECONDS", "30")))

    def _circuit_key(self) -> str:
        return f"{self.server_url.rstrip('/')}::{self.model}"

    def _circuit_open_reason(self) -> str | None:
        state = self._circuit_state.get(self._circuit_key()) or {}
        opened_at = float(state.get("openedAt") or 0)
        if not opened_at:
            return None
        elapsed = time.monotonic() - opened_at
        if elapsed >= self.circuit_cooldown_seconds:
            self._circuit_state.pop(self._circuit_key(), None)
            return None
        return f"LLM circuit is open for {round(self.circuit_cooldown_seconds - elapsed, 1)}s after repeated failures."

    def _record_success(self) -> None:
        self._circuit_state.pop(self._circuit_key(), None)

    def _record_failure(self, exc: Exception) -> None:
        key = self._circuit_key()
        state = self._circuit_state.get(key) or {"failures": 0, "openedAt": 0}
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        state["lastError"] = str(exc)[:240]
        if failures >= self.circuit_threshold:
            state["openedAt"] = time.monotonic()
        self._circuit_state[key] = state

    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code == 429 or exc.code >= 500
        return isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError))

    def _request_raw(self, req: urllib.request.Request) -> str:
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        telemetry.inject_trace_context(headers)

        with telemetry.start_span(
            "tool.llm_chat",
            {
                "llm.model": self.model,
                "llm.server_url": self.server_url,
                "llm.json_mode": json_mode,
                "llm.message_count": len(messages),
                "llm.max_retries": self.max_retries,
            },
        ) as span:
            open_reason = self._circuit_open_reason()
            if open_reason:
                telemetry.set_span_attrs(span, {"llm.circuit_open": True})
                raise RuntimeError(open_reason)

            req = urllib.request.Request(
                _chat_url(self.server_url),
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            retry_count = 0

            def request_with_retry() -> str:
                nonlocal retry_count
                for attempt in range(self.max_retries + 1):
                    retry_count = attempt
                    try:
                        response_text = self._request_raw(req)
                        self._record_success()
                        return response_text
                    except Exception as exc:
                        self._record_failure(exc)
                        telemetry.set_span_attrs(span, {"llm.retry_count": attempt, "llm.last_error": str(exc)[:240]})
                        if attempt >= self.max_retries or not self._should_retry(exc):
                            raise
                        time.sleep(self.backoff_seconds * (2**attempt))
                raise RuntimeError("LLM retry loop exited unexpectedly.")

            raw, cache_hit = cached_tool_call(
                "tool",
                "llm_chat",
                {
                    "serverUrl": self.server_url,
                    "model": self.model,
                    "temperature": temperature,
                    "jsonMode": json_mode,
                    "messages": messages,
                },
                request_with_retry,
            )
            raw = str(raw)
            telemetry.set_span_attrs(span, {"llm.retry_count": retry_count, "llm.cache_hit": cache_hit})

            try:
                payload = json.loads(raw)
                usage = payload.get("usage") if isinstance(payload, dict) else None
                if isinstance(usage, dict) and not cache_hit:
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    completion_tokens = int(usage.get("completion_tokens") or 0)
                    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
                    telemetry.record_token_usage(total_tokens, self.model)
                    telemetry.set_span_attrs(
                        span,
                        {
                            "llm.usage.prompt_tokens": prompt_tokens,
                            "llm.usage.completion_tokens": completion_tokens,
                            "llm.usage.total_tokens": total_tokens,
                        },
                    )
                return _extract_text(payload)
            except json.JSONDecodeError:
                streamed = _extract_sse_text(raw)
                if streamed:
                    return streamed
                loose = _extract_loose_content(raw)
                if loose:
                    return loose
                raise ValueError(f"Cannot parse model response: {raw[:240]}")

    def json(self, prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            text = self.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an autonomous planning/review agent inside a LangGraph coding pipeline. "
                            "Return valid JSON only. The user prefers Vietnamese final summaries."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                json_mode=True,
            )
        except Exception as exc:
            return dict(fallback, raw="", jsonParseError=str(exc))

        candidates = [text]
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if fenced:
            candidates.append(fenced.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])

        parse_error = ""
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
        return dict(fallback, raw=text[:4000], jsonParseError=parse_error)
