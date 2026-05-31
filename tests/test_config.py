import pytest

from nango_mcp.config import env_key_for_slug, load_settings


def test_env_key_for_slug_normalizes_secret_variable_suffixes() -> None:
    assert env_key_for_slug("prod") == "PROD"
    assert env_key_for_slug("customer-a") == "CUSTOMER_A"


def test_load_settings_supports_single_direct_environment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NANGO_SECRET_KEY=secret_default\nNANGO_ENVIRONMENT=local\n")
    monkeypatch.setenv("NANGO_MCP_ENV_FILE", str(env_file))

    settings = load_settings()

    assert settings.nango_url == "https://api.nango.dev"
    assert settings.secret_resolver == "direct"
    assert settings.environments[0].slug == "local"
    assert settings.environments[0].secret_key == "secret_default"


def test_load_settings_supports_multiple_direct_environments(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NANGO_BASE_URL=https://nango.example.test",
                "NANGO_MCP_ENVIRONMENTS=dev,prod",
                "NANGO_SECRET_KEY_DEV=dev-secret",
                "NANGO_SECRET_KEY_PROD=prod-secret",
                "NANGO_MCP_ENVIRONMENT_ALIASES_PROD=live,production",
            ]
        )
    )
    monkeypatch.setenv("NANGO_MCP_ENV_FILE", str(env_file))

    settings = load_settings()

    assert settings.nango_url == "https://nango.example.test"
    assert [item.slug for item in settings.environments] == ["dev", "prod"]
    assert settings.environments[1].aliases == ("live", "production")


def test_load_settings_requires_direct_secret(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NANGO_MCP_ENVIRONMENTS=prod\n")
    monkeypatch.setenv("NANGO_MCP_ENV_FILE", str(env_file))

    with pytest.raises(RuntimeError, match="NANGO_SECRET_KEY_PROD"):
        load_settings()


def test_load_settings_supports_optional_mutation_guards(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NANGO_SECRET_KEY=secret_default",
                "NANGO_MCP_READ_ONLY=true",
                "NANGO_MCP_REQUIRE_CONFIRMATION=true",
            ]
        )
    )
    monkeypatch.setenv("NANGO_MCP_ENV_FILE", str(env_file))

    settings = load_settings()

    assert settings.read_only is True
    assert settings.require_confirmation is True
