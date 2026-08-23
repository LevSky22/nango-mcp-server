import hashlib

import pytest

from nango_mcp.binary_resources import BinaryResourceStore


def test_binary_resource_is_opaque_private_and_scope_bound(tmp_path) -> None:
    source = tmp_path / "incoming"
    source.write_bytes(b"example payload")
    store = BinaryResourceStore(str(tmp_path / "resources"), 3600)
    resource = store.ingest(
        source,
        owner="automation",
        environment="sandbox",
        content_type="application/pdf; charset=binary",
        byte_length=15,
        sha256=hashlib.sha256(b"example payload").hexdigest(),
        suggested_name="report.pdf",
    )

    assert resource["uri"] == f"nango-mcp://download/{resource['id']}"
    assert resource["contentType"] == "application/pdf"
    assert not source.exists()
    content, metadata = store.read_authorized(
        resource["id"],
        owner="automation",
        environments=frozenset({"sandbox"}),
    )
    assert content == b"example payload"
    assert metadata["suggestedName"] == "report.pdf"

    with pytest.raises(PermissionError):
        store.read_authorized(
            resource["id"],
            owner="different-caller",
            environments=frozenset({"sandbox"}),
        )
