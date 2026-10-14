from llm_router.eligibility import estimated_cost
from llm_router.registry import ModelRecord
from contextvars import ContextVar

ROUTING_PROFILE = ContextVar("routing_profile", default="balanced")

W_COST = 4.0
W_LATENCY = 0.0004
SHORT_TURN_CHARS = 160


def score_model(
    model: ModelRecord,
    task_type: str,
    context_tokens: int,
    quality_rates: dict | None = None,
) -> float:
    quality_key = "image" if task_type == "image" else task_type
    measured = (quality_rates or {}).get((model.model_id, quality_key))
    quality = model.quality.get(quality_key, 0.5) if measured is None else measured
    weights = {"balanced": (W_COST, W_LATENCY), "fast": (2, .001), "quality": (1, .00002), "private": (W_COST, W_LATENCY)}
    cost, latency = weights.get(ROUTING_PROFILE.get(), weights["balanced"])
    return quality - cost * estimated_cost(model, context_tokens) - latency * model.expected_latency_ms


def rank_models(
    models: list[ModelRecord],
    task_type: str,
    context_tokens: int,
    preferred_id: str | None = None,
    quality_rates: dict | None = None,
) -> list[tuple[ModelRecord, float]]:
    ranked = [
        (model, score_model(model, task_type, context_tokens, quality_rates))
        for model in models
    ]
    ranked.sort(key=lambda item: (-item[1], item[0].input_usd_per_million, item[0].model_id))
    if preferred_id is None:
        return ranked
    preferred = [item for item in ranked if item[0].model_id == preferred_id]
    others = [item for item in ranked if item[0].model_id != preferred_id]
    return preferred + others
