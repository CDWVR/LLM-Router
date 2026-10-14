"""ASGI application for local development and OIDC-authenticated SaaS deployments."""
import asyncio
import base64
import hashlib
import io
import json
import math
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from . import registry
from .api import normalize_chat, APIRequestError
from .execute import run_turn
from .gateway import StreamClosed, GatewayError, REQUEST_OPTIONS
from .interoperability import normalize_extended
from .messages import conversation_messages
from .pipeline import NoEligibleModel
from .store import Store, ServiceError, digest
from .workspace import Workspace, encryption_key
from .configuration import database_url as normalize_database_url
from .probes import ProbeHostBoundary, RAILWAY_PROBE_HOST
from .web import WEB_ROOT, _decision_view, _model_fields, format_sse

SAFE_ERRORS = {"cost_budget_exceeded", "model_call_failed", "provider_not_configured", "provider_auth_failed", "latency_budget_exceeded", "rate_limited", "output_validation_failed", "verification_unavailable", "verification_rejected", "empty_response", "incomplete_stream"}


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ServiceError("invalid_number")
    return value


def text(value, maximum=4000, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ServiceError("invalid_text")
    return value.strip()


class SecurityHeaders:
    def __init__(self, app, saas=False):
        self.app, self.saas = app, saas

    async def __call__(self, scope, receive, send):
        started, request_id = time.monotonic(), uuid.uuid4().hex
        status = 500
        async def secured(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
                message.setdefault("headers", []).extend([(b"content-security-policy", b"default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")])
                if self.saas:
                    message["headers"].append((b"strict-transport-security", b"max-age=31536000"))
                if not scope.get("path", "").startswith(("/vendor/", "/fonts/")):
                    message["headers"].append((b"cache-control", b"no-store"))
            await send(message)
        try:
            await self.app(scope, receive, secured)
        finally:
            if scope["type"] == "http":
                logging.getLogger("router.requests").info(json.dumps({"request_id":request_id,"method":scope["method"],"route":getattr(scope.get("route"),"path","unmatched"),"status":status,"elapsed_ms":round((time.monotonic()-started)*1000)}))


def create_app(database_url=None, secret_key=None, saas=None):
    if saas is None and os.environ.get("ROUTER_DEPLOYMENT", "local") not in {"local", "saas"}:
        raise RuntimeError("ROUTER_DEPLOYMENT must be local or saas")
    saas = os.environ.get("ROUTER_DEPLOYMENT", "local") == "saas" if saas is None else saas
    database_url = normalize_database_url(database_url or os.environ.get("DATABASE_URL", "sqlite:///data/workspace.sqlite3"))
    public_url = os.environ.get("ROUTER_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/")
    issuer = os.environ.get("OIDC_ISSUER", "").rstrip("/")
    if saas and (not database_url.startswith("postgresql+psycopg://") or not public_url.startswith("https://") or not issuer.startswith("https://") or not os.environ.get("OIDC_CLIENT_ID") or not os.environ.get("OIDC_CLIENT_SECRET")):
        raise RuntimeError("SaaS requires PostgreSQL, HTTPS ROUTER_PUBLIC_URL, OIDC_ISSUER, OIDC_CLIENT_ID, and OIDC_CLIENT_SECRET")
    Path("data").mkdir(exist_ok=True)
    key = secret_key or encryption_key(saas)
    store, active = Store(database_url, auto_migrate=not saas), {}
    workers = set()
    workspace = Workspace(store, key, saas)
    oauth = None
    if saas:
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register("identity", client_id=os.environ["OIDC_CLIENT_ID"], client_secret=os.environ["OIDC_CLIENT_SECRET"], server_metadata_url=issuer+"/.well-known/openid-configuration", client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256", "timeout": 10})
    else:
        store.new_project("local-user", "Personal workspace", "local")

    def who(request, api=False):
        if api:
            header = request.headers.get("authorization", "")
            project = store.key_project(header[7:]) if header.startswith("Bearer ") else None
            if not project:
                raise ServiceError("unauthorized", 401)
            return {"project_id": project, "user_id": "api", "role": "member"}
        session = store.session(request.cookies.get("router_session"))
        if not session:
            raise ServiceError("unauthorized", 401)
        session["role"] = store.role(session["user_id"], session["project_id"])
        if request.method != "GET" and not secrets.compare_digest(request.headers.get("x-csrf-token", "").encode(), session["csrf"].encode()):
            raise ServiceError("forbidden", 403)
        return session

    def admin(session):
        if session["role"] not in {"owner", "admin"}:
            raise ServiceError("admin_required", 403)

    async def body(request):
        raw = bytearray()
        if len([h for h in request.scope["headers"] if h[0] == b"content-length"]) > 1:
            raise ServiceError("invalid_content_length")
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 12_000_000:
                    raise ServiceError("payload_too_large", 413)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise ServiceError("invalid_json") from None

    def cookie(response, token):
        response.set_cookie("router_session", token, max_age=86400, httponly=True, secure=saas, samesite="lax", path="/")
        return response

    async def home(request):
        session = await run_in_threadpool(store.session, request.cookies.get("router_session"))
        token = None
        if not session and not saas:
            token = await run_in_threadpool(store.create_session, "local-user", "local")
            session = await run_in_threadpool(store.session, token)
        page = (WEB_ROOT/"index.html").read_text(encoding="utf-8").replace("{{CSRF}}", session["csrf"] if session else "")
        response = HTMLResponse(page)
        return cookie(response, token) if token else response

    async def login(request):
        if not oauth:
            return RedirectResponse("/")
        return await oauth.identity.authorize_redirect(request, public_url+"/auth/callback")

    async def callback(request):
        if not oauth:
            raise ServiceError("not_found", 404)
        try:
            token = await oauth.identity.authorize_access_token(request)
            user = token["userinfo"]
            if user.get("iss") != issuer or not user.get("sub"):
                raise ValueError
        except Exception:
            return HTMLResponse('<h1>Sign-in could not be verified</h1><a href="/login">Try again</a>', status_code=401)
        user_id = digest(issuer+":"+user["sub"])
        memberships = await run_in_threadpool(store.memberships, user_id)
        project = memberships[0]["id"] if memberships else await run_in_threadpool(store.new_project, user_id)
        request.session.clear()
        return cookie(RedirectResponse("/", status_code=303), await run_in_threadpool(store.create_session, user_id, project))

    async def state(request):
        try:
            session = await run_in_threadpool(who, request)
        except ServiceError:
            return JSONResponse({"locked": True, "external_login": True, "turns": []})
        project = session["project_id"]
        thread = await run_in_threadpool(store.get, project, "thread", session.get("thread_id") or "") or {}
        models = await run_in_threadpool(workspace.model_rows, project)
        return JSONResponse({"turns": thread.get("turns", []), "has_image": bool(thread.get("attachment")), "verify": thread.get("verify", False), "locked": False, "catalog_error": "",
                             "providers": await run_in_threadpool(workspace.status, project), "models": [{"id": m["model_id"], "name": m["display_name"], "provider": m["provider"], "modalities": m["modalities"]} for m in models if m.get("enabled", True)]})

    async def workspace_get(request):
        session = await run_in_threadpool(who, request)
        project = session["project_id"]
        def read():
            threads = [{"id": t["id"], "title": t.get("title", "Untitled"), "archived": t.get("archived", False), "updated": t["updated"]} for t in store.list(project, "thread")]
            return {"project": project, "role": session["role"], "projects": store.memberships(session["user_id"]), "thread_id": session.get("thread_id"), "threads": threads,
                    "providers": workspace.status(project), "models": workspace.model_rows(project), "settings": store.get(project, "settings", "routing") or {"profile": "balanced", "max_cost_usd": 1, "max_latency_ms": 180000, "allowed_providers": []},
                    "usage": store.usage(project), "members": store.member_list(project) if session["role"] in {"owner", "admin"} else [], "keys": store.list_keys(project) if session["role"] in {"owner", "admin"} else [], "reports": [{k:v for k,v in r.items() if k != "screenshot"} for r in store.list(project, "report")] if session["role"] in {"owner", "admin"} else [], "evaluations": store.list(project, "evaluation", 50), "saas": saas}
        return JSONResponse(await run_in_threadpool(read))

    async def manage(request):
        session = await run_in_threadpool(who, request)
        data = await body(request)
        project, action = session["project_id"], data.get("action")
        def change():
            if action == "logout":
                store.logout(session)
            elif action == "join":
                joined = store.join(session["user_id"], text(data.get("invitation"), 200))
                store.set_session(session, project_id=joined, thread_id="")
            elif action == "new_project":
                project_id = store.new_project(session["user_id"], text(data.get("name"), 80))
                store.set_session(session, project_id=project_id, thread_id="")
            elif action == "switch_project":
                target = text(data.get("id"), 64)
                if not store.role(session["user_id"], target):
                    raise ServiceError("forbidden", 403)
                store.set_session(session, project_id=target, thread_id="")
            elif action in {"thread_open", "thread_rename", "thread_archive"}:
                identifier = text(data.get("id"), 64)
                thread = store.get(project, "thread", identifier)
                if thread is None:
                    raise ServiceError("not_found", 404)
                if action == "thread_open":
                    store.set_session(session, thread_id=identifier)
                else:
                    if action == "thread_rename":
                        thread = {"title": text(data.get("title"), 100)}
                    else:
                        thread = {"archived": data.get("archived") is True}
                    store.put(project, "thread", identifier, thread, merge=True)
            elif action == "save_evaluation":
                store.put(project, "evaluation", uuid.uuid4().hex, {"prompt": text(data.get("prompt"), 20000), "notes": text(data.get("notes", "Saved comparison"), 4000)}, 50)
            else:
                admin(session)
                if action == "invite":
                    return {"invitation": store.invite(project)}
                if action == "remove_member":
                    store.remove_member(project, text(data.get("user_id"), 256))
                    return {"ok": True}
                if action == "connection_save":
                    return workspace.save_connection(project, data)
                if action == "connection_test":
                    if not store.allow_request("discovery:"+project, 5):
                        raise ServiceError("rate_limited", 429)
                    return workspace.discover(project, text(data.get("name"), 64))
                if action == "model_save":
                    return workspace.save_model(project, data.get("model", {}))
                if action == "settings":
                    profile = data.get("profile")
                    if profile not in {"balanced", "fast", "quality", "private"}:
                        raise ServiceError("invalid_profile")
                    allowed = data.get("allowed_providers", [])
                    if not isinstance(allowed, list) or any(not isinstance(x, str) or x not in workspace.connections(project) for x in allowed):
                        raise ServiceError("invalid_providers")
                    settings = {"profile": profile, "max_cost_usd": number(data.get("max_cost_usd"), .000001, 1), "max_latency_ms": int(number(data.get("max_latency_ms"), 1000, 180000)), "allowed_providers": allowed}
                    daily_limit = number(data.get("daily_limit_usd"), .01, 10000)
                    revision = data.get("revision")
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ServiceError("invalid_revision")
                    store.save_routing(project, settings, daily_limit, revision)
                elif action == "key_create":
                    return {"key": store.create_key(project, text(data.get("name"), 80))}
                elif action == "key_revoke":
                    store.revoke_key(project, text(data.get("id"), 32))
                elif action == "report_status":
                    identifier = text(data.get("id"), 128)
                    report = store.get(project, "report", identifier)
                    if not report:
                        raise ServiceError("not_found", 404)
                    if data.get("status") not in {"new", "investigating", "resolved"}:
                        raise ServiceError("invalid_status")
                    report["status"] = data["status"]
                    report["tags"] = text(data.get("tags", ""), 200, 0)
                    store.put(project, "report", identifier, report)
                else:
                    raise ServiceError("unknown_action")
            return {"ok": True}
        result = await run_in_threadpool(change)
        try:
            await run_in_threadpool(store.put, project, "audit", uuid.uuid4().hex, {"action": action, "actor": session["user_id"]}, 10000)
        except Exception:
            logging.getLogger("router.audit").error("Audit storage unavailable")
        return JSONResponse(result)

    async def export(request):
        session = await run_in_threadpool(who, request)
        kind, identifier = request.path_params["kind"], request.path_params["identifier"]
        if kind not in {"thread", "report", "evaluation"}:
            raise ServiceError("not_found", 404)
        if kind == "report":
            admin(session)
        doc = await run_in_threadpool(store.get, session["project_id"], kind, identifier)
        if doc is None:
            raise ServiceError("not_found", 404)
        if request.query_params.get("image") == "1" and kind == "report":
            if not doc.get("screenshot"):
                raise ServiceError("not_found", 404)
            return Response(base64.b64decode(doc["screenshot"]), media_type="image/png")
        return JSONResponse(doc, headers={"Content-Disposition": 'attachment; filename="router-export.json"'})

    async def report(request):
        session = await run_in_threadpool(who, request)
        data = await body(request)
        description = text(data.get("description"), 4000, 10)
        screenshot = data.get("screenshot")
        def save():
            image = None
            if screenshot is not None:
                try:
                    if not isinstance(screenshot, str) or len(screenshot) > 3_000_000:
                        raise ValueError
                    with Image.open(io.BytesIO(base64.b64decode(screenshot, validate=True))) as source:
                        if source.format != "PNG" or source.width*source.height > 4_000_000:
                            raise ValueError
                        source.load()
                        out = io.BytesIO(); source.convert("RGB").save(out, format="PNG")
                        if out.tell() > 2_000_000:
                            raise ValueError
                        image = base64.b64encode(out.getvalue()).decode()
                except Exception:
                    raise ServiceError("invalid_screenshot") from None
            identifier = "BR-"+uuid.uuid4().hex
            store.submit_report(session["project_id"], identifier, {"description": description, "screenshot": image, "status": "new", "tags": "", "author": session["user_id"], "thread_id": session.get("thread_id")})
            return {"id": identifier}
        return JSONResponse(await run_in_threadpool(save), status_code=201)

    async def reset(request):
        session = await run_in_threadpool(who, request)
        await run_in_threadpool(store.set_session, session, thread_id="")
        return JSONResponse({"ok": True})

    async def label(request):
        session = await run_in_threadpool(who, request)
        data = await body(request)
        request_id = text(data.get("request_id"), 128)
        if data.get("label") not in {"pass", "fail"}:
            raise ServiceError("invalid_label")
        usage = await run_in_threadpool(store.usage, session["project_id"])
        if not any(r["telemetry"].get("request_id") == request_id for r in usage["requests"]):
            raise ServiceError("not_found", 404)
        await run_in_threadpool(store.put, session["project_id"], "feedback", request_id, {"label": data["label"], "author": session["user_id"]}, 500)
        return JSONResponse({"ok": True})

    async def cancel(request):
        session = await run_in_threadpool(who, request)
        data = await body(request)
        identifier = text(data.get("client_request_id"), 128)
        # Stored marker lets cancellation reach a request running in another process.
        await run_in_threadpool(store.put, session["project_id"], "cancel", identifier, {"expires": time.time()+300}, 1000)
        event = active.get((session["project_id"], identifier))
        if event:
            event.set()
        return JSONResponse({"ok": True})

    async def turn(request):
        if os.environ.get("ROUTER_GENERATION_ENABLED", "1") != "1":
            raise ServiceError("generation_paused", 503)
        api = request.url.path.startswith("/v1/")
        session = await run_in_threadpool(who, request, api)
        data = await body(request)
        project = session["project_id"]
        identifier = request.headers.get("idempotency-key") if api else data.get("client_request_id")
        identifier = text(identifier or uuid.uuid4().hex, 128)
        settings = await run_in_threadpool(store.get, project, "settings", "routing") or {}
        thread_id = None if api else (session.get("thread_id") or uuid.uuid4().hex)
        thread = await run_in_threadpool(store.get, project, "thread", thread_id or "") or {"history": [], "turns": []}
        image_data, image_type = None, None
        if api:
            raw_stream = data.get("stream", False)
            if not isinstance(raw_stream, bool):
                raise ServiceError("invalid_stream")
            normalized = {**data, "stream": False}
            history, options, gateway_options = normalize_extended(normalized)
        else:
            raw_stream = True
            message = text(data.get("message"), 100000)
            history = [*thread["history"], {"role": "user", "text": message}]
            if len(history) > 100 or sum(len(m["text"]) for m in history) > 100000:
                raise ServiceError("context_limit_start_new_thread", 413)
            options = {"require_verification": data.get("verify") is True}
            gateway_options = {}
            if data.get("image_base64"):
                image_data, image_type = data["image_base64"], data.get("image_type")
            elif data.get("reuse_image") and thread.get("attachment"):
                attachment = await run_in_threadpool(store.get, project, "attachment", thread["attachment"])
                if attachment:
                    image_data, image_type = attachment["data"], attachment["type"]
        cost = min(options.get("limits", registry.DEFAULT_LIMITS).max_cost_usd, settings.get("max_cost_usd", 1))
        latency = min(options.get("limits", registry.DEFAULT_LIMITS).max_latency_ms, settings.get("max_latency_ms", 180000))
        options["limits"] = replace(registry.DEFAULT_LIMITS, max_cost_usd=cost, max_latency_ms=latency)
        existing = await run_in_threadpool(store.admit, project, identifier, digest(json.dumps(data, sort_keys=True)), cost, thread_id)
        if existing:
            if not existing.get("result"):
                raise ServiceError("previous_request_failed", 409)
            result = existing["result"]
            if api and not raw_stream:
                return JSONResponse(result)
            async def replay():
                if api:
                    message = result["choices"][0]["message"]
                    delta = {"content": message["content"]}
                    if message.get("tool_calls"):
                        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
                    chunk = {"id": result["id"], "object": "chat.completion.chunk", "created": result["created"], "model": result["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": result["choices"][0]["finish_reason"]}], "usage": result["usage"]}
                    yield ("data: "+json.dumps(chunk)+"\n\ndata: [DONE]\n\n").encode()
                else:
                    yield format_sse("done", result)
            return StreamingResponse(replay(), media_type="text/event-stream")
        if not api:
            await run_in_threadpool(store.set_session, session, thread_id=thread_id)
        stop = threading.Event()
        active[(project, identifier)] = stop
        queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()
        telemetry = {}
        cancelled_at = [0.0, False]
        def cancelled():
            if stop.is_set():
                return True
            if time.monotonic()-cancelled_at[0] > .5:
                cancelled_at[:] = [time.monotonic(), bool(store.get(project, "cancel", identifier))]
            return cancelled_at[1]
        def emit(kind, value):
            if stop.is_set():
                raise StreamClosed
            future = asyncio.run_coroutine_threadsafe(queue.put((kind, value)), loop)
            try:
                future.result(timeout=10)
            except Exception:
                future.cancel(); stop.set(); raise StreamClosed from None
        def worker():
            final = None
            persisted = False
            published = False
            def delta(piece):
                nonlocal published
                published = True
                emit("delta", {"text": piece})
            def reset_output():
                if api and published:
                    raise StreamClosed
                emit("reset", {})
            try:
                with tempfile.TemporaryDirectory() as folder, workspace.scope(project, settings.get("profile", "balanced"), settings.get("allowed_providers")):
                    gateway_token = REQUEST_OPTIONS.set(gateway_options)
                    if gateway_options.get("requires_tools"):
                        registry.MODEL_SCOPE.set({k:v for k,v in registry.MODEL_SCOPE.get().items() if v.supports_tools})
                    image_path = ""
                    if image_data:
                        if image_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                            raise ServiceError("invalid_image")
                        raw = base64.b64decode(image_data, validate=True)
                        if len(raw) > 8*1024*1024:
                            raise ServiceError("image_too_large")
                        with Image.open(io.BytesIO(raw)) as source:
                            if source.width*source.height > 16_000_000:
                                raise ServiceError("image_too_large")
                            source.load()
                            image_path = str(Path(folder)/"attachment.jpg")
                            source.thumbnail((2048, 2048))
                            source.convert("RGB").save(image_path, quality=90)
                    def start(model_id, decision):
                        fields = _model_fields(model_id)
                        emit("model", {"model_id": model_id, "model_name": fields["name"], "model_index": fields["index"], "task": decision["task_type"], "confidence": None})
                    result = run_turn(history, image_path, thread.get("last_model"), conversation_messages, require_configured=True, owner_id=project,
                                      on_start=start, on_delta=delta if raw_stream and not gateway_options.get("requires_tools") else None,
                                      on_reset=reset_output, on_phase=lambda phase: emit("phase", {"phase": phase}),
                                      cancelled=cancelled, on_final=telemetry.update, **options)
                    if api:
                        final = {"id": "chatcmpl-"+result["request_id"], "object": "chat.completion", "created": int(time.time()), "model": result["model_id"],
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": result["finish_reason"]}],
                                 "usage": {"prompt_tokens": result["input_tokens"], "completion_tokens": result["output_tokens"], "total_tokens": sum([result["input_tokens"], result["output_tokens"]]) if result["input_tokens"] is not None and result["output_tokens"] is not None else None},
                                 "router": {"reserved_usd": result["reserved_usd"], "elapsed_ms": result["elapsed_ms"], "request_id": result["request_id"]}}
                        if result.get("tool_calls"):
                            final["choices"][0]["message"]["tool_calls"] = result["tool_calls"]
                    else:
                        final = _decision_view(result, bool(image_path))
                        thread.update(history=[*history, {"role": "assistant", "text": result["text"]}], turns=[*thread["turns"], {"role": "user", "text": history[-1]["text"], "image_attached": bool(image_path)}, {"role": "assistant", **final}], last_model=result["model_id"], verify=options.get("require_verification", False), title=thread.get("title") or history[-1]["text"][:70])
                        if image_path:
                            attachment_id = uuid.uuid4().hex
                            store.put(project, "attachment", attachment_id, {"data": base64.b64encode(Path(image_path).read_bytes()).decode(), "type": "image/jpeg"})
                            thread["attachment"] = attachment_id
                        store.put(project, "thread", thread_id, thread, preserve=("title", "archived"))
                store.finish(project, identifier, final, telemetry)
                persisted = True
                try:
                    workspace.record_health(project, telemetry)
                except Exception:
                    pass
                emit("done", final)
            except BaseException as exc:
                code = "cancelled" if isinstance(exc, StreamClosed) else "no_eligible_model" if isinstance(exc, NoEligibleModel) else exc.code if isinstance(exc, ServiceError) else str(exc) if str(exc) in SAFE_ERRORS else "model_call_failed"
                telemetry["status"] = "cancelled" if isinstance(exc, StreamClosed) else "failed"
                telemetry["error"] = code
                if not persisted:
                    store.finish(project, identifier, None, telemetry)
                try:
                    emit("error", {"error": code})
                except StreamClosed:
                    pass
            finally:
                if "gateway_token" in locals():
                    REQUEST_OPTIONS.reset(gateway_token)
                active.pop((project, identifier), None)
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(("end", None)), loop).result(timeout=2)
                except Exception:
                    pass
        task = asyncio.create_task(run_in_threadpool(worker))
        workers.add(task)
        task.add_done_callback(workers.discard)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        async def events():
            current_model, sent = "auto", False
            try:
                while True:
                    try:
                        kind, value = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"; continue
                    if kind == "end":
                        break
                    if not api:
                        yield format_sse(kind, value)
                        continue
                    if kind == "model":
                        current_model = value["model_id"]
                    elif kind == "reset":
                        # SDK consumers cannot retract previously emitted content.
                        if sent:
                            yield b'data: {"error":{"code":"stream_interrupted"}}\n\n'; stop.set(); break
                    elif kind in {"delta", "done"}:
                        content = value["text"] if kind == "delta" else (value["choices"][0]["message"]["content"] if not sent else "")
                        if content:
                            sent = True
                            yield ("data: "+json.dumps({"id": "chatcmpl-"+identifier, "object": "chat.completion.chunk", "created": int(time.time()), "model": current_model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})+"\n\n").encode()
                        if kind == "done":
                            calls = value["choices"][0]["message"].get("tool_calls")
                            if calls:
                                yield ("data: "+json.dumps({"id":"chatcmpl-"+identifier,"object":"chat.completion.chunk","created":int(time.time()),"model":value["model"],"choices":[{"index":0,"delta":{"tool_calls":[{"index":i,**call} for i,call in enumerate(calls)]},"finish_reason":None}]})+"\n\n").encode()
                            yield ("data: "+json.dumps({"id": "chatcmpl-"+identifier, "object": "chat.completion.chunk", "created": int(time.time()), "model": value["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": value["choices"][0]["finish_reason"]}], "usage": value["usage"]})+"\n\ndata: [DONE]\n\n").encode()
                    elif kind == "error":
                        yield ("data: "+json.dumps({"error": {"code": value["error"]}})+"\n\n").encode()
            finally:
                stop.set()
        if raw_stream:
            return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
        try:
            while True:
                kind, value = await queue.get()
                if kind == "done":
                    return JSONResponse(value)
                if kind == "error":
                    return JSONResponse({"error": {"code": value["error"], "message": value["error"], "type": "router_error"}}, status_code=502)
                if kind == "end":
                    raise ServiceError("model_call_failed", 502)
        finally:
            stop.set()

    async def models(request):
        session = await run_in_threadpool(who, request, True)
        def listing():
            with workspace.scope(session["project_id"]):
                from .api import model_list
                return model_list()
        return JSONResponse(await run_in_threadpool(listing))

    async def compare(request):
        if os.environ.get("ROUTER_GENERATION_ENABLED", "1") != "1":
            raise ServiceError("generation_paused", 503)
        session = await run_in_threadpool(who, request)
        data = await body(request)
        prompt = text(data.get("prompt"), 20000)
        selected = data.get("models")
        if not isinstance(selected, list) or not 2 <= len(selected) <= 3 or not all(isinstance(m, str) for m in selected) or len(set(selected)) != len(selected):
            raise ServiceError("choose_two_or_three_models")
        project = session["project_id"]
        settings = await run_in_threadpool(store.get, project, "settings", "routing") or {}
        ceiling = number(data.get("max_cost_usd"), .000001, settings.get("max_cost_usd", 1))
        batch = text(data.get("request_id"), 64)
        def execute_comparison():
            results = []
            with workspace.scope(project, settings.get("profile", "balanced"), settings.get("allowed_providers")):
                if any(m not in registry.MODEL_SCOPE.get() for m in selected):
                    raise ServiceError("model_not_enabled")
                for model in selected:
                    request_id = "compare-"+digest(batch+model)
                    existing = store.admit(project, request_id, digest(prompt+str(ceiling)+model), ceiling/len(selected))
                    if existing:
                        results.append(existing.get("result") or {"model_id": model, "error": "previous_request_failed"})
                        continue
                    telemetry = {}
                    try:
                        result = run_turn([{"role": "user", "text": prompt}], "", None, conversation_messages, requested_model=model, require_configured=True,
                                          limits=replace(registry.DEFAULT_LIMITS, max_cost_usd=ceiling/len(selected), max_latency_ms=settings.get("max_latency_ms", 180000)), on_final=telemetry.update)
                        public = {key: result[key] for key in ["model_id", "text", "elapsed_ms", "reserved_usd", "input_tokens", "output_tokens"]}
                        store.finish(project, request_id, public, telemetry)
                        results.append(public)
                    except Exception as exc:
                        public = {"model_id": model, "error": str(exc) if str(exc) in SAFE_ERRORS else "model_call_failed"}
                        store.finish(project, request_id, public, telemetry)
                        results.append(public)
            return {"results": results}
        return JSONResponse(await run_in_threadpool(execute_comparison))

    async def health(request):
        if request.url.path == "/readyz":
            try:
                await run_in_threadpool(store.memberships, "healthcheck")
            except Exception:
                return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def metrics(request):
        expected = os.environ.get("ROUTER_METRICS_TOKEN", "")
        if not expected or not secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer "+expected).encode()):
            raise ServiceError("forbidden", 403)
        def collect():
            from sqlalchemy import select, func
            from .store import runs
            with store.engine.connect() as db:
                counts = list(db.execute(select(runs.c.status, func.count()).group_by(runs.c.status)))
                active_count = db.execute(select(func.count()).select_from(runs).where(runs.c.status=="running", runs.c.lease>time.time())).scalar()
            return "# TYPE router_requests gauge\n"+"".join(f'router_requests{{status="{status}"}} {count}\n' for status,count in counts)+f"# TYPE router_active_requests gauge\nrouter_active_requests {active_count}\n"
        return Response(await run_in_threadpool(collect), media_type="text/plain")

    async def error(request, exc):
        code = exc.code if isinstance(exc, ServiceError) else "invalid_request"
        payload = {"code": code, "message": code.replace("_", " "), "type": "router_error"} if request.url.path.startswith("/v1/") else code
        return JSONResponse({"error": payload}, status_code=exc.status if isinstance(exc, ServiceError) else 400)

    @asynccontextmanager
    async def lifespan(app):
        async def maintenance():
            while True:
                try:
                    await run_in_threadpool(store.cleanup)
                except Exception:
                    logging.getLogger("router.maintenance").error("Retention cleanup failed")
                await asyncio.sleep(300)
        maintenance_task = asyncio.create_task(maintenance())
        yield
        maintenance_task.cancel()
        for event in list(active.values()):
            event.set()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        if workers:
            await asyncio.wait(workers, timeout=30)
        store.engine.dispose()

    routes = [Route("/", home), Route("/login", login), Route("/auth/callback", callback), Route("/state", state), Route("/workspace", workspace_get), Route("/workspace", manage, methods=["POST"]),
              Route("/export/{kind}/{identifier}", export), Route("/reports", report, methods=["POST"]), Route("/reset", reset, methods=["POST"]), Route("/label", label, methods=["POST"]), Route("/cancel", cancel, methods=["POST"]),
              Route("/turn", turn, methods=["POST"]), Route("/compare", compare, methods=["POST"]), Route("/v1/chat/completions", turn, methods=["POST"]), Route("/v1/models", models), Route("/healthz", health), Route("/readyz", health), Route("/metrics", metrics)]
    for filename in ["styles.css", "app.js", "scene.js", "report.js", "workspace.js", "favicon.svg"]:
        async def asset(request, name=filename):
            return FileResponse(WEB_ROOT/name)
        routes.append(Route("/"+filename, asset))
    routes.extend([Mount("/vendor", StaticFiles(directory=WEB_ROOT/"vendor")), Mount("/fonts", StaticFiles(directory=WEB_ROOT/"fonts"))])
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={ServiceError: error, APIRequestError: error, ValueError: error})
    app.state.store, app.state.workspace = store, workspace
    app.add_middleware(SessionMiddleware, secret_key=key.decode(), session_cookie="router_oidc", https_only=saas, max_age=600, same_site="lax")
    hosts = [urlsplit(public_url).hostname] if saas else ["127.0.0.1", "localhost", "testserver"]
    if saas and os.environ.get("RAILWAY_PROJECT_ID"):
        hosts.append(RAILWAY_PROBE_HOST)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(ProbeHostBoundary)
    from .abuse import AbuseProtection
    app.add_middleware(AbuseProtection, store=store, key=key, enabled=saas)
    app.add_middleware(SecurityHeaders, saas=saas)
    return app
