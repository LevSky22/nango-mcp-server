import pytest

from nango_mcp.config import EnvironmentConfig
from nango_mcp.discovery import resolve_environment, validate_environment_slug


def test_resolve_environment_accepts_configured_aliases() -> None:
    environments = (
        EnvironmentConfig(slug="production", aliases=("prod", "live")),
        EnvironmentConfig(slug="sandbox", aliases=("dev",)),
    )

    assert resolve_environment("prod", environments).slug == "production"
    assert resolve_environment("LIVE", environments).slug == "production"
    assert resolve_environment("sandbox", environments).slug == "sandbox"


def test_validate_environment_slug_blocks_path_traversal() -> None:
    assert validate_environment_slug(" PROD ") == "prod"

    with pytest.raises(ValueError):
        validate_environment_slug("../prod")


def test_resolve_environment_rejects_unconfigured_or_ambiguous_aliases() -> None:
    environments = (
        EnvironmentConfig(slug="production", aliases=("live",)),
        EnvironmentConfig(slug="prod-copy", aliases=("live",)),
    )

    with pytest.raises(ValueError):
        resolve_environment("live", environments)

    with pytest.raises(PermissionError):
        resolve_environment("staging", environments)
