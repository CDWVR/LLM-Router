import json
import math
from pathlib import Path
from llm_router.trace import TRACE_LOCK

POLICY_PATH = Path("data/policy.json")
QUALITY_TABLE_PATH = Path("data/quality_table.json")
# One rating must not move the ranker. A rate is used only after this many labels.
QUALITY_MIN_SAMPLES = 20
DEFAULT_POLICY = {
    "quality_table": str(QUALITY_TABLE_PATH),
    "candidate_quality_table": None,
    "shadow": False,
    "canary_fraction": 0,
}


def load_policy(path: Path = POLICY_PATH) -> dict:
    if not path.exists():
        return dict(DEFAULT_POLICY)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return dict(DEFAULT_POLICY)
    except (OSError, ValueError):
        return dict(DEFAULT_POLICY)
    policy = dict(DEFAULT_POLICY)
    policy.update(loaded)
    return policy


def load_rates(path: str | None) -> dict:
    if not path:
        return {}
    table_path = Path(path)
    if not table_path.exists():
        return {}
    try:
        table = json.loads(table_path.read_text(encoding="utf-8"))
        min_samples = max(QUALITY_MIN_SAMPLES, int(table.get("min_samples", QUALITY_MIN_SAMPLES)))
        rates = {}
        for model_id, tasks in (table.get("rates") or {}).items():
            for task_type, cell in tasks.items():
                rate = float(cell["rate"])
                if int(cell.get("n", 0)) >= min_samples and math.isfinite(rate) and 0 <= rate <= 1:
                    rates[(model_id, task_type)] = rate
        return rates
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def build_quality_table(traces_path: Path, output_path: Path, min_samples: int = 1) -> dict:
    counts: dict[tuple[str, str], list[int]] = {}
    if traces_path.exists():
        with TRACE_LOCK:
            lines = traces_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") != "completed" or record.get("include_in_quality") is False:
                continue
            if record.get("label") not in {"pass", "fail"}:
                continue
            model_id = record.get("returned_model_id")
            task_type = record.get("task_type")
            if not model_id or not task_type:
                continue
            counts.setdefault((model_id, task_type), []).append(1 if record["label"] == "pass" else 0)
    rates: dict[str, dict] = {}
    for (model_id, task_type), labels in counts.items():
        if len(labels) < min_samples:
            continue
        rates.setdefault(model_id, {})[task_type] = {
            "rate": round(sum(labels) / len(labels), 4),
            "n": len(labels),
        }
    table = {"min_samples": min_samples, "rates": rates}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOCK:
        temporary = output_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(table, indent=2), encoding="utf-8")
        temporary.replace(output_path)
    return table


def refresh_quality_table(traces_path: Path, output_path: Path = QUALITY_TABLE_PATH) -> None:
    try:
        build_quality_table(traces_path, output_path, min_samples=QUALITY_MIN_SAMPLES)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return


def main() -> None:
    table = build_quality_table(Path("data/traces.jsonl"), QUALITY_TABLE_PATH, min_samples=QUALITY_MIN_SAMPLES)
    print(json.dumps(table, indent=2))


if __name__ == "__main__":
    main()
