"""A deliberately narrow, stateless Chat Completions-compatible API."""
import math
import time
from dataclasses import replace

from jsonschema import validators
from llm_router.execute import run_turn
from llm_router.messages import conversation_messages
from llm_router.providers import load_connections
from llm_router import registry


class APIRequestError(ValueError):
    pass


def normalize_chat(body: dict) -> tuple[list[dict], dict]:
    allowed = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "response_format", "router"}
    if set(body) - allowed:
        raise APIRequestError("Unsupported request parameter. See INTEGRATIONS.md for the supported subset.")
    if body.get("stream", False) is not False:
        raise APIRequestError("This API currently requires stream=false; the web app supports streaming.")
    model = body.get("model", "auto")
    if not isinstance(model, str) or not model:
        raise APIRequestError("model must be auto or a catalog model ID.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise APIRequestError("messages must contain 1 to 100 text messages.")
    history = []
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content"}:
            raise APIRequestError("Each message needs role and text content.")
        role, text = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(text, str) or not text.strip():
            raise APIRequestError("Only nonempty system, user, and assistant text messages are supported.")
        history.append({"role": role, "text": text})
    if history[-1]["role"] != "user" or sum(len(m["text"]) for m in history) > 100_000:
        raise APIRequestError("End with a user message; total content must be under 100,000 characters.")
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise APIRequestError("Set only one output token limit.")
    cap = body.get("max_completion_tokens", body.get("max_tokens"))
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise APIRequestError("Output token limit must be a positive integer.")
    options = body.get("router", {})
    if not isinstance(options, dict) or set(options) - {"max_cost_usd", "max_latency_ms", "verify"}:
        raise APIRequestError("router supports max_cost_usd, max_latency_ms, and verify.")
    cost = options.get("max_cost_usd", registry.DEFAULT_LIMITS.max_cost_usd)
    latency = options.get("max_latency_ms", registry.DEFAULT_LIMITS.max_latency_ms)
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or not 0 <= cost <= registry.DEFAULT_LIMITS.max_cost_usd:
        raise APIRequestError("Cost limit must narrow the server limit.")
    if isinstance(latency, bool) or not isinstance(latency, int) or not 1000 <= latency <= registry.DEFAULT_LIMITS.max_latency_ms:
        raise APIRequestError("Latency limit must narrow the server limit and be at least 1,000 ms.")
    if not isinstance(options.get("verify", False), bool):
        raise APIRequestError("verify must be a boolean.")
    schema = None
    response_format = body.get("response_format")
    if response_format is not None:
        if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
            raise APIRequestError("response_format supports json_schema only.")
        definition = response_format.get("json_schema")
        schema = definition.get("schema") if isinstance(definition, dict) else None
        if not isinstance(schema, dict):
            raise APIRequestError("json_schema.schema must be an object.")
        try:
            validators.validator_for(schema).check_schema(schema)
            # Avoid remote reference fetching and ambiguous schema ownership.
            def references(value):
                if isinstance(value, dict):
                    return any(k in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(v, str) and not v.startswith("#") or references(v) for k, v in value.items())
                return isinstance(value, list) and any(references(v) for v in value)
            if references(schema):
                raise ValueError("Remote references are unsupported.")
        except Exception as exc:
            raise APIRequestError("Invalid or unsupported JSON Schema.") from exc
    return history, {"requested_model": None if model == "auto" else model, "max_output_tokens": cap,
                     "require_verification": options.get("verify", False), "response_schema": schema,
                     "limits": replace(registry.DEFAULT_LIMITS, max_cost_usd=cost, max_latency_ms=latency)}


def chat_completion(body: dict, trace_path=None) -> dict:
    history, kwargs = normalize_chat(body)
    result = run_turn(history, "", None, conversation_messages, require_configured=True, trace_path=trace_path, **kwargs)
    usage = {"prompt_tokens": result["input_tokens"], "completion_tokens": result["output_tokens"]}
    usage["total_tokens"] = sum(usage.values()) if all(v is not None for v in usage.values()) else None
    return {"id": "chatcmpl-" + result["request_id"], "object": "chat.completion", "created": int(time.time()),
            "model": result["model_id"], "choices": [{"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": result["finish_reason"]}],
            "usage": usage, "router": {"request_id": result["request_id"], "task": result["decision"]["task_type"],
                                       "verification": result["verification"], "elapsed_ms": result["elapsed_ms"],
                                       "reserved_usd": result["reserved_usd"]}}


def model_list() -> dict:
    with registry.CATALOG_LOCK:
        registry.reload_catalog()
        connections = load_connections()
        return {"object": "list", "data": [{"id": "auto", "object": "model", "owned_by": "router"},
                *[{"id": m.model_id, "object": "model", "owned_by": m.provider}
                  for m in registry.get_catalog() if connections[m.provider].configured]]}
