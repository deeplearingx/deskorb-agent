"""Capability declarations and conservative routing for configured models."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from model_adapter import PROVIDERS, ProviderProfile, normalize_provider, provider_profile


_TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PRIVACY_LEVELS = {"local", "configured", "third_party"}


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    protocol: str
    supports_tools: bool
    supports_vision: bool
    supports_streaming: bool
    supports_structured_output: bool = True
    context_tokens: int = 32_000
    privacy_level: str = "configured"

    def safe_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "protocol": self.protocol,
                "tools": self.supports_tools, "vision": self.supports_vision,
                "streaming": self.supports_streaming, "structured_output": self.supports_structured_output,
                "context_tokens": self.context_tokens, "privacy_level": self.privacy_level}


@dataclass(frozen=True)
class RoutingDecision:
    model: str
    capabilities: ModelCapabilities
    accepted: bool
    reason: str

    def safe_dict(self) -> dict[str, Any]:
        return {"model": self.model[:120], "accepted": self.accepted,
                "reason": self.reason[:240], "capabilities": self.capabilities.safe_dict()}


@dataclass(frozen=True)
class FallbackTarget:
    """An explicitly configured model destination used only after confirmation.

    API keys intentionally do not belong in this object.  They are resolved at
    call time from the provider-specific environment or credential store.
    """

    target_id: str
    provider: str
    model: str
    base_url: str
    privacy_level: str = "configured"
    supports_tools: bool = True
    supports_vision: bool = True
    supports_streaming: bool = True
    supports_structured_output: bool = True
    context_tokens: int = 32_000

    @property
    def profile(self) -> ProviderProfile:
        return provider_profile(self.provider, self.base_url)

    @property
    def capabilities(self) -> ModelCapabilities:
        profile = self.profile
        return ModelCapabilities(
            profile.name, profile.protocol, bool(self.supports_tools),
            bool(self.supports_vision), bool(self.supports_streaming),
            bool(self.supports_structured_output), max(4_000, int(self.context_tokens)),
            self.privacy_level,
        )

    def safe_dict(self, *, key_configured: bool | None = None) -> dict[str, Any]:
        value = {
            "id": self.target_id,
            "provider": self.profile.name,
            "model": self.model[:120],
            "base_url": self.profile.base_url[:240],
            "protocol": self.profile.protocol,
            "privacy_level": self.privacy_level,
            "capabilities": self.capabilities.safe_dict(),
            "requires_confirmation": True,
        }
        if key_configured is not None:
            value["api_key_configured"] = bool(key_configured)
        return value


def parse_fallback_targets(raw: str | list[dict[str, Any]] | dict[str, Any] | None) -> list[FallbackTarget]:
    """Parse a bounded, non-secret fallback catalog.

    Invalid entries are ignored rather than making DeskOrb fail to start.  The
    UI can expose a configuration warning separately; an ignored target can
    never be selected accidentally.
    """
    value: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        value = value.get("targets")
    if not isinstance(value, list):
        return []
    targets: list[FallbackTarget] = []
    seen: set[str] = set()
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get("id") or item.get("target_id") or "").strip()
        model = str(item.get("model") or "").strip()
        provider_raw = str(item.get("provider") or "auto").strip().lower()
        provider = normalize_provider(provider_raw)
        base_url = str(item.get("base_url") or item.get("url") or "").strip().rstrip("/")
        if (provider_raw not in PROVIDERS and provider_raw not in {"gpt", "openai-compatible-gpt", "compatible",
                                                                    "chat", "dashscope", "aliyun", "openai-responses",
                                                                    "responses-compatible", "chat-completions"} or
                not _TARGET_ID.fullmatch(target_id) or target_id in seen or not model or
                not base_url or not _safe_base_url(base_url)):
            continue
        privacy = str(item.get("privacy_level") or "configured").strip().lower()
        if privacy not in _PRIVACY_LEVELS:
            continue
        try:
            context_tokens = max(4_000, min(200_000, int(item.get("context_tokens", 32_000))))
        except (TypeError, ValueError):
            context_tokens = 32_000
        target = FallbackTarget(
            target_id=target_id, provider=provider, model=model, base_url=base_url,
            privacy_level=privacy,
            supports_tools=_as_bool(item.get("supports_tools", True)),
            supports_vision=_as_bool(item.get("supports_vision", True)),
            supports_streaming=_as_bool(item.get("supports_streaming", True)),
            supports_structured_output=_as_bool(item.get("supports_structured_output", True)),
            context_tokens=context_tokens,
        )
        seen.add(target_id)
        targets.append(target)
    return targets


def _safe_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    # Credentials in URLs leak through diagnostics and are never accepted.
    return not parsed.username and not parsed.password


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def capabilities_for(profile: ProviderProfile) -> ModelCapabilities:
    """Return conservative defaults for DeskOrb's supported API protocols.

    A successful health check proves connectivity only; tools and vision remain
    capability declarations so a custom gateway cannot be silently assumed to
    support either feature.
    """
    if profile.protocol == "responses":
        return ModelCapabilities(profile.name, profile.protocol, True, True, True)
    # The project requires an OpenAI-compatible chat endpoint for these
    # providers.  Both capabilities can be disabled by future per-model config
    # before a request reaches the adapter.
    return ModelCapabilities(profile.name, profile.protocol, True, True, True)


def route_model(model: str, capabilities: ModelCapabilities, *, needs_tools: bool = False,
                needs_vision: bool = False) -> RoutingDecision:
    if needs_tools and not capabilities.supports_tools:
        return RoutingDecision(model, capabilities, False, "Configured model does not declare tool-call support.")
    if needs_vision and not capabilities.supports_vision:
        return RoutingDecision(model, capabilities, False, "Configured model does not declare image-input support.")
    return RoutingDecision(model, capabilities, True, "Configured model satisfies requested capabilities.")


class ProviderCapabilityRegistry:
    """In-memory capability registry with explicit, explainable entries."""

    def __init__(self):
        self._items: dict[str, ModelCapabilities] = {}
        self._model_items: dict[tuple[str, str], ModelCapabilities] = {}

    def register(self, capabilities: ModelCapabilities) -> None:
        self._items[capabilities.provider] = capabilities

    def register_model(self, model: str, capabilities: ModelCapabilities) -> None:
        self._model_items[(capabilities.provider, str(model))] = capabilities

    def for_profile(self, profile: ProviderProfile, model: str | None = None) -> ModelCapabilities:
        if model is not None:
            existing_model = self._model_items.get((profile.name, str(model)))
            if existing_model is not None:
                return existing_model
        existing = self._items.get(profile.name)
        if existing is not None:
            return existing
        value = capabilities_for(profile)
        self.register(value)
        return value

    def catalog(self) -> list[dict[str, Any]]:
        values = [value.safe_dict() for value in self._items.values()]
        values.extend({**value.safe_dict(), "model": model[:120]}
                      for (_provider, model), value in self._model_items.items())
        return values


def parse_capability_overrides(raw: str | list[dict[str, Any]] | dict[str, Any] | None) -> list[tuple[str, ModelCapabilities]]:
    """Parse optional per-model capability declarations without secrets."""
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw) if raw.strip() else []
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        value = value.get("models") or value.get("targets")
    if not isinstance(value, list):
        return []
    overrides: list[tuple[str, ModelCapabilities]] = []
    for item in value[:32]:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        provider_raw = str(item.get("provider") or "auto").strip()
        provider = normalize_provider(provider_raw)
        if not model or (provider_raw.lower() not in PROVIDERS and provider_raw.lower() not in {
                "gpt", "openai-compatible-gpt", "compatible", "chat", "dashscope", "aliyun",
                "openai-responses", "responses-compatible", "chat-completions"}):
            continue
        profile = provider_profile(provider, str(item.get("base_url") or ""))
        try:
            context_tokens = max(4_000, min(200_000, int(item.get("context_tokens", 32_000))))
        except (TypeError, ValueError):
            context_tokens = 32_000
        privacy = str(item.get("privacy_level") or "configured").strip().lower()
        if privacy not in _PRIVACY_LEVELS:
            continue
        capabilities = ModelCapabilities(
            profile.name, profile.protocol, _as_bool(item.get("supports_tools", True)),
            _as_bool(item.get("supports_vision", True)), _as_bool(item.get("supports_streaming", True)),
            _as_bool(item.get("supports_structured_output", True)), context_tokens, privacy,
        )
        overrides.append((model, capabilities))
    return overrides


class ModelHealthStore:
    """Store provider health metadata only, never request or response content."""

    DEFAULT_RETENTION = 500

    def __init__(self, path: str | Path, *, retention: int = DEFAULT_RETENTION):
        self.path = Path(path)
        self.retention = max(20, min(10_000, int(retention)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS model_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                model TEXT NOT NULL, operation TEXT NOT NULL DEFAULT 'health',
                ok INTEGER NOT NULL, latency_ms REAL, first_token_ms REAL,
                status_code INTEGER, retry_count INTEGER NOT NULL DEFAULT 0,
                tool_rounds INTEGER, verification_passed INTEGER,
                failure_kind TEXT, created_at REAL NOT NULL
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(model_health)").fetchall()}
            migrations = {
                "operation": "ALTER TABLE model_health ADD COLUMN operation TEXT NOT NULL DEFAULT 'health'",
                "first_token_ms": "ALTER TABLE model_health ADD COLUMN first_token_ms REAL",
                "status_code": "ALTER TABLE model_health ADD COLUMN status_code INTEGER",
                "retry_count": "ALTER TABLE model_health ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
                "tool_rounds": "ALTER TABLE model_health ADD COLUMN tool_rounds INTEGER",
                "verification_passed": "ALTER TABLE model_health ADD COLUMN verification_passed INTEGER",
            }
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(statement)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_model_health_lookup "
                               "ON model_health(provider, model, id DESC)")

    def record(self, *, provider: str, model: str, ok: bool, latency_ms: float,
               failure_kind: str | None = None, operation: str = "health",
               first_token_ms: float | None = None, status_code: int | None = None,
               retry_count: int = 0, tool_rounds: int | None = None,
               verification_passed: bool | None = None) -> None:
        try:
            status = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status = None
        try:
            retries = max(0, min(20, int(retry_count)))
        except (TypeError, ValueError):
            retries = 0
        try:
            rounds = max(0, min(100, int(tool_rounds))) if tool_rounds is not None else None
        except (TypeError, ValueError):
            rounds = None
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""INSERT INTO model_health(
                provider, model, operation, ok, latency_ms, first_token_ms,
                status_code, retry_count, tool_rounds, verification_passed,
                failure_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (str(provider)[:80], str(model)[:120], str(operation or "health")[:40],
                                int(bool(ok)), float(latency_ms),
                                float(first_token_ms) if first_token_ms is not None else None,
                                status, retries, rounds,
                                int(bool(verification_passed)) if verification_passed is not None else None,
                                str(failure_kind)[:80] if failure_kind else None, time.time()))
            # Keep long-running overlays bounded while preserving the newest
            # samples for each provider/model pair.
            connection.execute("""DELETE FROM model_health
                WHERE provider = ? AND model = ? AND id NOT IN (
                    SELECT id FROM model_health WHERE provider = ? AND model = ?
                    ORDER BY id DESC LIMIT ?
                )""", (str(provider)[:80], str(model)[:120], str(provider)[:80],
                       str(model)[:120], self.retention))

    def summary(self, provider: str, model: str, *, limit: int = 100,
                operation: str | None = None) -> dict[str, Any]:
        where = "provider=? AND model=?"
        params: list[Any] = [str(provider), str(model)]
        if operation:
            where += " AND operation=?"
            params.append(str(operation))
        params.append(max(1, min(500, int(limit))))
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(f"""SELECT ok, latency_ms, first_token_ms,
                    retry_count, status_code, verification_passed, failure_kind
                    FROM model_health WHERE {where} ORDER BY id DESC LIMIT ?""", params).fetchall()
        latencies = sorted(float(row[1]) for row in rows if row[1] is not None)
        first_tokens = sorted(float(row[2]) for row in rows if row[2] is not None)
        retry_rows = [row for row in rows if int(row[3] or 0) > 0]
        verification_rows = [row for row in rows if row[5] is not None]
        status_codes: dict[str, int] = {}
        failure_kinds: dict[str, int] = {}
        for row in rows:
            if row[4] is not None:
                key = str(int(row[4]))
                status_codes[key] = status_codes.get(key, 0) + 1
            if row[6]:
                key = str(row[6])[:80]
                failure_kinds[key] = failure_kinds.get(key, 0) + 1
        return {"provider": str(provider), "model": str(model), "samples": len(rows),
                "operation": str(operation) if operation else "all",
                "success_rate": round(sum(bool(row[0]) for row in rows) / len(rows), 4) if rows else None,
                "p50_latency_ms": _percentile(latencies, 0.50),
                "p95_latency_ms": _percentile(latencies, 0.95),
                "p50_first_token_ms": _percentile(first_tokens, 0.50),
                "p95_first_token_ms": _percentile(first_tokens, 0.95),
                "retry_rate": round(len(retry_rows) / len(rows), 4) if rows else None,
                "verification_rate": (round(sum(bool(row[5]) for row in verification_rows) /
                                             len(verification_rows), 4) if verification_rows else None),
                "status_codes": status_codes, "failure_kinds": failure_kinds}


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return values[round((len(values) - 1) * quantile)]
