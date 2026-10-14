import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from io import BytesIO
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image, UnidentifiedImageError
from llm_router.api import APIRequestError, chat_completion, model_list
from llm_router.providers import provider_status

from llm_router.execute import run_turn
from llm_router.gateway import StreamClosed
from llm_router.messages import conversation_messages
from llm_router.pipeline import NoEligibleModel
from llm_router.quality import refresh_quality_table
from llm_router import registry
from llm_router.render import render_markdown
from llm_router.trace import TRACE_PATH, label_request

HOST = "127.0.0.1"
PORT = 8765
MESSAGE_CAP = 100_000
IMAGE_CAP = 8 * 1024 * 1024
BODY_CAP = 4 * ((IMAGE_CAP + 2) // 3) + 500_000
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
UPLOAD_ROOT = Path("data/uploads")
PUBLIC_ERRORS = {
    "rate_limited",
    "latency_budget_exceeded",
    "all_models_failed",
    "no_eligible_model",
    "model_call_failed",
    "provider_not_configured", "provider_auth_failed", "cost_budget_exceeded",
    "output_validation_failed", "verification_rejected", "verification_unavailable",
}
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)

_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_generation_slot = threading.BoundedSemaphore(1)
_unlock_attempts: dict[str, list[float]] = {}


def _allow_unlock(address: str) -> bool:
    now = time.monotonic()
    with _lock:
        for key in list(_unlock_attempts):
            _unlock_attempts[key] = [stamp for stamp in _unlock_attempts[key] if now - stamp < 300]
            if not _unlock_attempts[key]:
                del _unlock_attempts[key]
        attempts = _unlock_attempts.get(address, [])
        if len(attempts) >= 10 or (address not in _unlock_attempts and len(_unlock_attempts) >= 1024):
            return False
        _unlock_attempts[address] = attempts + [now]
        return True


def format_sse(event: str, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    data = "".join(f"data: {line}\n" for line in body.split("\n"))
    return f"event: {event}\n{data}\n".encode("utf-8")


def sniff_image(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    for magic, mime, ext in SIGNATURES:
        if data.startswith(magic):
            return mime, ext
    return None


def _host_name(host: str | None) -> str:
    if not host:
        return ""
    value = host.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 1 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def host_allowed(host: str | None) -> bool:
    name = _host_name(host)
    if not name:
        return False
    if name in {"localhost", socket.gethostname().lower()}:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def share_urls() -> list[str]:
    urls = [f"http://127.0.0.1:{PORT}"]
    if os.environ.get("ROUTER_HOST", HOST) in {"127.0.0.1", "localhost", "::1"}:
        return urls
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            urls.append(f"http://{address}:{PORT}")
    except OSError:
        pass
    return urls


def _session(token: str) -> dict:
    with _lock:
        now = time.monotonic()
        for expired in [key for key, value in _sessions.items() if now - value["last_seen"] > 3600 and not value["lock"].locked()]:
            del _sessions[expired]
        if token not in _sessions:
            if len(_sessions) >= 128:
                idle = [key for key, value in _sessions.items() if not value["lock"].locked()]
                if idle:
                    del _sessions[min(idle, key=lambda key: _sessions[key]["last_seen"])]
            _sessions[token] = {
                "history": [],
                "turns": [],
                "last_image": "",
                "last_model": None,
                "verify": False,
                "csrf": "",
                "unlocked": False,
                "lock": threading.Lock(), "owner_id": secrets.token_urlsafe(24),
                "cancel": threading.Event(), "active_request": None, "last_seen": now,
            }
        _sessions[token]["last_seen"] = now
        return _sessions[token]


def _cookies(header: str | None) -> dict[str, str]:
    jar = SimpleCookie()
    if header:
        jar.load(header)
    return {key: morsel.value for key, morsel in jar.items()}


def _known_session(token):
    with _lock:
        state = _sessions.get(token)
        if state and time.monotonic() - state["last_seen"] > 3600 and not state["lock"].locked():
            del _sessions[token]
            return None
        return state


def password_required() -> bool:
    return bool(os.environ.get("ROUTER_PASSWORD", "").strip())


def password_matches(supplied: str) -> bool:
    expected = os.environ.get("ROUTER_PASSWORD", "").strip()
    if not expected or not isinstance(supplied, str):
        return False
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _model_fields(model_id: str) -> dict:
    model = registry.BY_ID.get(model_id)
    try:
        index = registry.get_catalog().index(model) if model else -1
    except ValueError:
        index = -1
    return {
        "model_id": model_id,
        "name": (model.display_name if model else "") or model_id,
        "index": index,
    }


def _rejected_view(rejected: list[dict]) -> list[dict]:
    return [
        {**_model_fields(row["model_id"]), "reason": row.get("reason"), "detail": row.get("detail")}
        for row in rejected
    ]


def _decision_view(result: dict, image_attached: bool) -> dict:
    decision = result["decision"]
    task = decision.get("task_type")
    jev = decision.get("jev") or {}
    confidence = jev.get("route_confidence")
    if result["model_id"] != decision.get("selected", result["model_id"]):
        confidence = None
    selected = _model_fields(result["model_id"])
    candidates = []
    for row in decision.get("ranked") or []:
        model = registry.BY_ID.get(row["model_id"])
        if model is None:
            continue
        candidates.append({
            **_model_fields(model.model_id),
            "score": row["score"],
            "quality": row.get("quality", model.quality.get(task, 0.0) if model else 0.0),
            "quality_source": row.get("quality_source") or "estimated",
            "latency_ms": row.get("latency_ms", model.expected_latency_ms if model else 0),
            "selected": model.model_id == result["model_id"],
        })
    attempts = [
        {
            **_model_fields(row["model_id"]),
            "trigger": row.get("trigger"),
            "error": None if row.get("error") is None else (row["error"] if row["error"] in PUBLIC_ERRORS else "call_failed"),
        }
        for row in result.get("attempts") or []
    ]
    return {
        "html": render_markdown(result["text"]),
        "request_id": result.get("request_id"),
        "reserved_usd": result.get("reserved_usd"),
        "truncated": result.get("finish_reason") == "length",
        "model_id": selected["model_id"],
        "model_name": selected["name"],
        "model_index": selected["index"],
        "task": task,
        "confidence": confidence,
        "analysis_source": decision.get("analysis_source"),
        "used_fallback": result["model_id"] != decision.get("selected", result["model_id"]),
        "routed_model_name": _model_fields(decision.get("selected", result["model_id"]))["name"],
        "candidates": candidates,
        "rejected": _rejected_view(decision.get("rejected") or []),
        "catalog_size": len(registry.get_catalog()),
        "attempts": attempts,
        "elapsed_ms": result.get("elapsed_ms"),
        "validation": result.get("validation"),
        "verification": result.get("verification"),
        "image_attached": image_attached,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Router"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: list[str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for header in extra or []:
            name, value = header.split(": ", 1)
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _open_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _write_event(self, event: str, payload: dict) -> None:
        try:
            self.wfile.write(format_sse(event, payload))
            self.wfile.flush()
        except OSError as exc:
            raise StreamClosed from exc

    def _json(self, status: int, payload: dict, extra: list[str] | None = None):
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _reject_host(self) -> bool:
        if host_allowed(self.headers.get("Host")):
            return False
        self._json(400, {"error": "invalid_host"})
        return True

    def _identity(self) -> tuple[str, str, dict] | None:
        cookies = _cookies(self.headers.get("Cookie"))
        session_id = cookies.get("router_session")
        csrf = cookies.get("router_csrf")
        if not session_id or not csrf:
            self._json(403, {"error": "forbidden"})
            return None
        state = _known_session(session_id)
        if state is None or not state.get("csrf") or not secrets.compare_digest(csrf.encode("utf-8"), state["csrf"].encode("utf-8")) or self.headers.get("X-CSRF-Token") != csrf:
            self._json(403, {"error": "forbidden"})
            return None
        state["last_seen"] = time.monotonic()
        return session_id, csrf, state

    def do_GET(self):
        if self._reject_host():
            return
        path = urlparse(self.path).path
        if path == "/v1/models":
            if self._api_authorized():
                self._json(200, model_list())
            return
        if path == "/":
            cookies = _cookies(self.headers.get("Cookie"))
            existing = _known_session(cookies.get("router_session", ""))
            if existing and existing.get("csrf") == cookies.get("router_csrf"):
                session_id = cookies["router_session"]
                csrf = existing["csrf"]
                extra = []
            else:
                session_id = secrets.token_urlsafe(24)
                csrf = secrets.token_urlsafe(24)
                created = _session(session_id)
                created["csrf"] = csrf
                extra = [
                    f"Set-Cookie: router_session={session_id}; Path=/; SameSite=Strict; HttpOnly",
                    f"Set-Cookie: router_csrf={csrf}; Path=/; SameSite=Strict; HttpOnly",
                ]
            page = (WEB_ROOT / "index.html").read_text(encoding="utf-8").replace("{{CSRF}}", csrf)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", extra)
            return
        if path == "/state":
            cookies = _cookies(self.headers.get("Cookie"))
            state = _known_session(cookies.get("router_session", ""))
            if state is None:
                self._json(403, {"error": "forbidden"})
                return
            if password_required() and not state.get("unlocked"):
                self._json(200, {"locked": True, "turns": [], "has_image": False})
                return
            self._json(200, {
                "turns": state["turns"],
                "has_image": bool(state["last_image"]),
                "verify": state["verify"],
                "locked": password_required() and not state.get("unlocked"),
                "catalog_error": registry.catalog_error,
                "providers": provider_status(),
                "models": [{"id": m.model_id, "name": m.display_name, "provider": m.provider, "modalities": sorted(m.modalities)} for m in registry.get_catalog()],
            })
            return
        files = {
            "/styles.css": "text/css; charset=utf-8",
            "/app.js": "text/javascript; charset=utf-8",
            "/report.js": "text/javascript; charset=utf-8",
            "/vendor/html2canvas.min.js": "text/javascript; charset=utf-8",
            "/scene.js": "text/javascript; charset=utf-8",
            "/vendor/three.module.min.js": "text/javascript; charset=utf-8",
            "/favicon.svg": "image/svg+xml",
            "/fonts/inter.woff2": "font/woff2",
            "/fonts/newsreader.woff2": "font/woff2",
            "/fonts/newsreader-italic.woff2": "font/woff2",
            "/fonts/jetbrains-mono.woff2": "font/woff2",
        }
        if path in files:
            file_path = (WEB_ROOT / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(WEB_ROOT.resolve())):
                self._json(404, {"error": "not_found"})
                return
            self._send(200, file_path.read_bytes(), files[path])
            return
        self._json(404, {"error": "not_found"})

    def _api_authorized(self) -> bool:
        expected = os.environ.get("ROUTER_API_TOKEN", "").strip()
        supplied = self.headers.get("Authorization", "")
        if not expected or not secrets.compare_digest(supplied.encode("utf-8"), ("Bearer " + expected).encode("utf-8")):
            self._json(401, {"error": {"message": "A configured router API token is required.", "type": "authentication_error", "code": "unauthorized"}})
            return False
        return True

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError
        except ValueError:
            self._json(400, {"error": "invalid_content_length"})
            return None
        cap = 3_100_000 if urlparse(self.path).path == "/reports" else BODY_CAP
        if length > cap:
            self._json(413, {"error": "payload_too_large"})
            return None
        self.connection.settimeout(15)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError, OSError):
            self._json(400, {"error": "invalid_json"})
            return None
        return body

    def do_POST(self):
        if self._reject_host():
            return
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            if not self._api_authorized():
                return
            body = self._read_body()
            if body is None:
                return
            if not _generation_slot.acquire(blocking=False):
                self._json(429, {"error": {"message": "Router is busy. Retry shortly.", "code": "router_busy", "type": "rate_limit_error"}})
                return
            try:
                self._json(200, chat_completion(body, trace_path=TRACE_PATH))
            except APIRequestError as exc:
                self._json(400, {"error": {"message": str(exc), "code": "invalid_request", "type": "invalid_request_error"}})
            except NoEligibleModel:
                self._json(400, {"error": {"message": "No configured model can take this request.", "code": "no_eligible_model", "type": "invalid_request_error"}})
            except Exception as exc:
                code = getattr(exc, "code", "model_call_failed")
                self._json(502, {"error": {"message": code if code in PUBLIC_ERRORS else "model_call_failed", "code": code if code in PUBLIC_ERRORS else "model_call_failed", "type": "router_error"}})
            finally:
                _generation_slot.release()
            return
        identity = self._identity()
        if identity is None:
            return
        _session_id, _csrf, state = identity
        body = self._read_body()
        if body is None:
            return
        if path == "/reports":
            from .reports import save_report, ReportError
            if password_required() and not state.get("unlocked"):
                self._json(403, {"error": "locked"})
                return
            try:
                report_id = save_report(body, state["owner_id"])
                self._json(201, {"id": report_id})
            except ReportError as exc:
                self._json(exc.status, {"error": str(exc)})
            except Exception:
                self._json(503, {"error": "report_unavailable"})
            return
        if path == "/cancel":
            if state["active_request"] == body.get("client_request_id") and state["active_request"]:
                state["cancel"].set()
            self._json(200, {"ok": True})
            return
        if not state["lock"].acquire(blocking=False):
            self._json(409, {"error": "router_busy"})
            return
        acquired = False
        try:
            if path == "/turn":
                acquired = _generation_slot.acquire(blocking=False)
                if not acquired:
                    self._json(429, {"error": "router_busy"})
                    return
            self._post_session(path, body, state)
        finally:
            if acquired:
                state["active_request"] = None
                _generation_slot.release()
            state["lock"].release()

    def _post_session(self, path: str, body: dict, state: dict):
        if path == "/unlock":
            if not password_required():
                state["unlocked"] = True
                self._json(200, {"ok": True})
                return
            supplied = body.get("password")
            if not _allow_unlock(self.client_address[0]):
                self._json(429, {"error": "unlock_rate_limited"}, ["Retry-After: 300"])
                return
            if not isinstance(supplied, str) or not password_matches(supplied):
                self._json(403, {"error": "locked"})
                return
            state["unlocked"] = True
            self._json(200, {"ok": True})
            return
        if path in {"/turn", "/reset", "/label"} and password_required() and not state.get("unlocked"):
            self._json(403, {"error": "locked"})
            return
        if path == "/reset":
            state["history"].clear()
            state["turns"].clear()
            state["last_image"] = ""
            state["last_model"] = None
            state["verify"] = False
            self._json(200, {"ok": True})
            return
        if path == "/label":
            label = body.get("label")
            request_id = body.get("request_id")
            if not isinstance(request_id, str) or label not in {"pass", "fail"} or not label_request(request_id, state["owner_id"], label, TRACE_PATH):
                self._json(400, {"error": "not_labeled"})
                return
            refresh_quality_table(TRACE_PATH)
            self._json(200, {"ok": True})
            return
        if path != "/turn":
            self._json(404, {"error": "not_found"})
            return
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json(400, {"error": "empty_message"})
            return
        if len(message) > MESSAGE_CAP:
            self._json(413, {"error": "message_too_long"})
            return
        verify = body.get("verify") is True
        state["verify"] = verify
        image_path = ""
        image_attached = False
        if body.get("reuse_image") is True and state["last_image"]:
            image_path = state["last_image"]
            image_attached = True
        elif body.get("image_base64"):
            saved = _save_image(body.get("image_base64"), body.get("image_type"))
            if isinstance(saved, str) and saved.startswith("error:"):
                self._json(400, {"error": saved.removeprefix("error:")})
                return
            image_path = saved
            image_attached = True
        client_request_id = body.get("client_request_id")
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 100:
            self._json(400, {"error": "invalid_request"})
            return
        state["active_request"] = client_request_id
        state["cancel"].clear()
        history = [*state["history"], {"role": "user", "text": message.strip()}]
        opened = False

        def emit(event: str, payload: dict) -> None:
            nonlocal opened
            if state["cancel"].is_set():
                raise StreamClosed
            if not opened:
                self._open_stream()
                opened = True
            self._write_event(event, payload)

        def on_start(model_id: str, decision: dict) -> None:
            fields = _model_fields(model_id)
            jev = decision.get("jev") or {}
            confidence = jev.get("route_confidence")
            if model_id != decision.get("selected"):
                confidence = None
            emit("model", {
                "model_id": fields["model_id"],
                "model_name": fields["name"],
                "model_index": fields["index"],
                "task": decision.get("task_type"),
                "confidence": confidence,
            })

        def fail(status: int, payload: dict) -> None:
            if not opened:
                self._json(status, payload)
                return
            try:
                emit("error", payload)
            except StreamClosed:
                return

        try:
            result = run_turn(
                history,
                image_path,
                state["last_model"],
                conversation_messages,
                require_verification=verify,
                trace_path=TRACE_PATH,
                on_start=on_start,
                on_delta=lambda piece: emit("delta", {"text": piece}),
                on_reset=lambda: emit("reset", {}),
                on_phase=lambda phase: emit("phase", {"phase": phase}),
                owner_id=state["owner_id"],
                require_configured=True,
                cancelled=state["cancel"].is_set,
            )
        except StreamClosed:
            return
        except NoEligibleModel as exc:
            fail(400, {"error": "no_eligible_model", "rejected": _rejected_view(exc.rejected)})
            return
        except Exception as exc:
            code = str(exc)
            fail(502, {"error": code if code in PUBLIC_ERRORS else "model_call_failed"})
            return
        view = _decision_view(result, image_attached)
        if state["cancel"].is_set():
            return
        state["history"] = history
        state["history"].append({"role": "assistant", "text": result["text"]})
        state["last_model"] = result["model_id"]
        if image_path:
            state["last_image"] = image_path
        state["turns"].append({"role": "user", "text": message.strip(), "image_attached": image_attached})
        state["turns"].append({"role": "assistant", **view})
        try:
            emit("done", view)
        except StreamClosed:
            return


def _save_image(encoded, declared) -> str:
    import base64
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return "error:invalid_image"
    if len(data) > IMAGE_CAP:
        return "error:image_too_large"
    sniffed = sniff_image(data)
    if sniffed is None or declared != sniffed[0]:
        return "error:invalid_image"
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                return "error:image_too_large"
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return "error:invalid_image"
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_ROOT / f"{secrets.token_hex(16)}{sniffed[1]}"
    path.write_bytes(data)
    return str(path)


def serve() -> None:
    global PORT
    host = os.environ.get("ROUTER_HOST", HOST)
    PORT = int(os.environ.get("ROUTER_PORT", str(PORT)))
    if host not in {"127.0.0.1", "localhost", "::1"} and not password_required():
        raise RuntimeError("Set ROUTER_PASSWORD before enabling LAN access.")
    server = ThreadingHTTPServer((host, PORT), Handler)
    for url in share_urls():
        print(f"Open {url}", flush=True)
    server.serve_forever()
