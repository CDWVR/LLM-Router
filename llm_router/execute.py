import hashlib
import json
import math
import os
import time
import uuid
import random
from dataclasses import asdict, replace
from contextlib import nullcontext

from llm_router.budget import Budget, context_upper_bound
from llm_router.gateway import CANCEL_CHECK, GatewayError, StreamClosed, complete, complete_streaming, is_rate_limited, time_left_ms
from llm_router.pipeline import route_request
from llm_router.policy import rank_models
from llm_router.quality import load_policy, load_rates
from llm_router.registry import BY_ID, CATALOG_LOCK, DEFAULT_LIMITS, MODEL_SCOPE, reload_catalog
from llm_router.trace import append_trace
from llm_router.validate import matches_schema, verify_answer


def canary_turn(request_id: str, fraction: float) -> bool:
    if fraction <= 0:
        return False
    bucket = int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    return bucket < int(fraction * 100)


def _ranked_rows(models, task_type, context_tokens, preferred_id, rates) -> list[dict]:
    rows = []
    for model, score in rank_models(models, task_type, context_tokens, preferred_id, rates):
        measured = (rates or {}).get((model.model_id, task_type))
        rows.append({
            "model_id": model.model_id, "score": round(score, 4),
            "quality": model.quality.get(task_type, 0.5) if measured is None else measured,
            "quality_source": "estimated" if measured is None else "measured",
            "latency_ms": model.expected_latency_ms,
        })
    return rows


def run_turn(
    history: list[dict], image_path: str, previous_model_id: str | None, messages_for,
    require_verification: bool = False, response_schema: dict | None = None,
    limits=DEFAULT_LIMITS, trace_path=None, on_start=None, on_delta=None, on_reset=None,
    on_phase=None, require_configured: bool = False, requested_model: str | None = None,
    max_output_tokens: int | None = None, owner_id: str | None = None,
    cancelled=None, on_final=None,
) -> dict:
    started = time.monotonic()
    record = {
        "request_id": str(uuid.uuid4()), "owner_id": owner_id, "status": "started",
        "task_type": None, "attempts": [], "reserved_usd": 0,
    }
    if os.environ.get("ROUTER_LOG_PROMPTS") == "1":
        record["user_text"] = history[-1]["text"]
    if trace_path is not None:
        append_trace(record, trace_path)
    cancel_token = CANCEL_CHECK.set(cancelled)
    try:
        if cancelled and cancelled():
            raise StreamClosed
        # One catalog snapshot for the whole request. The local HTTP server admits
        # one generation at a time, so waiting here cannot silently build a queue.
        with (nullcontext() if MODEL_SCOPE.get() is not None else CATALOG_LOCK):
            def phase(value):
                if cancelled and cancelled():
                    raise StreamClosed
                if on_phase:
                    on_phase(value)
                record.setdefault("phase_times_ms", {})[value] = round((time.monotonic() - started) * 1000)
            result = _run_turn(history, image_path, previous_model_id, messages_for,
                               require_verification, response_schema, limits, record, started,
                               on_start, on_delta, on_reset, phase, require_configured,
                               requested_model, max_output_tokens)
            if cancelled and cancelled():
                raise StreamClosed
        record.update(status="completed", returned_model_id=result["model_id"],
                      validation=result["validation"], verification=result["verification"])
        return result
    except StreamClosed:
        record["status"] = "cancelled"
        raise
    except Exception as exc:
        record.update(status="failed", error=getattr(exc, "code", type(exc).__name__))
        raise
    finally:
        CANCEL_CHECK.reset(cancel_token)
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if trace_path is not None:
            append_trace(record, trace_path)
        if on_final:
            on_final(record)


def _run_turn(history, image_path, previous_model_id, messages_for, require_verification,
              response_schema, limits, record, started, on_start, on_delta, on_reset,
              on_phase, require_configured, requested_model, max_output_tokens):
    reload_catalog()
    budget = Budget(limits.max_cost_usd)
    has_image = bool(image_path) or any(block.get("type") == "image_url" for turn in history for block in turn.get("blocks", []))
    record["budget_ceiling_usd"] = limits.max_cost_usd
    context_tokens = context_upper_bound(history, bool(image_path))
    if response_schema is not None:
        context_tokens += len(json.dumps(response_schema).encode("utf-8")) + 512
    # Semantic routing is optional; without it new requests still classify locally.
    use_jev = MODEL_SCOPE.get() is None and os.environ.get("ROUTER_USE_JEV", "0") == "1" and not requested_model and not image_path
    jev_reserve = float(os.environ.get("ROUTER_JEV_RESERVE_USD", "0.01"))
    if not math.isfinite(jev_reserve) or jev_reserve <= 0:
        raise ValueError("ROUTER_JEV_RESERVE_USD must be positive and finite.")
    if use_jev and budget.remaining > jev_reserve:
        budget.reserved += jev_reserve
    else:
        use_jev = False
    remaining_ms = time_left_ms(started, limits.max_latency_ms)
    if remaining_ms < 1000:
        raise GatewayError("latency_budget_exceeded")
    decision = route_request(
        user_text=history[-1]["text"], context_tokens=context_tokens,
        has_image=has_image, previous_model_id=previous_model_id,
        limits=replace(limits, max_cost_usd=budget.remaining),
        history=history, jev_timeout=min(5.0, remaining_ms / 1000),
        use_jev=use_jev, require_configured=require_configured,
        requested_model=requested_model, on_phase=on_phase,
    )
    policy = load_policy() if MODEL_SCOPE.get() is None else {}
    live_rates = load_rates(policy.get("quality_table"))
    candidate_rates = load_rates(policy.get("candidate_quality_table"))
    models = [BY_ID[row["model_id"]] for row in decision["ranked"]]
    record["catalog_version"] = hashlib.sha256(json.dumps([asdict(m) for m in models], sort_keys=True, default=lambda value: sorted(value)).encode()).hexdigest()[:16]
    record["policy_version"] = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]
    jev = decision.get("jev") or {}
    preferred = decision["selected"] if requested_model or jev.get("route") or "stickiness" in decision["analysis_source"] else None
    served = _ranked_rows(models, decision["task_type"], context_tokens, preferred, live_rates)
    candidate_ranked = None
    experiment = None
    if candidate_rates and policy.get("shadow"):
        candidate_ranked = _ranked_rows(models, decision["task_type"], context_tokens, preferred, candidate_rates)
    if candidate_rates and canary_turn(record["request_id"], float(policy.get("canary_fraction") or 0)):
        candidate_ranked = candidate_ranked or _ranked_rows(models, decision["task_type"], context_tokens, preferred, candidate_rates)
        served, experiment = candidate_ranked, "canary"
    decision["ranked"] = served
    decision["selected"] = served[0]["model_id"]
    independent = next((row["model_id"] for row in served[1:]
                        if BY_ID[row["model_id"]].provider != BY_ID[served[0]["model_id"]].provider), None)
    decision["fallbacks"] = [independent or served[1]["model_id"]] if len(served) > 1 else []
    record.update(decision=decision, shadow_ranked=candidate_ranked, experiment=experiment,
                  include_in_quality=experiment != "canary", task_type=decision["task_type"])
    attempts = record["attempts"]
    blocked_providers = set()
    text = model_id = None
    validation, verification = "not_required", "not_run"
    should_verify = require_verification or decision.get("needs_verification")
    # Required checks buffer output. Unchecked streaming can be abandoned/reset.
    live = on_delta is not None and response_schema is None and not should_verify
    for index, candidate_id in enumerate([decision["selected"], *decision["fallbacks"]]):
        model = BY_ID[candidate_id]
        if model.provider in blocked_providers:
            attempts.append({"model_id": candidate_id, "trigger": "skipped_provider", "error": "rate_limited"})
            continue
        shown = False

        def publish(piece):
            nonlocal shown
            if time_left_ms(started, limits.max_latency_ms) <= 0:
                raise GatewayError("latency_budget_exceeded")
            shown = True
            on_delta(piece)

        def abandon():
            nonlocal shown
            if shown and on_reset is not None:
                on_reset()
            shown = False

        for retry in range(2):
            trigger = "transport_retry" if retry else "initial" if index == 0 else "next_ranked"
            remaining_ms = time_left_ms(started, limits.max_latency_ms)
            if remaining_ms < 1000:
                raise GatewayError("latency_budget_exceeded")
            cap, reserved = budget.reserve(model, context_tokens, max_output_tokens)
            record["reserved_usd"] = budget.reserved
            if not cap:
                attempts.append({"model_id": candidate_id, "trigger": trigger, "error": "cost_budget_exceeded"})
                break
            attempt = {"model_id": candidate_id, "trigger": trigger, "reserved_usd": reserved, "output_cap": cap}
            attempts.append(attempt)
            try:
                if on_phase:
                    on_phase("writing")
                if on_start:
                    on_start(candidate_id, decision)
                messages = messages_for(history, candidate_id, image_path)
                if response_schema is not None:
                    from langchain_core.messages import SystemMessage
                    messages = [SystemMessage(content="Return only JSON conforming to this schema: " + json.dumps(response_schema)), *messages]
                if live:
                    result = complete_streaming(candidate_id, messages, remaining_ms / 1000, cap, publish)
                else:
                    result = complete(candidate_id, messages, timeout_s=remaining_ms / 1000, max_tokens=cap)
                if time_left_ms(started, limits.max_latency_ms) <= 0:
                    raise GatewayError("latency_budget_exceeded")
                ok, validation = matches_schema(result["text"], response_schema)
                attempt.update(input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
                               error=None if ok else "output_validation_failed")
                if not ok:
                    abandon()
                    break
                text, model_id = result["text"], candidate_id
                break
            except StreamClosed:
                attempt["error"] = "cancelled"
                raise
            except Exception as exc:
                abandon()
                code = exc.code if isinstance(exc, GatewayError) else "rate_limited" if is_rate_limited(exc) else "model_call_failed"
                attempt["error"] = code
                if code == "latency_budget_exceeded":
                    raise
                if is_rate_limited(exc):
                    blocked_providers.add(model.provider)
                    break
                if isinstance(exc, GatewayError) and not exc.retryable:
                    break
                if retry:
                    break
                if MODEL_SCOPE.get() is not None:
                    delay = min(random.uniform(.15, .45), max(0, time_left_ms(started, limits.max_latency_ms)/1000-.5))
                    until = time.monotonic()+delay
                    while time.monotonic()<until:
                        check = CANCEL_CHECK.get()
                        if check and check():
                            raise StreamClosed
                        time.sleep(min(.05, max(0, until-time.monotonic())))
        if text is not None:
            break
    if text is None:
        raise GatewayError(attempts[-1]["error"] if attempts else "all_models_failed")
    if should_verify:
        if on_phase:
            on_phase("verification")
        verifier_context = context_upper_bound(history, bool(image_path)) + len(text.encode("utf-8")) + 512
        # A pinned execution model still allows a different eligible reviewer.
        from llm_router.eligibility import eligible_models
        reviewers, _ = eligible_models(replace(limits, max_cost_usd=budget.remaining),
                                      verifier_context, has_image, require_configured=require_configured)
        checker = next((m for m in reviewers if m.model_id != model_id and m.provider not in blocked_providers), None)
        remaining_ms = time_left_ms(started, limits.max_latency_ms)
        if checker is None or remaining_ms < 1000:
            raise GatewayError("verification_unavailable")
        cap, reserved = budget.reserve(checker, verifier_context, 64)
        if cap < 8:
            raise GatewayError("verification_unavailable")
        record["verification_reserved_usd"] = reserved
        record["reserved_usd"] = budget.reserved
        try:
            accepted, verification = verify_answer(history[-1]["text"], text, checker.model_id,
                                                    remaining_ms / 1000, max_tokens=cap,
                                                    context_messages=messages_for(history, checker.model_id, image_path))
        except StreamClosed:
            raise
        except Exception as exc:
            raise GatewayError("verification_unavailable") from exc
        if time_left_ms(started, limits.max_latency_ms) <= 0:
            raise GatewayError("latency_budget_exceeded")
        if not accepted:
            raise GatewayError("verification_rejected" if verification == "reject" else "verification_unavailable")
        verification = "accept"
    record["reserved_usd"] = budget.reserved
    return {
        "text": text, "model_id": model_id, "decision": decision,
        "validation": validation, "verification": verification,
        "request_id": record["request_id"], "attempts": attempts,
        "reserved_usd": budget.reserved,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
        "finish_reason": result.get("finish_reason", "stop"),
        "tool_calls": result.get("tool_calls", []),
    }
