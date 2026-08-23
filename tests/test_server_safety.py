import pytest

from nango_mcp import server
from nango_mcp.config import EnvironmentConfig, Settings


def _set_settings(*, read_only: bool = False) -> None:
    server._settings = Settings(
        nango_url="https://api.nango.dev",
        environments=(EnvironmentConfig(slug="default", secret_key="secret"),),
        read_only=read_only,
    )
    server._resolver = None
    server._nango = None


def teardown_function() -> None:
    server._settings = None
    server._resolver = None
    server._nango = None


def test_writable_guard_is_off_by_default() -> None:
    _set_settings()

    server._assert_writable()


def test_native_approval_state_binds_tool_environment_and_arguments() -> None:
    result = server._input_required(
        "delete_integration",
        "sandbox",
        ("calendar",),
        "snapshot-digest",
    )

    assert result.result_type == "input_required"
    assert "confirmation" not in str(result).lower()
    assert "sandbox" in result.request_state
    assert "delete_integration" in result.request_state


def test_read_only_mode_blocks_mutations() -> None:
    _set_settings(read_only=True)

    with pytest.raises(ValueError, match="read-only mode"):
        server._assert_writable()
