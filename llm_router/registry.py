import json
import math
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from llm_router.providers import ProviderConfigError, load_connections


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    provider: str
    modalities: frozenset[str]
    context_window: int
    regions: frozenset[str]
    input_usd_per_million: float
    output_usd_per_million: float
    expected_latency_ms: int
    quality: dict[str, float]
    criteria: str
    # Used only to estimate cost for ranking. Not sent to the provider.
    output_token_cap: int = 1024
    max_output_tokens: int = 65536
    display_name: str = ""
    upstream_model_id: str = ""
    supports_tools: bool = False


@dataclass(frozen=True)
class Limits:
    max_latency_ms: int
    max_cost_usd: float
    residency_regions: frozenset[str]
    deny_model_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if isinstance(self.max_cost_usd, bool) or not isinstance(self.max_cost_usd, (int, float)) or not math.isfinite(self.max_cost_usd) or self.max_cost_usd < 0:
            raise MalformedRequest("Cost ceiling must be finite and nonnegative.")
        if isinstance(self.max_latency_ms, bool) or not isinstance(self.max_latency_ms, int) or self.max_latency_ms < 1:
            raise MalformedRequest("Latency ceiling must be a positive integer.")


class MalformedRequest(Exception):
    pass


class MalformedCatalog(Exception):
    pass


CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.json"
CATALOG_LOCK = threading.RLock()
_MODALITIES = frozenset({"text", "image"})

# Replaced by reload_catalog. BY_ID is the same dict for the life of the process
# so imports of BY_ID stay current when the file changes.
CATALOG: tuple[ModelRecord, ...] = ()
MODEL_SCOPE = ContextVar("models", default=None)


class ModelLookup(dict):
    def __getitem__(self, key):
        scoped = MODEL_SCOPE.get()
        return scoped[key] if scoped is not None else super().__getitem__(key)

    def get(self, key, default=None):
        scoped = MODEL_SCOPE.get()
        return scoped.get(key, default) if scoped is not None else super().get(key, default)


BY_ID: dict[str, ModelRecord] = ModelLookup()


def get_catalog():
    scoped = MODEL_SCOPE.get()
    return tuple(scoped.values()) if scoped is not None else CATALOG
catalog_error: str = ""


def _number(row: dict, key: str, default: float, minimum: float = 0) -> float:
    value = row.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedCatalog(f"{row.get('model_id', 'a model')} has an invalid {key}.")
    if not math.isfinite(value) or value < minimum:
        raise MalformedCatalog(f"{row.get('model_id', 'a model')} has a negative {key}.")
    return float(value)


def _record(row: dict) -> ModelRecord:
    if not isinstance(row, dict):
        raise MalformedCatalog("Each catalog entry must be an object.")
    model_id = row.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise MalformedCatalog("A catalog entry is missing model_id.")
    provider = row.get("provider")
    if provider not in load_connections():
        raise MalformedCatalog(f"{model_id} names an unknown provider connection.")
    modalities = row.get("modalities")
    if not isinstance(modalities, list) or not modalities or any(item not in _MODALITIES for item in modalities):
        raise MalformedCatalog(f"{model_id} modalities must be text, image, or both.")
    if "text" not in modalities:
        raise MalformedCatalog(f"{model_id} must accept text.")
    criteria = row.get("criteria")
    if not isinstance(criteria, str) or not criteria.strip():
        raise MalformedCatalog(f"{model_id} needs a criteria sentence so Jev can tell it apart.")
    quality = row.get("quality")
    if not isinstance(quality, dict) or not quality:
        raise MalformedCatalog(f"{model_id} needs a quality estimate for at least one task.")
    parsed_quality = {}
    for task, score in quality.items():
        if not isinstance(task, str) or isinstance(score, bool) or not isinstance(score, (int, float)):
            raise MalformedCatalog(f"{model_id} has an invalid quality entry.")
        if not 0 <= float(score) <= 1:
            raise MalformedCatalog(f"{model_id} quality for {task} must be between 0 and 1.")
        parsed_quality[task] = float(score)
    regions = row.get("regions", ["us"])
    if not isinstance(regions, list) or not regions or not all(isinstance(item, str) and item for item in regions):
        raise MalformedCatalog(f"{model_id} has an invalid regions list.")
    name = row.get("display_name") or model_id
    if not isinstance(name, str):
        raise MalformedCatalog(f"{model_id} has an invalid display_name.")
    upstream = row.get("upstream_model_id", model_id)
    if not isinstance(upstream, str) or not upstream.strip():
        raise MalformedCatalog(f"{model_id} needs an upstream_model_id.")
    return ModelRecord(
        model_id=model_id.strip(),
        provider=provider,
        modalities=frozenset(modalities),
        context_window=int(_number(row, "context_window", 128_000, minimum=1)),
        regions=frozenset(regions),
        input_usd_per_million=_number(row, "input_usd_per_million", 0.1),
        output_usd_per_million=_number(row, "output_usd_per_million", 0.4),
        expected_latency_ms=int(_number(row, "expected_latency_ms", 1000, minimum=1)),
        quality=parsed_quality,
        criteria=criteria.strip(),
        output_token_cap=int(_number(row, "output_token_cap", 1024, minimum=1)),
        max_output_tokens=int(_number(row, "max_output_tokens", 65536, minimum=1)),
        display_name=name.strip() or model_id.strip(),
        upstream_model_id=upstream.strip(),
        supports_tools=row.get("supports_tools") is True,
    )


def reload_catalog(path: Path | None = None) -> bool:
    if MODEL_SCOPE.get() is not None:
        return True
    with CATALOG_LOCK:
        return _reload_catalog(path)


def _reload_catalog(path: Path | None = None) -> bool:
    """Reread the catalog. A broken file keeps the previous models and records catalog_error."""
    global CATALOG, catalog_error
    source = Path(path) if path is not None else CATALOG_PATH
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
        rows = loaded.get("models") if isinstance(loaded, dict) else None
        if not isinstance(rows, list) or not rows:
            raise MalformedCatalog("catalog.json needs a non-empty models list.")
        if any(not isinstance(row, dict) or not isinstance(row.get("enabled", True), bool) for row in rows):
            raise MalformedCatalog("Catalog entries need a boolean enabled flag.")
        models = tuple(_record(row) for row in rows if row.get("enabled", True))
        if not models:
            raise MalformedCatalog("Enable at least one model.")
        ids = [model.model_id for model in models]
        if len(ids) != len(set(ids)):
            raise MalformedCatalog("catalog.json has two entries with the same model_id.")
    except (OSError, json.JSONDecodeError, MalformedCatalog, ProviderConfigError) as exc:
        catalog_error = str(exc)
        if not BY_ID:
            raise MalformedCatalog(catalog_error) from exc
        return False
    catalog_error = ""
    CATALOG = models
    BY_ID.clear()
    BY_ID.update({model.model_id: model for model in models})
    return True


reload_catalog()

DEFAULT_LIMITS = Limits(
    max_latency_ms=180_000,
    max_cost_usd=1.0,
    residency_regions=frozenset({"us"}),
)


def narrow_limits(tenant: Limits, caller: Limits | None) -> Limits:
    if caller is None:
        return tenant
    if caller.max_latency_ms > tenant.max_latency_ms:
        raise MalformedRequest("A caller cannot raise the latency ceiling.")
    if caller.max_cost_usd > tenant.max_cost_usd:
        raise MalformedRequest("A caller cannot raise the cost ceiling.")
    if not caller.residency_regions.issubset(tenant.residency_regions):
        raise MalformedRequest("A caller cannot add a residency region.")
    return Limits(
        max_latency_ms=caller.max_latency_ms,
        max_cost_usd=caller.max_cost_usd,
        residency_regions=caller.residency_regions,
        deny_model_ids=tenant.deny_model_ids | caller.deny_model_ids,
    )
