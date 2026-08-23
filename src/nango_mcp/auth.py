from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import validate_environment_slug


@dataclass(frozen=True)
class CallerScope:
    label: str
    environments: frozenset[str]
    denied_tools: frozenset[str] = frozenset()
    allowed_proxy_methods: frozenset[str] = frozenset({"*"})
    denied_proxy_path_patterns: tuple[str, ...] = ()
    mutation_approval: str = "server"
    server_approval_proxy_path_patterns: tuple[str, ...] = ()

    @property
    def is_broad(self) -> bool:
        return self.environments == frozenset({"*"})


TokenRegistry = dict[str, CallerScope]


def _validate_environment(value: str, denied: set[str] | frozenset[str]) -> str:
    environment = validate_environment_slug(value)
    if environment in denied:
        raise PermissionError(f"environment {environment!r} is denied")
    return environment


class TokenRegistrySource:
    """Validated token policy with optional atomic file-backed hot reload."""

    def __init__(
        self,
        raw: str,
        denied_environments: set[str] | frozenset[str],
        path: str = "",
    ) -> None:
        self._raw = raw
        self._denied_environments = frozenset(denied_environments)
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._signature: tuple[int, int, int] | None = None
        self._registry: TokenRegistry = {}
        self.current()

    def current(self) -> TokenRegistry:
        if self._path is None:
            if not self._registry:
                self._registry = load_token_registry(self._raw, self._denied_environments)
            return self._registry

        stat = self._path.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return self._registry
        with self._lock:
            stat = self._path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if signature != self._signature:
                registry = load_token_registry(self._path.read_text(), self._denied_environments)
                self._registry = registry
                self._signature = signature
        return self._registry

    def explicit_environments(self) -> frozenset[str]:
        return frozenset(
            environment
            for scope in self.current().values()
            for environment in scope.environments
            if environment != "*"
        )

_current_scope: ContextVar[CallerScope | None] = ContextVar("nango_mcp_caller_scope", default=None)


def load_token_registry(
    raw: str,
    denied_environments: set[str] | frozenset[str],
) -> TokenRegistry:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("NANGO_MCP_TOKENS must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("NANGO_MCP_TOKENS must be a JSON object keyed by SHA-256 digest")

    registry: TokenRegistry = {}
    label_policies: dict[str, CallerScope] = {}
    for digest, policy in payload.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("NANGO_MCP_TOKENS keys must be canonical lowercase SHA-256 digests")
        if not isinstance(policy, dict):
            raise RuntimeError("Each NANGO_MCP_TOKENS entry must be an object")
        label = policy.get("label")
        scopes = policy.get("scopes")
        denied_tools = policy.get("denied_tools", [])
        allowed_proxy_methods = policy.get("allowed_proxy_methods", ["*"])
        denied_proxy_path_patterns = policy.get("denied_proxy_path_patterns", [])
        mutation_approval = policy.get("mutation_approval", "server")
        server_approval_proxy_path_patterns = policy.get(
            "server_approval_proxy_path_patterns", []
        )
        if not isinstance(label, str) or not label.strip():
            raise RuntimeError("Each NANGO_MCP_TOKENS entry requires a non-empty label")
        if not isinstance(scopes, list) or not scopes:
            raise RuntimeError("Each NANGO_MCP_TOKENS entry requires non-empty scopes")

        normalized_scopes: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str) or not scope.strip():
                raise RuntimeError("NANGO_MCP_TOKENS scopes must be non-empty strings")
            value = scope.strip().lower()
            if value == "*":
                normalized_scopes.add(value)
            else:
                try:
                    normalized_scopes.add(_validate_environment(value, denied_environments))
                except (ValueError, PermissionError) as exc:
                    raise RuntimeError(f"Invalid NANGO_MCP_TOKENS scope {value!r}") from exc
        if "*" in normalized_scopes and len(normalized_scopes) != 1:
            raise RuntimeError("A broad '*' scope cannot be mixed with explicit environment names")

        if not isinstance(denied_tools, list) or not all(
            isinstance(item, str) and item.strip() for item in denied_tools
        ):
            raise RuntimeError("denied_tools must be a list of non-empty strings")
        if not isinstance(allowed_proxy_methods, list) or not allowed_proxy_methods or not all(
            isinstance(item, str) and item.strip() for item in allowed_proxy_methods
        ):
            raise RuntimeError("allowed_proxy_methods must be a non-empty list of methods")
        methods = frozenset(item.strip().upper() for item in allowed_proxy_methods)
        if "*" in methods and len(methods) != 1:
            raise RuntimeError("A broad '*' proxy method cannot be mixed with explicit methods")
        if not isinstance(denied_proxy_path_patterns, list) or not all(
            isinstance(item, str) and item.strip() for item in denied_proxy_path_patterns
        ):
            raise RuntimeError("denied_proxy_path_patterns must be a list of regex strings")
        for pattern in denied_proxy_path_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuntimeError(f"Invalid denied proxy path regex {pattern!r}") from exc
        if not isinstance(server_approval_proxy_path_patterns, list) or not all(
            isinstance(item, str) and item.strip()
            for item in server_approval_proxy_path_patterns
        ):
            raise RuntimeError(
                "server_approval_proxy_path_patterns must be a list of regex strings"
            )
        for pattern in server_approval_proxy_path_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuntimeError(
                    f"Invalid server approval proxy path regex {pattern!r}"
                ) from exc
        if mutation_approval not in {"server", "host"}:
            raise RuntimeError("mutation_approval must be 'server' or 'host'")

        clean_label = label.strip()
        frozen_scopes = frozenset(normalized_scopes)
        caller_scope = CallerScope(
            label=clean_label,
            environments=frozen_scopes,
            denied_tools=frozenset(item.strip() for item in denied_tools),
            allowed_proxy_methods=methods,
            denied_proxy_path_patterns=tuple(
                item.strip() for item in denied_proxy_path_patterns
            ),
            mutation_approval=mutation_approval,
            server_approval_proxy_path_patterns=tuple(
                item.strip() for item in server_approval_proxy_path_patterns
            ),
        )
        existing_policy = label_policies.get(clean_label)
        if existing_policy is not None and existing_policy != caller_scope:
            raise RuntimeError(f"Duplicate caller label {clean_label!r} has conflicting scope policies")
        label_policies[clean_label] = caller_scope
        if digest in registry:
            raise RuntimeError("NANGO_MCP_TOKENS contains a duplicate digest")
        registry[digest] = caller_scope
    return registry


def authenticate(authorization_header: str | None, registry: TokenRegistry) -> CallerScope:
    prefix = "Bearer "
    if not authorization_header or not authorization_header.startswith(prefix):
        raise PermissionError("unauthorized")
    token = authorization_header[len(prefix) :].strip()
    if not re.fullmatch(r"nangomcp1_[A-Za-z0-9_-]{43,}", token):
        raise PermissionError("unauthorized")
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for digest, caller_scope in registry.items():
        if hmac.compare_digest(candidate, digest):
            return caller_scope
    raise PermissionError("unauthorized")


def permitted_environments(process_environments: set[str] | frozenset[str], caller_scope: CallerScope) -> frozenset[str]:
    concrete = frozenset(process_environments)
    permitted = concrete if caller_scope.is_broad else concrete.intersection(caller_scope.environments)
    if not permitted:
        raise PermissionError("caller scope does not permit any configured environment")
    return frozenset(permitted)


def set_scope(scope: CallerScope) -> Token[CallerScope | None]:
    return _current_scope.set(scope)


def reset_scope(token: Token[CallerScope | None]) -> None:
    _current_scope.reset(token)


def require_scope() -> CallerScope:
    scope = _current_scope.get()
    if scope is None:
        raise PermissionError("caller scope is unavailable")
    return scope


def authorize_operation(
    scope: CallerScope,
    tool: str,
    *,
    provider_config_key: str = "",
    method: str = "",
    path: str = "",
) -> None:
    """Apply caller-specific operation policy after authentication."""
    if tool in scope.denied_tools:
        raise PermissionError(f"caller policy denies tool {tool!r}")
    if tool not in {"proxy_request", "download_provider_file"}:
        return
    normalized_method = method.strip().upper()
    if "*" not in scope.allowed_proxy_methods and normalized_method not in scope.allowed_proxy_methods:
        raise PermissionError(f"caller policy denies proxy method {normalized_method!r}")
    if proxy_route_matches(
        scope.denied_proxy_path_patterns,
        provider_config_key=provider_config_key,
        method=normalized_method,
        path=path,
    ):
        raise PermissionError("caller policy denies this provider route")


def proxy_route_matches(
    patterns: tuple[str, ...],
    *,
    provider_config_key: str,
    method: str,
    path: str,
) -> bool:
    """Match provider routes in either provider:path or METHOD:provider:path form."""
    route = f"{provider_config_key}:{path}"
    method_route = f"{method.strip().upper()}:{route}"
    return any(
        re.search(pattern, route, re.I) or re.search(pattern, method_route, re.I)
        for pattern in patterns
    )


def registry_summary(registry: TokenRegistry) -> list[dict[str, Any]]:
    return [
        {
            "label": scope.label,
            "scopes": sorted(scope.environments),
            "denied_tools": sorted(scope.denied_tools),
            "allowed_proxy_methods": sorted(scope.allowed_proxy_methods),
            "denied_proxy_path_patterns": list(scope.denied_proxy_path_patterns),
            "mutation_approval": scope.mutation_approval,
            "server_approval_proxy_path_patterns": list(
                scope.server_approval_proxy_path_patterns
            ),
        }
        for scope in registry.values()
    ]
