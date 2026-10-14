import concurrent.futures
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from starlette.testclient import TestClient
from llm_router.service import create_app
from llm_router.store import ServiceError, digest
from llm_router.providers import load_connections
from llm_router import registry


class SaaSTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"GROQ_API_KEY": "mock-only", "ROUTER_USE_JEV": "0"})
        self.env.start()
        self.app = create_app("sqlite:///"+str(Path(self.folder.name)/"workspace.db"), Fernet.generate_key(), False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        page = self.client.get("/")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        self.store = self.app.state.store

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.folder.cleanup()

    def post(self, path, data):
        return self.client.post(path, json=data, headers={"X-CSRF-Token": self.csrf})

    def test_csrf_and_authentication_boundaries(self):
        self.assertEqual(self.client.post("/workspace", json={"action":"new_project", "name":"x"}).status_code, 403)
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/workspace").headers["x-frame-options"], "DENY")

    def test_tenant_thread_export_isolation(self):
        other = self.store.new_project("other-user", "Other")
        self.store.put(other, "thread", "private-thread", {"history": [], "turns": [], "title": "Secret"})
        self.assertEqual(self.client.get("/export/thread/private-thread").status_code, 404)
        self.assertEqual(self.post("/workspace", {"action":"thread_open", "id":"private-thread"}).status_code, 404)

    def test_api_keys_scoped_and_revocable(self):
        key = self.post("/workspace", {"action":"key_create", "name":"SDK"}).json()["key"]
        self.assertEqual(self.client.get("/v1/models", headers={"Authorization":"Bearer "+key}).status_code, 200)
        listing = self.client.get("/workspace").json()["keys"]
        self.assertNotIn(key, json.dumps(listing))
        self.post("/workspace", {"action":"key_revoke", "id":listing[0]["id"]})
        self.assertEqual(self.client.get("/v1/models", headers={"Authorization":"Bearer "+key}).status_code, 401)

    def test_encrypted_keys_and_concurrent_scopes(self):
        workspace = self.app.state.workspace
        a, b = self.store.new_project("a"), self.store.new_project("b")
        for pid, secret in [(a,"secret-A"),(b,"secret-B")]:
            workspace.save_connection(pid, {"name":"own", "template":"openai", "api_key":secret})
            self.assertNotIn(secret, json.dumps(self.store.get(pid,"connection","own")))
        def read(pid):
            with workspace.scope(pid):
                return load_connections()["own"].key()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(read,[a,b])),["secret-A","secret-B"])

    def test_destination_change_requires_new_key(self):
        workspace=self.app.state.workspace
        workspace.save_connection("local", {"name":"own","template":"openai","api_key":"secret"})
        with self.assertRaisesRegex(ServiceError,"new_destination_requires_key"):
            workspace.save_connection("local", {"name":"own","template":"anthropic"})

    def test_public_custom_endpoint_is_allowlisted(self):
        workspace=self.app.state.workspace
        workspace.saas=True
        with self.assertRaisesRegex(ServiceError,"provider_host_not_approved"):
            workspace.save_connection("local", {"name":"bad","template":"custom","base_url":"https://127.0.0.1/v1","api_key":"secret"})
        self.assertEqual(workspace.connections(self.store.new_project("empty")), {})

    def test_budget_admission_atomic_and_idempotent(self):
        self.store.budget("local", 1)
        def reserve(index):
            try:
                self.store.admit("local",str(index),digest(str(index)),.6)
                return True
            except ServiceError:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(reserve,range(2))),1)

    def test_success_persists_and_duplicate_does_not_call_provider(self):
        def stream(model,messages,timeout,max_tokens,on_delta):
            on_delta("Hello")
            return {"text":"Hello","input_tokens":8,"output_tokens":2}
        payload={"message":"Hello", "client_request_id":"one"}
        with patch("llm_router.execute.complete_streaming",side_effect=stream) as mock:
            response=self.post("/turn",payload)
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn("event: done",response.text)
            duplicate=self.post("/turn",payload)
            self.assertIn("event: done",duplicate.text)
            self.assertEqual(mock.call_count,1)
        state=self.client.get("/state").json()
        self.assertEqual(len(state["turns"]),2)
        threads=self.client.get("/workspace").json()["threads"]
        self.assertEqual(len(threads),1)
        self.assertEqual(self.client.get("/export/thread/"+threads[0]["id"]).status_code,200)

    def test_member_cannot_read_reports_or_manage_keys(self):
        invitation=self.store.invite("local")
        self.store.join("member",invitation)
        token=self.store.create_session("member","local")
        self.client.cookies.set("router_session",token)
        self.csrf=self.store.session(token)["csrf"]
        self.assertEqual(self.post("/workspace",{"action":"key_create","name":"bad"}).status_code,403)
        self.store.put("local","report","private",{"description":"secret"})
        self.assertEqual(self.client.get("/export/report/private").status_code,403)

    def test_invite_single_use(self):
        invite=self.store.invite("local")
        self.store.join("new-member",invite)
        with self.assertRaisesRegex(ServiceError,"invalid_invitation"):
            self.store.join("intruder",invite)

    def test_report_triage_scope(self):
        report=self.post("/reports",{"description":"Something went wrong"}).json()
        self.assertEqual(self.post("/workspace",{"action":"report_status","id":report["id"],"status":"resolved","tags":"ui"}).status_code,200)
        self.assertEqual(self.client.get("/export/report/"+report["id"]).json()["status"],"resolved")

    def test_saas_configuration_fails_closed(self):
        with self.assertRaises(RuntimeError):
            create_app("sqlite:///:memory:",Fernet.generate_key(),True)

    def test_api_stream_and_replay_use_chunk_envelopes(self):
        key=self.store.create_key("local","test")
        headers={"Authorization":"Bearer "+key,"Idempotency-Key":"stream-one"}
        payload={"messages":[{"role":"user","content":"Hello"}],"stream":True}
        def stream(model,messages,timeout,max_tokens,on_delta):
            on_delta("Hello")
            return {"text":"Hello","input_tokens":8,"output_tokens":2}
        with patch("llm_router.execute.complete_streaming",side_effect=stream) as mock:
            first=self.client.post("/v1/chat/completions",json=payload,headers=headers)
            second=self.client.post("/v1/chat/completions",json=payload,headers=headers)
        for response in [first,second]:
            self.assertIn('chat.completion.chunk',response.text)
            self.assertIn('[DONE]',response.text)
        self.assertEqual(mock.call_count,1)

    def test_tool_response_and_capability_filter(self):
        workspace=self.app.state.workspace
        model=workspace.model_rows("local")[0]
        model["supports_tools"]=True
        workspace.save_model("local",model)
        key=self.store.create_key("local","tools")
        tool={"type":"function","function":{"name":"weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}
        calls=[{"id":"call_1","type":"function","function":{"name":"weather","arguments":'{"city":"Delhi"}'}}]
        with patch("llm_router.execute.complete",return_value={"text":"","tool_calls":calls,"finish_reason":"tool_calls","input_tokens":20,"output_tokens":10}):
            response=self.client.post("/v1/chat/completions",json={"model":model["model_id"],"messages":[{"role":"user","content":"Weather?"}],"tools":[tool]},headers={"Authorization":"Bearer "+key})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["tool_calls"],calls)

    def test_remote_images_rejected_and_inline_images_budgeted(self):
        from llm_router.interoperability import normalize_extended
        from llm_router.api import APIRequestError
        from llm_router.budget import context_upper_bound
        with self.assertRaises(APIRequestError):
            normalize_extended({"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"http://169.254.169.254"}}]}]})
        self.assertGreater(context_upper_bound([{"text":"image", "blocks":[{"type":"image_url"}]}]),32768)

    def test_concurrent_report_rate_limit(self):
        def submit(index):
            try:
                self.store.submit_report("local", str(index), {"description": "An issue"})
                return True
            except ServiceError:
                return False
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertEqual(sum(pool.map(submit, range(10))), 5)

    def test_unknown_deployment_mode_rejected(self):
        with patch.dict(os.environ, {"ROUTER_DEPLOYMENT": "saass"}):
            with self.assertRaises(RuntimeError):
                create_app()

    def test_sdk_partial_output_never_falls_back(self):
        from llm_router.gateway import GatewayError
        key = self.store.create_key("local", "stream-failure")
        def stream(model, messages, timeout, max_tokens, on_delta):
            on_delta("Partial")
            raise GatewayError("incomplete_stream")
        with patch("llm_router.execute.complete_streaming", side_effect=stream) as mock:
            response = self.client.post("/v1/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "Hello"}]}, headers={"Authorization": "Bearer "+key})
        self.assertIn("Partial", response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual(mock.call_count, 1)

    def test_generation_preserves_concurrent_thread_metadata(self):
        self.store.put("local", "thread", "race", {"title": "Old", "history": []})
        self.store.put("local", "thread", "race", {"title": "New", "archived": True}, merge=True)
        self.store.put("local", "thread", "race", {"title": "Old", "history": ["answer"]}, preserve=("title", "archived"))
        saved = self.store.get("local", "thread", "race")
        self.assertEqual(saved, {"title": "New", "archived": True, "history": ["answer"]})

    def test_invalid_budget_does_not_change_policy(self):
        response = self.post("/workspace", {"action": "settings", "revision": 0, "profile": "fast", "max_cost_usd": .1, "max_latency_ms": 10000, "daily_limit_usd": -1})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.store.get("local", "settings", "routing"))

    def test_stale_routing_edit_conflicts_without_changing_budget(self):
        payload = {"action": "settings", "revision": 0, "profile": "fast", "max_cost_usd": .1, "max_latency_ms": 10000, "daily_limit_usd": 50}
        self.assertEqual(self.post("/workspace", payload).status_code, 200)
        payload.update(profile="quality", daily_limit_usd=100)
        self.assertEqual(self.post("/workspace", payload).status_code, 409)
        self.assertEqual(self.store.usage("local")["daily_limit_usd"], 50)
        self.assertEqual(self.store.get("local", "settings", "routing")["profile"], "fast")

    def test_generation_pause_blocks_web_and_comparison(self):
        with patch.dict(os.environ, {"ROUTER_GENERATION_ENABLED": "0"}), patch("llm_router.service.run_turn") as generation:
            self.assertEqual(self.post("/turn", {"message": "Hello"}).status_code, 503)
            self.assertEqual(self.post("/compare", {}).status_code, 503)
            generation.assert_not_called()

    def test_stale_connection_edit_preserves_new_credential(self):
        first = {"action":"connection_save", "name":"shared", "template":"openai", "api_key":"first", "revision":0}
        self.assertEqual(self.post("/workspace", first).status_code, 200)
        current = self.app.state.workspace.status("local")
        self.assertEqual(next(p for p in current if p["name"]=="shared")["revision"], 1)
        newer = {**first, "api_key":"rotated", "revision":1}
        self.assertEqual(self.post("/workspace", newer).status_code, 200)
        stale = {**first, "api_key":"", "enabled":False, "revision":1}
        self.assertEqual(self.post("/workspace", stale).status_code, 409)
        connection = self.app.state.workspace.connections("local")["shared"]
        self.assertTrue(connection.enabled)
        self.assertEqual(connection.key(), "rotated")
        self.assertNotIn("rotated", json.dumps(self.client.get("/workspace").json()))

    def test_stale_model_edit_rejected(self):
        model = self.app.state.workspace.model_rows("local")[0]
        self.assertEqual(self.post("/workspace", {"action":"model_save", "model":{**model,"revision":0}}).status_code, 200)
        self.assertEqual(self.post("/workspace", {"action":"model_save", "model":{**model,"revision":0,"enabled":False}}).status_code, 409)
        self.assertTrue(self.store.get("local", "model", model["model_id"])["enabled"])

    def test_report_conflict_preserves_latest_triage(self):
        report = self.post("/reports", {"description":"The model button did not respond"}).json()
        action = {"action":"report_status", "id":report["id"], "status":"investigating", "tags":"frontend", "revision":0}
        self.assertEqual(self.post("/workspace", action).status_code, 200)
        self.assertEqual(self.post("/workspace", {**action,"status":"resolved"}).status_code, 409)
        saved = self.store.get("local", "report", report["id"])
        self.assertEqual(saved["status"], "investigating")
        self.assertEqual(saved["tags"], "frontend")
        self.assertEqual(saved["description"], "The model button did not respond")

    def test_null_or_boolean_revision_cannot_bypass_conflict_check(self):
        for revision in [None, True, -1, "0"]:
            response = self.post("/workspace", {"action":"connection_save", "name":"invalid", "template":"openai", "api_key":"test-only", "revision":revision})
            self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.store.get("local", "connection", "invalid"))

    def test_concurrent_document_edit_has_one_winner(self):
        self.store.put("local", "model", "concurrent", {"enabled":True})
        def change(index):
            try:
                self.store.put("local", "model", "concurrent", {"winner":index}, expected_revision=0)
                return True
            except ServiceError as exc:
                self.assertEqual(exc.code, "configuration_conflict")
                return False
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertEqual(sum(pool.map(change, range(8))), 1)
        self.assertEqual(self.store.get("local", "model", "concurrent")["revision"], 1)
