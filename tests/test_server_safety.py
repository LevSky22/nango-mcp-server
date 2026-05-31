import pytest

from nango_mcp import server
from nango_mcp.config import EnvironmentConfig, Settings


def _set_settings(*, read_only: bool = False, require_confirmation: bool = False) -> None:
    server._settings = Settings(
        nango_url="https://api.nango.dev",
        environments=(EnvironmentConfig(slug="default", secret_key="secret"),),
        read_only=read_only,
        require_confirmation=require_confirmation,
    )
    server._resolver = None
    server._nango = None


def teardown_function() -> None:
    server._settings = None
    server._resolver = None
    server._nango = None


def test_confirmation_guard_is_off_by_default() -> None:
    _set_settings()

    server._assert_confirmation("", server.WRITE_CONFIRMATION)


def test_confirmation_guard_can_be_required() -> None:
    _set_settings(require_confirmation=True)

    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server._assert_confirmation("", server.WRITE_CONFIRMATION)


def test_read_only_mode_blocks_mutations() -> None:
    _set_settings(read_only=True)

    with pytest.raises(ValueError, match="read-only mode"):
        server._assert_confirmation(server.WRITE_CONFIRMATION, server.WRITE_CONFIRMATION)
