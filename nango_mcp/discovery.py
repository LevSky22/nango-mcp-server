from __future__ import annotations

import re
import unicodedata

from .config import EnvironmentConfig


def normalize_alias(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value.lower())


def validate_environment_slug(environment: str) -> str:
    slug = environment.strip().lower()
    if not slug:
        raise ValueError("environment is required")
    if "/" in slug or ".." in slug:
        raise ValueError("environment must be a simple slug")
    return slug


def resolve_environment(environment: str, environments: tuple[EnvironmentConfig, ...]) -> EnvironmentConfig:
    slug = validate_environment_slug(environment)
    normalized = normalize_alias(slug)
    matches = [
        item
        for item in environments
        if normalized == normalize_alias(item.slug) or normalized in {normalize_alias(alias) for alias in item.aliases}
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"environment reference {environment!r} is ambiguous")
    raise PermissionError(f"environment {environment!r} is not configured")
