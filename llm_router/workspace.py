"""Connection onboarding, encrypted secrets, and isolated execution snapshots."""
import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from . import registry
from .gateway import open_request
from .policy import ROUTING_PROFILE
from .providers import BUILTINS, CONNECTION_SCOPE, ProviderConfigError, parse_connection
from .store import ServiceError


def encryption_key(saas=False):
    key = os.environ.get("ROUTER_SECRET_KEY")
    if key:
        Fernet(key.encode())
        return key.encode()
    if saas:
        raise RuntimeError("ROUTER_SECRET_KEY is required in SaaS mode")
    path = Path("data/secret.key")
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(Fernet.generate_key())
    return path.read_bytes().strip()


class Workspace:
    def __init__(self, store, key, saas=False):
        self.store, self.cipher, self.saas = store, Fernet(key), saas

    def connections(self, project):
        connections = {} if self.saas else dict(BUILTINS)
        for row in self.store.list(project, "connection", 50):
            config = row["config"]
            connection = parse_connection(row["id"], config)
            secret = self.cipher.decrypt(row["secret"].encode()).decode() if row.get("secret") else ""
            connections[row["id"]] = replace(connection, secret=secret)
        return connections

    def status(self, project):
        revisions = {row["id"]: row.get("revision", 0) for row in self.store.list(project, "connection", 50)}
        return [{"name": c.name, "adapter": c.adapter, "base_url": c.base_url, "configured": c.configured, "enabled": c.enabled, "revision": revisions.get(c.name, 0),
                 "health": self.store.get(project, "health", c.name) or {"status": "untested"}}
                for c in self.connections(project).values()]

    def _parse(self, name, config):
        """Reject an unusable endpoint with the provider's own reason.

        parse_connection raises a ValueError, which the API otherwise reports as
        a bare "invalid request". The wording it produces is the only thing that
        tells the operator which part of the form to change.
        """
        try:
            return parse_connection(name, config)
        except ProviderConfigError as error:
            raise ServiceError("invalid_connection", 400, str(error)) from None

    def save_connection(self, project, body):
        name = body.get("name", "")
        template = body.get("template", "custom")
        if template in BUILTINS:
            config = asdict(BUILTINS[template]); config.pop("secret")
            config["enabled"] = body.get("enabled", True)
        else:
            config = {"adapter": body.get("adapter", "openai_compatible"), "base_url": body.get("base_url"),
                      "api_key_env": "ROUTER_MANAGED_KEY", "enabled": body.get("enabled", True)}
            if not self.saas and urlsplit(config["base_url"] or "").hostname in {"localhost", "127.0.0.1", "::1"}:
                config["api_key_env"] = None
        connection = self._parse(name, config)
        parsed = urlsplit(connection.base_url)
        if self.saas:
            approved = {urlsplit(c.base_url).hostname for c in BUILTINS.values()}
            approved.update(filter(None, os.environ.get("ROUTER_PROVIDER_HOSTS", "").split(",")))
            if parsed.scheme != "https" or parsed.hostname not in approved or parsed.port not in {None, 443}:
                raise ServiceError("provider_host_not_approved", 403)
        key = body.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192:
            raise ServiceError("invalid_api_key")
        old = self.store.get(project, "connection", name)
        encrypted = self.cipher.encrypt(key.strip().encode()).decode() if key.strip() else (old or {}).get("secret", "")
        # Never reuse a saved credential when changing its destination.
        if old and old["config"]["base_url"] != connection.base_url and not key.strip():
            raise ServiceError("new_destination_requires_key")
        if config["api_key_env"] and not encrypted:
            raise ServiceError("api_key_required")
        self.store.put(project, "connection", name, {"config": config, "secret": encrypted}, 30, expected_revision=body.get("revision", 0))
        self.store.remove(project, "health", name)
        return {"ok": True}

    def discover(self, project, name):
        connection = self.connections(project).get(name)
        if not connection or not connection.configured:
            raise ServiceError("provider_not_configured")
        headers = {"Accept": "application/json"}
        if connection.adapter == "anthropic":
            headers.update({"x-api-key": connection.key(), "anthropic-version": "2023-06-01"})
        elif connection.key():
            headers["Authorization"] = "Bearer " + connection.key()
        try:
            with open_request(urllib.request.Request(connection.base_url+"/models", headers=headers), 8) as response:
                payload = response.read(1_000_001)
                if len(payload) > 1_000_000:
                    raise ValueError
                rows = json.loads(payload).get("data", [])
                result = [{"id": r["id"], "name": r.get("display_name", r["id"])} for r in rows[:300] if isinstance(r, dict) and isinstance(r.get("id"), str)]
        except urllib.error.HTTPError as error:
            # A rejected credential is the common case and deserves a real answer.
            reason = {401: "The provider rejected this API key.", 403: "This key is not permitted to list models.",
                      404: "That endpoint does not offer a /models list.",
                      429: "The provider is rate limiting this key."}.get(error.code, "The provider is unavailable." if 500 <= error.code < 600 else "The provider returned HTTP "+str(error.code)+".")
            logging.getLogger("router.providers").warning("Discovery rejected for %s: HTTP %s %s", name, error.code, reason)
            self.store.put(project, "health", name, {"status": "unavailable", "checked": time.time(), "reason": reason})
            raise ServiceError("provider_discovery_failed", 502, reason) from None
        except Exception as error:
            unreachable = "The endpoint could not be reached. Check the URL and that the provider is reachable from the server."
            # The type separates a name or TLS problem from a timeout or a
            # refused connection without ever logging the credential.
            logging.getLogger("router.providers").warning("Discovery failed for %s: %s: %s", name, type(error).__name__, error)
            self.store.put(project, "health", name, {"status": "unavailable", "checked": time.time(), "reason": unreachable})
            raise ServiceError("provider_discovery_failed", 502, unreachable) from None
        self.store.put(project, "health", name, {"status": "reachable", "checked": time.time()})
        return {"models": result, "note": "Discovery checks credentials without generating an answer. Review pricing and capabilities before enabling."}

    def model_rows(self, project):
        saved = self.store.list(project, "model", 200)
        if not self.saas:
            custom_ids = {r["id"] for r in saved}
            for model in registry.CATALOG:
                if model.model_id not in custom_ids:
                    row = asdict(model)
                    row["modalities"], row["regions"] = list(model.modalities), list(model.regions)
                    saved.append({"id": model.model_id, **row, "enabled": True})
        return saved

    def save_model(self, project, body):
        if not isinstance(body, dict):
            raise ServiceError("invalid_model")
        if not isinstance(body.get("model_id"), str) or not 1 <= len(body["model_id"]) <= 128:
            raise ServiceError("invalid_model_id")
        # A bare "provider/" prefix means the operator never supplied an
        # upstream model ID. It would route but never match a real call.
        if body["model_id"].strip() != body["model_id"] or body["model_id"].endswith("/"):
            raise ServiceError("invalid_model_id")
        if not isinstance(body.get("upstream_model_id"), str) or not body["upstream_model_id"].strip():
            raise ServiceError("invalid_model_id")
        token = CONNECTION_SCOPE.set(self.connections(project))
        try:
            model = registry._record(body)
        finally:
            CONNECTION_SCOPE.reset(token)
        row = asdict(model)
        row["modalities"], row["regions"] = list(model.modalities), list(model.regions)
        if not isinstance(body.get("enabled", True), bool):
            raise ServiceError("invalid_enabled")
        row["enabled"] = body.get("enabled", True)
        self.store.put(project, "model", model.model_id, row, 200, expected_revision=body.get("revision", 0))
        return {"ok": True}

    @contextmanager
    def scope(self, project, profile="balanced", allowed=None):
        connections = self.connections(project)
        token = CONNECTION_SCOPE.set(connections)
        model_token = profile_token = None
        try:
            models = {}
            for row in self.model_rows(project):
                if not row.get("enabled", True):
                    continue
                model = registry._record(row)
                health = self.store.get(project, "health", model.provider) or {}
                if health.get("retry_after", 0) > time.time():
                    continue
                if allowed and model.provider not in allowed:
                    continue
                if profile == "private" and urlsplit(connections[model.provider].base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                    continue
                models[model.model_id] = model
            model_token = registry.MODEL_SCOPE.set(models)
            profile_token = ROUTING_PROFILE.set(profile)
            yield
        finally:
            if profile_token is not None:
                ROUTING_PROFILE.reset(profile_token)
            if model_token is not None:
                registry.MODEL_SCOPE.reset(model_token)
            CONNECTION_SCOPE.reset(token)

    def record_health(self, project, telemetry):
        mapping = {m["model_id"]: m["provider"] for m in self.model_rows(project)}
        for attempt in telemetry.get("attempts", []):
            provider = mapping.get(attempt.get("model_id"))
            if not provider:
                continue
            error = attempt.get("error")
            self.store.put(project, "health", provider, {"status": "healthy" if not error else "degraded", "checked": time.time(),
                           "error": error, "retry_after": time.time()+30 if error in {"rate_limited", "model_call_failed"} else 0})
