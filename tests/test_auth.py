import hashlib
import json

import pytest

from nango_mcp.auth import CallerScope, TokenRegistrySource, authenticate, load_token_registry


def _token(label: str) -> str:
    return "nangomcp1_" + label.ljust(43, "x")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def test_registry_authenticates_digest_without_retaining_raw_token() -> None:
    token = _token("local")
    registry = load_token_registry(
        json.dumps({_digest(token): {"label": "automation", "scopes": ["sandbox"]}}),
        frozenset(),
    )

    assert authenticate(f"Bearer {token}", registry) == CallerScope(
        "automation",
        frozenset({"sandbox"}),
    )
    assert token not in repr(registry)


def test_registry_rejects_denied_environment() -> None:
    token = _token("local")
    with pytest.raises(RuntimeError, match="Invalid NANGO_MCP_TOKENS scope"):
        load_token_registry(
            json.dumps({_digest(token): {"label": "automation", "scopes": ["production"]}}),
            frozenset({"production"}),
        )


def test_registry_file_hot_reloads_atomically(tmp_path) -> None:
    old_token = _token("old")
    new_token = _token("new")
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({_digest(old_token): {"label": "old", "scopes": ["sandbox"]}}))
    source = TokenRegistrySource("", frozenset(), str(path))
    assert authenticate(f"Bearer {old_token}", source.current()).label == "old"

    replacement = tmp_path / "tokens.next"
    replacement.write_text(json.dumps({_digest(new_token): {"label": "new", "scopes": ["sandbox"]}}))
    replacement.replace(path)
    assert authenticate(f"Bearer {new_token}", source.current()).label == "new"
    with pytest.raises(PermissionError):
        authenticate(f"Bearer {old_token}", source.current())
