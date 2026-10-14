import json
import os
import math
import urllib.error
import urllib.request

JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CONFIDENCE_FLOOR = 0.6
PROBABILITY_MARGIN = 0.1


class JevUnavailable(Exception):
    pass


def post_decisions(body: dict, timeout: float) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise JevUnavailable("OPENROUTER_API_KEY is not set.")
    request = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise JevUnavailable(str(exc)) from exc


def choice_is_actionable(answer: dict, option_keys: set[str]) -> bool:
    if not isinstance(answer, dict):
        return False
    choice = answer.get("choice")
    probabilities = answer.get("probabilities") or {}
    if not isinstance(choice, str) or not isinstance(probabilities, dict) or choice not in option_keys or choice not in probabilities:
        return False
    try:
        confidence = float(answer.get("confidence") or 0)
        selected = float(probabilities[choice])
        others = [float(value) for key, value in probabilities.items() if key != choice]
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in [confidence, selected, *others]):
        return False
    runner_up = max(others) if others else 0.0
    return confidence >= CONFIDENCE_FLOOR and selected - runner_up >= PROBABILITY_MARGIN


def ask(state: dict, eligible_criteria: dict[str, str], timeout: float) -> dict:
    questions = {
        "task_type": {
            "type": "choice",
            "instructions": "What kind of task is the latest user message?",
            "criteria": {
                "chat": "Greeting, short question, definition, rewrite, or basic arithmetic.",
                "code": "Write, debug, or explain an algorithm and implement it.",
                "extract": "Pull specific fields or a summary out of supplied text.",
                "plan": "Multi-step planning or a long set of instructions.",
            },
        },
        "needs_verification": {
            "type": "noul",
            "instructions": "Should a second model check the answer before it is returned?",
            "criteria": {
                "true": "The task is high stakes or the user asked for a checked result.",
                "false": "A single model answer is enough.",
            },
        },
    }
    if len(eligible_criteria) >= 2:
        questions["route"] = {
            "type": "choice",
            "instructions": "Choose the least costly option that can complete the task. The options are already allowed.",
            "criteria": eligible_criteria,
        }
    payload = post_decisions(
        {"model": JEV_MODEL, "state": state, "questions": questions},
        timeout=timeout,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise JevUnavailable("Invalid semantic-router response.")
    answers = payload.get("answers") or {}
    task = answers.get("task_type") or {}
    route = answers.get("route") or {}
    verification = answers.get("needs_verification") or {}
    if any(not isinstance(answer, dict) for answer in [task, route, verification]):
        raise JevUnavailable("Invalid semantic-router response.")
    try:
        verify_probability = float(verification.get("noul") or 0)
    except (TypeError, ValueError) as exc:
        raise JevUnavailable("Invalid semantic-router response.") from exc
    task_keys = set(questions["task_type"]["criteria"])
    route_keys = set(eligible_criteria)
    return {
        "task_type": task.get("choice") if choice_is_actionable(task, task_keys) else None,
        "task_confidence": task.get("confidence"),
        "task_probabilities": task.get("probabilities"),
        "route": route.get("choice") if choice_is_actionable(route, route_keys) else None,
        "route_confidence": route.get("confidence"),
        "route_probabilities": route.get("probabilities"),
        "needs_verification": math.isfinite(verify_probability) and 0.8 <= verify_probability <= 1,
        "model": payload.get("model"),
        "usage": payload.get("usage"),
    }
