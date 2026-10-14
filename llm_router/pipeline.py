from llm_router.eligibility import eligible_models
from llm_router.jev_client import JevUnavailable, ask
from llm_router.policy import SHORT_TURN_CHARS, rank_models
from llm_router.registry import DEFAULT_LIMITS, Limits
import re

# Leave room under Jev's 32,000-token context for the questions themselves.
JEV_CHAR_BUDGET = 96_000


def conversation_state(history: list[dict]) -> dict:
    latest = history[-1]["text"]
    if len(latest) > JEV_CHAR_BUDGET:
        latest = latest[-JEV_CHAR_BUDGET:]
    budget = JEV_CHAR_BUDGET - len(latest)
    kept: list[dict] = []
    for turn in reversed(history[:-1]):
        text = turn.get("text") or ""
        if not text:
            continue
        if len(text) > budget:
            if budget > 200:
                kept.append({"role": turn.get("role") or "user", "text": text[-budget:]})
            break
        kept.append({"role": turn.get("role") or "user", "text": text})
        budget -= len(text)
    kept.reverse()
    state = {"latest_user_message": latest, "has_image": False}
    if kept:
        state["recent_turns"] = kept
    return state


class NoEligibleModel(Exception):
    def __init__(self, rejected: list[dict]):
        self.rejected = rejected
        super().__init__("No eligible model")


def infer_task(text: str) -> str:
    if re.search(r"\b(code|debug|function|python|javascript|algorithm|implement|tests?)\b|```", text, re.I):
        return "code"
    if re.search(r"\b(plan|schedule|roadmap|steps)\b", text, re.I):
        return "plan"
    if re.search(r"\b(extract|fields|summari[sz]e|json)\b", text, re.I):
        return "extract"
    return "chat"


def route_request(
    user_text: str,
    context_tokens: int,
    has_image: bool,
    previous_model_id: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
    jev_timeout: float = 5.0,
    history: list[dict] | None = None,
    require_configured: bool = False,
    use_jev: bool = True,
    requested_model: str | None = None,
    on_phase=None,
) -> dict:
    if on_phase:
        on_phase("eligibility")
    eligible, rejected = eligible_models(limits, context_tokens, has_image, require_configured=require_configured)
    if requested_model:
        eligible = [model for model in eligible if model.model_id == requested_model]
    if not eligible:
        raise NoEligibleModel(rejected)

    analysis_source = "single_eligible"
    jev = None
    task_type = "image" if has_image else infer_task(user_text)
    preferred = None

    if on_phase:
        on_phase("analysis")
    if requested_model:
        preferred = requested_model
        analysis_source = "user_selected"
    elif len(eligible) == 1:
        preferred = eligible[0].model_id
    elif has_image:
        analysis_source = "modality"
        task_type = "image"
    elif use_jev:
        try:
            thread = history or [{"role": "user", "text": user_text}]
            jev = ask(
                conversation_state(thread),
                {model.model_id: model.criteria for model in eligible},
                timeout=jev_timeout,
            )
            analysis_source = "jev"
            if jev["task_type"]:
                task_type = jev["task_type"]
            if jev["route"]:
                preferred = jev["route"]
            elif (
                previous_model_id
                and task_type == "chat"
                and any(model.model_id == previous_model_id for model in eligible)
                and len(user_text) <= SHORT_TURN_CHARS
            ):
                preferred = previous_model_id
                analysis_source = "jev_low_confidence_stickiness"
            else:
                analysis_source = "jev_low_confidence_policy"
        except JevUnavailable as exc:
            jev = {"error": str(exc)}
            analysis_source = "fallback_heuristic"
            if (
                previous_model_id
                and task_type == "chat"
                and any(model.model_id == previous_model_id for model in eligible)
                and len(user_text) <= SHORT_TURN_CHARS
            ):
                preferred = previous_model_id
    else:
        analysis_source = "fallback_heuristic"

    ranked = rank_models(eligible, task_type, context_tokens, preferred)
    # Prefer an independent connection for recovery, then the next ranked model.
    fallback = next((model.model_id for model, _ in ranked[1:] if model.provider != ranked[0][0].provider), None)
    if fallback is None and len(ranked) > 1:
        fallback = ranked[1][0].model_id
    return {
        "ranked": [{"model_id": model.model_id, "score": round(score, 4)} for model, score in ranked],
        "selected": ranked[0][0].model_id,
        "fallbacks": [fallback] if fallback else [],
        "rejected": rejected,
        "analysis_source": analysis_source,
        "task_type": task_type,
        "needs_verification": bool(jev and jev.get("needs_verification")),
        "jev": None if jev is None else {key: value for key, value in jev.items() if key != "usage"},
        "limits": {
            "max_latency_ms": limits.max_latency_ms,
            "max_cost_usd": limits.max_cost_usd,
            "residency_regions": sorted(limits.residency_regions),
        },
    }
