import json
import threading
from datetime import datetime, timezone
from pathlib import Path

TRACE_PATH = Path("data/traces.jsonl")
TRACE_LOCK = threading.RLock()


def append_trace(record: dict, path: Path = TRACE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**record, "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")


def label_request(request_id: str, owner_id: str, label: str, path: Path = TRACE_PATH) -> bool:
    if label not in {"pass", "fail"} or not request_id or not owner_id:
        return False
    with TRACE_LOCK:
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        for index in range(len(lines) - 1, -1, -1):
            if not lines[index].strip():
                continue
            record = json.loads(lines[index])
            if record.get("status") != "completed" or record.get("request_id") != request_id or record.get("owner_id") != owner_id:
                continue
            record["label"] = label
            lines[index] = json.dumps(record, ensure_ascii=False)
            temporary = path.with_suffix(".jsonl.tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.replace(path)
            return True
    return False
