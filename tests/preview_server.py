"""Mock-only browser preview. No dotenv loading, credentials, or provider network calls."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_router import web, reports


def answer(model_id, messages, timeout_s, max_tokens=None):
    return {"text": "## Your model, one interface\nRouter selects a configured model and shows why.\n- Credentials stay on the server.\n- Budgets and deadlines apply to retries.\n- Required checks withhold rejected answers.", "input_tokens": 48, "output_tokens": 42}


def stream(model_id, messages, timeout_s, max_tokens, on_delta):
    result = answer(model_id, messages, timeout_s, max_tokens)
    for index in range(0, len(result["text"]), 15):
        time.sleep(0.12)
        on_delta(result["text"][index:index + 15])
    return result


if __name__ == "__main__":
    # Credentials are sent nowhere; readiness simply needs a nonempty mock key.
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
        "GROQ_API_KEY": "mock-only", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENROUTER_API_KEY": "",
        "ROUTER_PASSWORD": "", "ROUTER_HOST": "127.0.0.1", "ROUTER_PORT": "8877", "ROUTER_USE_JEV": "0",
    }), patch.object(reports, "REPORT_DB", Path(folder) / "reports.sqlite3"), patch.object(web, "TRACE_PATH", Path(folder) / "traces.jsonl"), patch("llm_router.web.refresh_quality_table"), patch("llm_router.execute.complete", side_effect=answer), patch("llm_router.execute.complete_streaming", side_effect=stream), patch("llm_router.execute.verify_answer", return_value=(True, "accept")):
        web.serve()
