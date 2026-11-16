"""OpenAI-compatible SSE transport, implemented using the Python standard library."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Callable

ENDPOINT = "https://api.exaone.ai/v1/chat/completions"
MODEL = "k-exaone_v2.5"


class Cancelled(Exception):
    pass


class APIError(Exception):
    pass


class ExaoneClient:
    def __init__(self, key: str, opener=None):
        self.key = key
        self.opener = opener or urllib.request.urlopen

    def complete(self, messages: list, tools: list, effort: str, max_tokens: int,
                 cancel: threading.Event, on_text: Callable[[str], None] = lambda _: None,
                 on_status: Callable[[str], None] = lambda _: None) -> dict:
        if cancel.is_set():
            raise Cancelled()
        payload = {"model": MODEL, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
                   "reasoning_effort": effort, "temperature": 1.0, "top_p": 0.95}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Authorization": f"Bearer {self.key}",
                                                  "Content-Type": "application/json", "Accept": "text/event-stream"})
        content, reasoning, calls, usage = [], [], {}, {}
        finish, done, reasoning_notified = None, False, False
        received_bytes = 0
        try:
            with self.opener(request, timeout=90) as response:
                event_lines = []

                def process_event(lines):
                    nonlocal usage, finish, done, reasoning_notified
                    data = "\n".join(lines)
                    if data == "[DONE]":
                        done = True
                        return
                    if not data:
                        return
                    event = json.loads(data)
                    if event.get("error"):
                        raise APIError("API가 오류 응답을 반환했습니다. 모델 지원·계정 상태를 확인하세요.")
                    usage = event.get("usage") or usage
                    for choice in event.get("choices", []):
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                            on_text(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                            if not reasoning_notified:
                                on_status("추론 중…")
                                reasoning_notified = True
                        for part in delta.get("tool_calls") or []:
                            index = part.get("index", 0)
                            call = calls.setdefault(index, {"id": "", "type": "function",
                                                           "function": {"name": "", "arguments": ""}})
                            # Standard SSE may split function name / arguments across chunks.
                            if part.get("id"):
                                call["id"] = part["id"]
                            function = part.get("function") or {}
                            call["function"]["name"] += function.get("name") or ""
                            call["function"]["arguments"] += function.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]

                for raw in response:
                    if cancel.is_set():
                        raise Cancelled()
                    received_bytes += len(raw)
                    if received_bytes > 8_000_000:
                        raise APIError("응답이 너무 큽니다. 출력 토큰 한도를 줄이세요.")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        process_event(event_lines)
                        event_lines = []
                        if done:
                            break
                    elif line.startswith("data:"):
                        event_lines.append(line[5:].lstrip(" "))
                if event_lines and not done:
                    process_event(event_lines)
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            reasons = {400: "요청 형식·문맥 길이·도구 지원을 확인하세요.",
                       401: "API 키가 잘못되었거나 만료되었습니다.",
                       403: "이 API를 사용할 권한이 없습니다.",
                       429: "호출 한도를 초과했습니다. 잠시 후 다시 시도하세요."}
            # Do not echo raw server bodies; they may contain submitted private data.
            raise APIError(f"HTTP {status}: {reasons.get(status, 'API 서버 오류입니다. 잠시 후 재시도하세요.')}"
                           + (f" Retry-After: {retry_after}" if retry_after else "")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise APIError(f"네트워크 연결 오류 ({type(exc).__name__}). 연결 및 방화벽을 확인하세요.") from None
        except (ValueError, UnicodeError, TypeError) as exc:
            raise APIError(f"API 응답을 해석하지 못했습니다 ({type(exc).__name__}).") from None
        if cancel.is_set():
            raise Cancelled()
        if not done or not finish:
            raise APIError("응답이 중간에 끊겼습니다. 불완전한 도구 호출은 실행하지 않았습니다.")
        if finish not in ("stop", "tool_calls"):
            raise APIError(f"생성이 완료되지 않았습니다 ({finish}). 출력 토큰을 늘리거나 요청을 나누세요.")
        message = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            values = [calls[i] for i in sorted(calls)]
            ids = [c["id"] for c in values]
            if not all(ids) or len(set(ids)) != len(ids) or len(values) > 16:
                raise APIError("잘못된 도구 호출 ID 또는 호출 수입니다.")
            for call in values:
                if not call["function"]["name"]:
                    raise APIError("이름이 없는 도구 호출입니다.")
            message["tool_calls"] = values
        return {"message": message, "usage": usage}
