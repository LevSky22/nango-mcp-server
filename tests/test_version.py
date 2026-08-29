import tomllib
from pathlib import Path

import nango_mcp
from nango_mcp import server


def test_release_versions_are_synchronized() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert nango_mcp.__version__ == "2.0.0"
    assert pyproject["project"]["version"] == "2.0.0"
    assert server.mcp.version == "2.0.0"
