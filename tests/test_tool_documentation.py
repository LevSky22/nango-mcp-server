import re
from pathlib import Path

from nango_mcp import server


TOOL_HEADING = re.compile(r"^### `([a-z][a-z0-9_]*)`$", re.MULTILINE)


def test_complete_tool_reference_matches_registered_tools() -> None:
    reference = Path(__file__).parents[1] / "docs" / "tools.md"
    documented = TOOL_HEADING.findall(reference.read_text(encoding="utf-8"))
    registered = sorted(server.mcp._tool_manager._tools)  # type: ignore[attr-defined]

    assert len(documented) == len(set(documented)), "tool reference contains duplicate tool headings"
    assert sorted(documented) == registered


def test_readme_links_complete_tool_reference() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "[complete tool reference](https://github.com/LevSky22/nango-mcp-server/blob/main/docs/tools.md)" in readme
