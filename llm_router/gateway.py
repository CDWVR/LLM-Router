"""Protocol adapters for OpenAI-compatible Chat Completions and Claude Messages."""
import json
import time
import urllib.error
import urllib.request
import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from llm_router.providers import load_connections
from llm_router.registry import BY_ID
CANCEL_CHECK = ContextVar("router_cancel_check", default=None)
REQUEST_OPTIONS = ContextVar("router_request_options", default=None)


class GatewayError(RuntimeError):
    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(detail or code)


class StreamClosed(Exception):
    """The browser went away while a reply was streaming."""


def time_left_ms(started: float, max_latency_ms: int) -> float:
    return max_latency_ms - (time.monotonic() - started) * 1000


def is_rate_limited(exc: Exception) -> bool:
    if isinstance(exc, GatewayError):
        return exc.code == "rate_limited"
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def response_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block if isinstance(block, str) else block.get("text", "")
                       for block in content if isinstance(block, str) or isinstance(block, dict) and block.get("type") == "text")
    return ""


def openai_messages(messages) -> list[dict]:
    roles = {"ai": "assistant", "human": "user", "system": "system", "tool": "tool"}
    result = []
    for m in messages:
        row = {"role": roles.get(getattr(m, "type", ""), "user"), "content": m.content}
        if row["role"] == "tool":
            row["tool_call_id"] = m.tool_call_id
        calls = getattr(m, "additional_kwargs", {}).get("tool_calls")
        if calls:
            row["tool_calls"] = calls
        result.append(row)
    return result


def anthropic_messages(messages) -> tuple[list[dict], str]:
    converted, system = [], []
    for message in openai_messages(messages):
        content = message["content"]
        if message["role"] == "tool":
            converted.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content}]})
            continue
        if message.get("tool_calls"):
            blocks = [{"type": "text", "text": content}] if content else []
            for call in message["tool_calls"]:
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
            converted.append({"role": "assistant", "content": blocks})
            continue
        if message["role"] == "system":
            system.append(response_text(content))
            continue
        if isinstance(content, list):
            blocks = []
            for block in content:
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    if not url.startswith("data:image/") or ";base64," not in url:
                        raise GatewayError("invalid_image")
                    meta, data = url.split(";base64,", 1)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": meta[5:], "data": data}})
                else:
                    raise GatewayError("unsupported_content")
            content = blocks
        converted.append({"role": message["role"], "content": content})
    return converted, "\n\n".join(system)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward provider credentials to a different endpoint.
        return None


def open_request(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def _request(model_id, messages, max_tokens, stream, timeout):
    model = BY_ID.get(model_id)
    if model is None:
        raise GatewayError("model_call_failed")
    connection = load_connections()[model.provider]
    if not connection.configured:
        raise GatewayError("provider_not_configured")
    headers = {"Content-Type": "application/json"}
    body = {"model": model.upstream_model_id or model.model_id, "stream": stream}
    if connection.adapter == "anthropic":
        body["messages"], system = anthropic_messages(messages)
        if system:
            body["system"] = system
        body["max_tokens"] = max_tokens or model.output_token_cap
        headers.update({"x-api-key": connection.key(), "anthropic-version": "2023-06-01"})
        endpoint = "/messages"
    else:
        body["messages"] = openai_messages(messages)
        body[connection.token_parameter] = max_tokens or model.output_token_cap
        if stream and connection.include_usage:
            body["stream_options"] = {"include_usage": True}
        if connection.key():
            headers["Authorization"] = f"Bearer {connection.key()}"
        endpoint = "/chat/completions"
    request = urllib.request.Request(connection.base_url + endpoint, json.dumps(body).encode("utf-8"), headers, method="POST")
    options = REQUEST_OPTIONS.get() or {}
    if options.get("tools"):
        if not model.supports_tools:
            raise GatewayError("unsupported_tools")
        if connection.adapter == "anthropic":
            body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters", {"type":"object"})} for t in options["tools"]]
        else:
            body["tools"] = options["tools"]
        request = urllib.request.Request(connection.base_url + endpoint, json.dumps(body).encode(), headers, method="POST")
    try:
        return open_request(request, timeout), connection.adapter
    except urllib.error.HTTPError as exc:
        code = "rate_limited" if exc.code == 429 else "provider_auth_failed" if exc.code in {401, 403} else "model_call_failed"
        raise GatewayError(code, retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GatewayError("model_call_failed", retryable=True) from exc


def _remaining(deadline):
    check = CANCEL_CHECK.get()
    if check and check():
        raise StreamClosed
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GatewayError("latency_budget_exceeded")
    return remaining


def _socket_timeout(response, deadline):
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(_remaining(deadline))


@contextmanager
def _guard(response, deadline):
    """Interrupt an open response even if a peer trickles bytes without newlines."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    stop = threading.Event()
    check = CANCEL_CHECK.get()
    def watch():
        while not stop.wait(min(0.1, max(0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline or check and check():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
    watcher = threading.Thread(target=watch, daemon=True) if sock is not None else None
    if watcher:
        watcher.start()
    try:
        yield
    except (OSError, TimeoutError) as exc:
        _remaining(deadline)
        raise GatewayError("model_call_failed", retryable=True) from exc
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=0.2)


def _read(response, deadline):
    chunks, total = [], 0
    while True:
        _socket_timeout(response, deadline)
        _remaining(deadline)
        chunk = response.read1(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            raise GatewayError("model_call_failed")
        chunks.append(chunk)
    _remaining(deadline)
    return b"".join(chunks)


def complete(model_id: str, messages, timeout_s: float, max_tokens: int | None = None) -> dict:
    if timeout_s < 1:
        raise GatewayError("latency_budget_exceeded")
    deadline = time.monotonic() + timeout_s
    response, adapter = _request(model_id, messages, max_tokens, False, _remaining(deadline))
    with _guard(response, deadline), response:
        payload = json.loads(_read(response, deadline))
    if payload.get("error"):
        raise GatewayError("model_call_failed")
    usage = payload.get("usage") or {}
    finish_reason = "stop"
    tool_calls = []
    if adapter == "anthropic":
        text = response_text(payload.get("content"))
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        finish_reason = "length" if payload.get("stop_reason") == "max_tokens" else "stop"
        tool_calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}} for b in payload.get("content", []) if b.get("type") == "tool_use"]
        if tool_calls:
            finish_reason = "tool_calls"
    else:
        choice = (payload.get("choices") or [{}])[0]
        text = response_text((choice.get("message") or {}).get("content"))
        input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        finish_reason = choice.get("finish_reason") or "stop"
        tool_calls = (choice.get("message") or {}).get("tool_calls", [])
    if finish_reason not in {"stop", "length", "tool_calls"} or (tool_calls and not (REQUEST_OPTIONS.get() or {}).get("tools")):
        raise GatewayError("model_call_failed")
    if not text and not tool_calls:
        raise GatewayError("empty_response", retryable=True)
    return {"text": text, "tool_calls": tool_calls, "input_tokens": input_tokens, "output_tokens": output_tokens, "finish_reason": finish_reason}


def complete_streaming(model_id: str, messages, timeout_s: float, max_tokens: int | None, on_delta) -> dict:
    if timeout_s < 1:
        raise GatewayError("latency_budget_exceeded")
    deadline = time.monotonic() + timeout_s
    response, adapter = _request(model_id, messages, max_tokens, True, _remaining(deadline))
    parts, data_lines = [], []
    input_tokens = output_tokens = None
    terminal = False
    finish_reason = "stop"
    total_bytes = 0
    with _guard(response, deadline), response:
        while True:
            _socket_timeout(response, deadline)
            _remaining(deadline)
            raw = response.readline(1024 * 1024)
            _remaining(deadline)
            if not raw:
                break
            total_bytes += len(raw)
            if total_bytes > 16 * 1024 * 1024:
                raise GatewayError("model_call_failed")
            if len(raw) >= 1024 * 1024:
                raise GatewayError("model_call_failed")
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
                continue
            if line or not data_lines:
                continue
            data, data_lines = "\n".join(data_lines), []
            if data == "[DONE]":
                terminal = True
                break
            payload = json.loads(data)
            if payload.get("error") or payload.get("type") == "error":
                raise GatewayError("model_call_failed", retryable=True)
            piece = ""
            if adapter == "anthropic":
                kind = payload.get("type")
                if kind == "message_start":
                    input_tokens = (payload.get("message", {}).get("usage") or {}).get("input_tokens")
                elif kind == "content_block_delta" and payload.get("delta", {}).get("type") == "text_delta":
                    piece = payload["delta"]["text"]
                elif kind == "message_delta":
                    output_tokens = (payload.get("usage") or {}).get("output_tokens")
                    finish_reason = "length" if payload.get("delta", {}).get("stop_reason") == "max_tokens" else "stop"
                elif kind == "message_stop":
                    terminal = True
                    break
            else:
                usage = payload.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)
                choice = (payload.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                piece = response_text((choice.get("delta") or {}).get("content"))
            if piece:
                parts.append(piece)
                on_delta(piece)
    if finish_reason not in {"stop", "length"}:
        raise GatewayError("model_call_failed")
    if not terminal or not parts:
        raise GatewayError("empty_response" if not parts else "incomplete_stream", retryable=True)
    return {"text": "".join(parts), "input_tokens": input_tokens, "output_tokens": output_tokens, "finish_reason": finish_reason}
