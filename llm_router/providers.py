"""Server-owned connections. Credentials are resolved at call time, never serialized."""
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

PROVIDERS_PATH = Path(__file__).resolve().parent.parent / "providers.json"
CONNECTION_SCOPE = ContextVar("connections", default=None)


class ProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    name: str
    adapter: str
    base_url: str
    api_key_env: str | None
    token_parameter: str = "max_tokens"
    enabled: bool = True
    include_usage: bool = True
    secret: str | None = field(default=None, repr=False, compare=False)

    def key(self) -> str:
        if self.secret is not None:
            return self.secret
        return os.environ.get(self.api_key_env, "").strip() if self.api_key_env else ""

    @property
    def configured(self) -> bool:
        return self.enabled and (self.api_key_env is None or bool(self.key()))


BUILTINS = {
    "groq": Connection("groq", "openai_compatible", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": Connection("openrouter", "openai_compatible", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": Connection("openai", "openai_compatible", "https://api.openai.com/v1", "OPENAI_API_KEY", "max_completion_tokens"),
    "anthropic": Connection("anthropic", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
}


def parse_connection(name: str, row: dict) -> Connection:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not isinstance(row, dict):
        raise ProviderConfigError("Invalid provider connection.")
    adapter = row.get("adapter")
    if adapter not in {"openai_compatible", "anthropic"}:
        raise ProviderConfigError(f"{name}: unsupported adapter.")
    base = row.get("base_url")
    if not isinstance(base, str):
        raise ProviderConfigError(f"{name}: base_url is required.")
    parsed = urlsplit(base)
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigError(f"{name}: use a base URL without credentials, query, or fragment.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ProviderConfigError(f"{name}: HTTPS is required except on loopback.")
    env = row.get("api_key_env")
    if env is not None and (not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env)):
        raise ProviderConfigError(f"{name}: api_key_env must name an environment variable.")
    if env is None and not loopback:
        raise ProviderConfigError(f"{name}: remote endpoints require api_key_env.")
    token = row.get("token_parameter", "max_tokens")
    if token not in {"max_tokens", "max_completion_tokens"}:
        raise ProviderConfigError(f"{name}: invalid token parameter.")
    for key in ("enabled", "include_usage"):
        if key in row and not isinstance(row[key], bool):
            raise ProviderConfigError(f"{name}: {key} must be a boolean.")
    return Connection(name, adapter, base.rstrip("/"), env, token, row.get("enabled", True), row.get("include_usage", True))


def load_connections(path: Path = PROVIDERS_PATH) -> dict[str, Connection]:
    if CONNECTION_SCOPE.get() is not None:
        return CONNECTION_SCOPE.get()
    connections = dict(BUILTINS)
    if not path.exists():
        return connections
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        rows = loaded.get("providers") if isinstance(loaded, dict) else None
        if not isinstance(rows, dict):
            raise ProviderConfigError("providers.json needs a providers object.")
        for name, row in rows.items():
            connections[name] = parse_connection(name, row)
    except (OSError, ValueError) as exc:
        raise ProviderConfigError("Invalid providers.json configuration.") from exc
    return connections


def provider_status() -> list[dict]:
    return [{"name": c.name, "adapter": c.adapter, "configured": c.configured, "enabled": c.enabled}
            for c in load_connections().values()]
