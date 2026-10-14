import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image
from llm_router import registry, web
from llm_router.api import APIRequestError, normalize_chat
from llm_router.budget import Budget, context_upper_bound
from llm_router.execute import run_turn
from llm_router.gateway import GatewayError, StreamClosed, complete, complete_streaming
from llm_router.providers import BUILTINS, ProviderConfigError, parse_connection, provider_status
from llm_router.trace import append_trace, label_request
from llm_router.validate import matches_schema


HISTORY = [{"role": "user", "text": "Hi"}]
POLICY = {"quality_table": None, "candidate_quality_table": None, "shadow": False, "canary_fraction": 0}


class ExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ROUTER_USE_JEV": "0", "ROUTER_JEV_RESERVE_USD": "0.01"})
        self.policy = patch("llm_router.execute.load_policy", return_value=POLICY)
        self.env.start()
        self.policy.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.policy.stop)

    def turn(self, **kwargs):
        return run_turn(HISTORY, "", None, lambda *args: [HumanMessage(content="Hi")], **kwargs)

    def test_zero_budget_never_calls_a_provider(self):
        from llm_router.pipeline import NoEligibleModel
        with patch("llm_router.execute.complete") as call:
            with self.assertRaises(NoEligibleModel):
                self.turn(limits=replace(registry.DEFAULT_LIMITS, max_cost_usd=0))
        call.assert_not_called()

    def test_reservations_limit_retries_and_cap_output(self):
        caps = []
        def fail(model, messages, timeout_s, max_tokens=None):
            caps.append(max_tokens)
            raise TimeoutError("offline failure")
        with patch("llm_router.execute.complete", side_effect=fail):
            with self.assertRaisesRegex(GatewayError, "cost_budget_exceeded"):
                self.turn(limits=replace(registry.DEFAULT_LIMITS, max_cost_usd=0.00005))
        self.assertEqual(len(caps), 1)
        self.assertLess(caps[0], registry.BY_ID["openai/gpt-oss-20b"].output_token_cap)

    def test_deadline_recomputed_before_retry(self):
        clock = [0.0]
        timeouts = []
        def call(model, messages, timeout_s, max_tokens=None):
            timeouts.append(timeout_s)
            if len(timeouts) == 1:
                clock[0] += 4
                raise TimeoutError
            return {"text": "ok"}
        with patch("time.monotonic", side_effect=lambda: clock[0]), patch("llm_router.execute.complete", side_effect=call):
            self.turn(limits=replace(registry.DEFAULT_LIMITS, max_latency_ms=10000))
        self.assertEqual(timeouts, [10, 6])

    def test_routing_time_is_included(self):
        from llm_router.pipeline import route_request
        clock = [0.0]
        def slow_route(**kwargs):
            decision = route_request(**kwargs)
            clock[0] += 11
            return decision
        with patch("time.monotonic", side_effect=lambda: clock[0]), patch("llm_router.execute.route_request", side_effect=slow_route), patch("llm_router.execute.complete") as call:
            with self.assertRaisesRegex(GatewayError, "latency_budget_exceeded"):
                self.turn(limits=replace(registry.DEFAULT_LIMITS, max_latency_ms=10000))
        call.assert_not_called()

    def test_required_verification_never_streams_rejected_output(self):
        deltas = []
        with patch("llm_router.execute.complete", return_value={"text": "proposed"}), patch("llm_router.execute.verify_answer", return_value=(False, "reject")), patch("llm_router.execute.complete_streaming") as stream:
            with self.assertRaisesRegex(GatewayError, "verification_rejected"):
                self.turn(require_verification=True, on_delta=deltas.append)
        stream.assert_not_called()
        self.assertEqual(deltas, [])

    def test_full_schema_and_valid_fallback(self):
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer", "minimum": 1}}, "additionalProperties": False}
        self.assertEqual(matches_schema('{"n":"bad"}', schema), (False, "schema_mismatch"))
        with patch("llm_router.execute.complete", side_effect=[{"text": '{"n":"bad"}'}, {"text": '{"n":2}'}]):
            result = self.turn(response_schema=schema)
        self.assertEqual(result["model_id"], "openai/gpt-oss-120b")
        self.assertEqual(result["validation"], "ok")

    def test_terminal_cancellation_trace_and_prompt_privacy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "traces.jsonl"
            with patch("llm_router.execute.complete_streaming", side_effect=StreamClosed), patch.dict(os.environ, {"ROUTER_LOG_PROMPTS": "0"}):
                with self.assertRaises(StreamClosed):
                    self.turn(on_delta=lambda _: None, trace_path=path)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["status"] for r in records], ["started", "cancelled"])
            self.assertTrue(all("timestamp" in r and "user_text" not in r for r in records))

    def test_cancelled_buffered_answer_is_never_committed(self):
        cancelled = threading.Event()
        def answer(*args, **kwargs):
            cancelled.set()
            return {"text": "never return this"}
        with patch("llm_router.execute.complete", side_effect=answer):
            with self.assertRaises(StreamClosed):
                self.turn(cancelled=cancelled.is_set)

    def test_multilingual_context_is_not_characters_divided_by_four(self):
        self.assertGreaterEqual(context_upper_bound([{"text": "नमस्ते"}]), len("नमस्ते".encode("utf-8")))

    def test_cross_connection_fallback_after_rate_limit(self):
        base = registry.BY_ID["openai/gpt-oss-20b"]
        independent = replace(base, model_id="independent", provider="openai", upstream_model_id="test-model", quality={"chat": 0.8})
        models = (base, registry.BY_ID["openai/gpt-oss-120b"], independent)
        calls = []
        def call(model_id, messages, timeout_s, max_tokens=None):
            calls.append(model_id)
            if model_id != "independent":
                raise GatewayError("rate_limited")
            return {"text": "recovered"}
        with patch("llm_router.execute.reload_catalog"), patch.object(registry, "CATALOG", models), patch.dict(registry.BY_ID, {m.model_id: m for m in models}), patch("llm_router.execute.complete", side_effect=call):
            result = self.turn()
        self.assertEqual(calls, [base.model_id, "independent"])
        self.assertEqual(result["model_id"], "independent")


class ProviderContractTests(unittest.TestCase):
    def test_real_http_trickling_stream_respects_deadline(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from llm_router.providers import Connection
        stop = threading.Event()
        class TricklingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b"data: ")
                    self.wfile.flush()
                    while not stop.wait(0.05):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), TricklingHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = Connection("groq", "openai_compatible", f"http://127.0.0.1:{server.server_port}", None)
        started = time.monotonic()
        try:
            with patch("llm_router.gateway.load_connections", return_value={"groq": connection}):
                with self.assertRaisesRegex(GatewayError, "latency_budget_exceeded"):
                    complete_streaming("openai/gpt-oss-20b", [HumanMessage(content="hi")], 1, 64, lambda _: None)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_config_rejects_insecure_remote_endpoints_and_inline_secrets(self):
        for url in ["http://example.com/v1", "https://secret@example.com/v1", "https://example.com/v1?api_key=secret"]:
            with self.assertRaises(ProviderConfigError):
                parse_connection("custom", {"adapter": "openai_compatible", "base_url": url, "api_key_env": "CUSTOM_API_KEY"})
        local = parse_connection("local", {"adapter": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None})
        self.assertTrue(local.configured)

    def test_status_does_not_expose_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-value"}):
            serialized = json.dumps(provider_status())
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("api_key", serialized)

    def transport(self, provider, payload, stream=False):
        model = replace(registry.BY_ID["openai/gpt-oss-20b"], provider=provider, upstream_model_id="provider-native-id")
        captured = []
        def open_fake(request, timeout):
            captured.append(request)
            return io.BytesIO(payload)
        messages = [SystemMessage(content="Be concise."), HumanMessage(content=[{"type": "text", "text": "Describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}}])]
        pieces = []
        with patch.dict(registry.BY_ID, {model.model_id: model}), patch.dict(os.environ, {BUILTINS[provider].api_key_env: "test-key"}), patch("llm_router.gateway.open_request", side_effect=open_fake):
            if stream:
                result = complete_streaming(model.model_id, messages, 5, 123, pieces.append)
            else:
                result = complete(model.model_id, messages, 5, 123)
        return captured[0], result, pieces

    def test_openai_request_alias_system_and_token_cap(self):
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}).encode()
        request, result, _ = self.transport("openai", payload)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "provider-native-id")
        self.assertEqual(body["max_completion_tokens"], 123)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(result["input_tokens"], 7)

    def test_output_limit_is_reported_instead_of_claiming_normal_completion(self):
        payload = b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}'
        _, result, _ = self.transport("openai", payload)
        self.assertEqual(result["finish_reason"], "length")

    def test_anthropic_native_image_and_auth_conversion(self):
        request, result, _ = self.transport("anthropic", b'{"content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":7,"output_tokens":2}}')
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(body["system"], "Be concise.")
        self.assertEqual(body["messages"][0]["content"][1]["source"]["type"], "base64")
        self.assertEqual(result["output_tokens"], 2)

    def test_openai_stream_usage_and_terminal(self):
        payload = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        _, result, pieces = self.transport("openai", payload, True)
        self.assertEqual(pieces, ["Hello"])
        self.assertEqual(result["input_tokens"], 5)

    def test_anthropic_stream_usage_and_terminal(self):
        events = [{"type": "message_start", "message": {"usage": {"input_tokens": 8}}}, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}, {"type": "message_delta", "usage": {"output_tokens": 2}}, {"type": "message_stop"}]
        payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        _, result, pieces = self.transport("anthropic", payload, True)
        self.assertEqual(pieces, ["Hi"])
        self.assertEqual(result["output_tokens"], 2)

    def test_truncated_stream_is_not_completed(self):
        with self.assertRaisesRegex(GatewayError, "incomplete_stream"):
            self.transport("openai", b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', True)

    def test_http_errors_are_structured_without_provider_body(self):
        def reject(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "secret diagnostic", {}, io.BytesIO(b"private"))
        with patch.dict(os.environ, {"GROQ_API_KEY": "fake"}), patch("llm_router.gateway.open_request", side_effect=reject):
            with self.assertRaisesRegex(GatewayError, "provider_auth_failed") as error:
                complete("openai/gpt-oss-20b", [HumanMessage(content="hi")], 5)
        self.assertFalse(error.exception.retryable)
        self.assertNotIn("secret", str(error.exception))


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.trace_patch = patch.object(web, "TRACE_PATH", Path(self.folder.name) / "traces.jsonl")
        self.env = patch.dict(os.environ, {"ROUTER_API_TOKEN": "test-router-token", "ROUTER_PASSWORD": "", "ROUTER_USE_JEV": "0", "GROQ_API_KEY": "fake"})
        self.trace_patch.start()
        self.env.start()
        self.refresh_patch = patch("llm_router.web.refresh_quality_table")
        self.refresh_patch.start()
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.trace_patch.stop()
        self.env.stop()
        self.refresh_patch.stop()
        self.folder.cleanup()

    def request(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), response.headers

    def session(self):
        _, page, headers = self.request("/")
        cookie = "; ".join(value.split(";", 1)[0] for value in headers.get_all("Set-Cookie"))
        import re
        csrf = re.search(rb'name="csrf-token" content="([^"]+)"', page)[1].decode()
        return {"Cookie": cookie, "X-CSRF-Token": csrf}

    def test_api_requires_token_and_has_model_discovery(self):
        status, _, _ = self.request("/v1/models")
        self.assertEqual(status, 401)
        status, payload, _ = self.request("/v1/models", headers={"Authorization": "Bearer test-router-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"][0]["id"], "auto")

    def test_sdk_shaped_chat_endpoint_preserves_system_messages(self):
        seen = []
        def answer(model, messages, timeout_s, max_tokens=None):
            seen.extend(messages)
            return {"text": "hello", "input_tokens": 4, "output_tokens": 1}
        with patch("llm_router.execute.complete", side_effect=answer):
            status, payload, _ = self.request("/v1/chat/completions", {"model": "auto", "messages": [{"role": "system", "content": "Be concise"}, {"role": "user", "content": "Hello"}], "stream": False}, {"Authorization": "Bearer test-router-token"})
        data = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        self.assertEqual(data["usage"]["total_tokens"], 5)
        self.assertEqual(seen[0].type, "system")

    def test_forged_session_cannot_mutate_state(self):
        status, _, _ = self.request("/reset", {}, {"Cookie": "router_session=forged; router_csrf=forged", "X-CSRF-Token": "forged"})
        self.assertEqual(status, 403)

    def test_json_non_object_rejected(self):
        status, _, _ = self.request("/reset", [], self.session())
        self.assertEqual(status, 400)

    def test_reset_clears_routing_state(self):
        headers = self.session()
        token = headers["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        state = web._sessions[token]
        state["last_model"], state["verify"] = "old", True
        status, _, _ = self.request("/reset", {}, headers)
        self.assertEqual(status, 200)
        self.assertIsNone(state["last_model"])
        self.assertFalse(state["verify"])

    def test_feedback_is_bound_to_owner_and_request(self):
        headers_a, headers_b = self.session(), self.session()
        token = headers_a["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        owner = web._sessions[token]["owner_id"]
        append_trace({"status": "completed", "request_id": "answer-a", "owner_id": owner}, web.TRACE_PATH)
        status, _, _ = self.request("/label", {"request_id": "answer-a", "label": "pass"}, headers_b)
        self.assertEqual(status, 400)
        status, _, _ = self.request("/label", {"request_id": "answer-a", "label": "pass"}, headers_a)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(web.TRACE_PATH.read_text())["label"], "pass")

    def test_busy_generation_does_not_queue_or_reset_history(self):
        headers = self.session()
        token = headers["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        state = web._sessions[token]
        state["lock"].acquire()
        try:
            status, _, _ = self.request("/reset", {}, headers)
            self.assertEqual(status, 409)
        finally:
            state["lock"].release()
        web._generation_slot.acquire()
        try:
            status, _, _ = self.request("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, {"Authorization": "Bearer test-router-token"})
            self.assertEqual(status, 429)
        finally:
            web._generation_slot.release()

    def test_browser_events_follow_backend_phases(self):
        headers = self.session()
        def stream(model, messages, timeout_s, max_tokens, on_delta):
            on_delta("hello")
            return {"text": "hello"}
        with patch("llm_router.execute.complete_streaming", side_effect=stream):
            status, payload, _ = self.request("/turn", {"message": "Hi", "client_request_id": "browser-test"}, headers)
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertLess(text.index('"phase": "eligibility"'), text.index('"phase": "analysis"'))
        self.assertLess(text.index('"phase": "writing"'), text.index("event: delta"))
        self.assertIn("event: done", text)
        self.assertIn('"request_id"', text)

    def test_cancel_is_session_and_active_request_bound(self):
        headers_a, headers_b = self.session(), self.session()
        token = headers_a["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        state = web._sessions[token]
        state["active_request"] = "active"
        state["cancel"].clear()
        self.request("/cancel", {"client_request_id": "active"}, headers_b)
        self.assertFalse(state["cancel"].is_set())
        self.request("/cancel", {"client_request_id": "stale"}, headers_a)
        self.assertFalse(state["cancel"].is_set())
        self.request("/cancel", {"client_request_id": "active"}, headers_a)
        self.assertTrue(state["cancel"].is_set())


class RequestValidationTests(unittest.TestCase):
    def test_invalid_semantic_router_numbers_do_not_escape(self):
        from llm_router.jev_client import choice_is_actionable
        self.assertFalse(choice_is_actionable({"choice": "a", "confidence": "invalid", "probabilities": {"a": 0.9}}, {"a"}))
        self.assertFalse(choice_is_actionable({"choice": "a", "confidence": 0.9, "probabilities": {"a": float("nan")}}, {"a"}))

    def test_quality_files_are_safe_to_edit_and_cannot_bypass_sample_floor(self):
        from llm_router.quality import load_rates
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "quality.json"
            path.write_text("broken")
            self.assertEqual(load_rates(str(path)), {})
            path.write_text(json.dumps({"min_samples": 1, "rates": {"a": {"chat": {"n": 1, "rate": 1}}}}))
            self.assertEqual(load_rates(str(path)), {})
    def test_unsupported_api_features_are_explicit(self):
        for body in [{"messages": HISTORY, "stream": True}, {"messages": [{"role": "user", "content": "hi"}], "tools": []}, {"messages": [{"role": "user", "content": "hi"}], "router": {"max_cost_usd": float("nan")}}]:
            with self.assertRaises(APIRequestError):
                normalize_chat(body)

    def test_remote_schema_references_rejected(self):
        with self.assertRaises(APIRequestError):
            normalize_chat({"messages": [{"role": "user", "content": "hi"}], "response_format": {"type": "json_schema", "json_schema": {"schema": {"$ref": "https://example.com/schema"}}}})

    def test_image_validates_actual_decode_not_just_magic(self):
        self.assertEqual(web._save_image(base64.b64encode(b"\x89PNG\r\n\x1a\nrest").decode(), "image/png"), "error:invalid_image")
        with tempfile.TemporaryDirectory() as folder, patch.object(web, "UPLOAD_ROOT", Path(folder)):
            buffer = io.BytesIO()
            Image.new("RGB", (2, 2)).save(buffer, format="PNG")
            saved = web._save_image(base64.b64encode(buffer.getvalue()).decode(), "image/png")
            self.assertTrue(Path(saved).exists())


if __name__ == "__main__":
    unittest.main()
