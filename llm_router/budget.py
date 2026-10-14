"""Conservative catalog-priced reservations; not a provider billing guarantee."""
import math
import json
from dataclasses import dataclass

from llm_router.registry import ModelRecord


def context_upper_bound(history: list[dict], has_image: bool = False) -> int:
    # UTF-8 bytes avoid the usual characters/4 underestimate on multilingual text.
    # Image charges vary by provider. Keep a deliberately large fixed allowance.
    images = sum(1 for turn in history for block in turn.get("blocks", []) if block.get("type") == "image_url")
    calls = sum(len(json.dumps(turn.get("tool_calls", []))) for turn in history if turn.get("tool_calls"))
    return max(1, sum(len(turn["text"].encode("utf-8")) + 32 for turn in history) + calls + 32_768 * (images + int(has_image)))


def output_allowance(model: ModelRecord, context_tokens: int, remaining_usd: float, requested: int | None = None) -> int:
    input_cost = model.input_usd_per_million * context_tokens / 1_000_000
    if input_cost > remaining_usd:
        return 0
    cap = min(model.output_token_cap, model.max_output_tokens, model.context_window - context_tokens)
    if requested is not None:
        cap = min(cap, requested)
    if model.output_usd_per_million:
        affordable = math.floor(max(0, remaining_usd - input_cost) * 1_000_000 / model.output_usd_per_million)
        cap = min(cap, affordable)
    return max(0, cap)


@dataclass
class Budget:
    ceiling: float
    reserved: float = 0

    @property
    def remaining(self) -> float:
        return max(0, self.ceiling - self.reserved)

    def reserve(self, model: ModelRecord, context_tokens: int, requested: int | None = None) -> tuple[int, float]:
        cap = output_allowance(model, context_tokens, self.remaining, requested)
        if cap < 1:
            return 0, 0
        cost = (model.input_usd_per_million * context_tokens + model.output_usd_per_million * cap) / 1_000_000
        self.reserved += cost
        return cap, cost
