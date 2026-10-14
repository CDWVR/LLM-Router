import llm_router.registry as registry
from llm_router.registry import BY_ID, Limits, ModelRecord
from llm_router.providers import load_connections


def estimated_cost(model: ModelRecord, context_tokens: int) -> float:
    input_cost = model.input_usd_per_million * context_tokens / 1_000_000
    output_cost = model.output_usd_per_million * model.output_token_cap / 1_000_000
    return input_cost + output_cost


def eligible_models(
    limits: Limits,
    context_tokens: int,
    has_image: bool,
    catalog: tuple[ModelRecord, ...] | None = None,
    require_configured: bool = False,
) -> tuple[list[ModelRecord], list[dict]]:
    if catalog is None:
        catalog = registry.get_catalog()
    kept: list[ModelRecord] = []
    rejected: list[dict] = []
    required = "image" if has_image else "text"
    connections = load_connections() if require_configured else {}
    for model in catalog:
        if model.model_id in limits.deny_model_ids:
            rejected.append({"model_id": model.model_id, "reason": "not_permitted", "detail": "denylist"})
            continue
        if require_configured and not connections[model.provider].configured:
            rejected.append({"model_id": model.model_id, "reason": "unavailable", "detail": "provider_not_configured"})
            continue
        if not model.regions.issubset(limits.residency_regions):
            rejected.append({"model_id": model.model_id, "reason": "not_permitted", "detail": "residency"})
            continue
        if required not in model.modalities:
            rejected.append({"model_id": model.model_id, "reason": "unsuitable", "detail": "modality"})
            continue
        if model.context_window <= context_tokens:
            rejected.append({"model_id": model.model_id, "reason": "unsuitable", "detail": "context_window"})
            continue
        from llm_router.budget import output_allowance
        if output_allowance(model, context_tokens, limits.max_cost_usd) < 1:
            rejected.append({"model_id": model.model_id, "reason": "not_permitted", "detail": "cost_ceiling"})
            continue
        if model.expected_latency_ms > limits.max_latency_ms:
            rejected.append({"model_id": model.model_id, "reason": "not_permitted", "detail": "latency_ceiling"})
            continue
        kept.append(model)
    return kept, rejected


def models_by_ids(model_ids: list[str]) -> list[ModelRecord]:
    return [BY_ID[model_id] for model_id in model_ids]
