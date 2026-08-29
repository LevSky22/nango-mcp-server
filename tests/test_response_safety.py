import base64
import hashlib
import hmac
import json

import pytest

from nango_mcp.response_safety import (
    HARD_RESULT_BUDGET_BYTES,
    INLINE_BUDGET_BYTES,
    ArtifactStore,
    bound_proxy_response,
    serialized_bytes,
)


def _envelope(response):
    return {
        "ok": True,
        "status": 200,
        "contentType": "application/json",
        "responseHeaders": {},
        "response": response,
    }


def test_proxy_response_makes_progress_when_one_item_exceeds_inline_budget(tmp_path) -> None:
    """
    bound_proxy_response used to issue a cursor at the *same* offset when a single item
    was too big, so the caller followed it forever receiving an empty list.
    """
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 8 * 1024 * 1024)
    rows = [{"id": index, "blob": "x" * 40_000} for index in range(3)]
    result = bound_proxy_response(
        _envelope(rows),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        response_page_size=20,
    )
    assert result["responseMeta"]["returnedCount"] == 1
    assert result["responseMeta"]["truncationReason"] == "item_size_limit"

    # Following the cursor must advance rather than repeat the same offset.
    seen_offsets = [0]
    cursor = result["responseMeta"]["nextCursor"]
    guard = 0
    while cursor:
        guard += 1
        assert guard < 20, "pagination failed to terminate"
        page = bound_proxy_response(
            _envelope(rows),
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            response_page_size=20,
            response_cursor=cursor,
        )
        assert page["responseMeta"]["returnedCount"] >= 1
        seen_offsets.append(seen_offsets[-1] + page["responseMeta"]["returnedCount"])
        cursor = page["responseMeta"]["nextCursor"]
    assert seen_offsets == sorted(set(seen_offsets)), "offsets must strictly advance"


def test_proxy_artifact_stores_full_envelope_not_the_projection(tmp_path) -> None:
    """
    The artifact must hold the unreduced envelope. Writing it post-projection meant later
    artifact queries ran against a reduced copy while presenting as authoritative.
    """
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 8 * 1024 * 1024)
    rows = [{"id": index, "kind": "task", "secret": "s" * 400} for index in range(200)]
    result = bound_proxy_response(
        _envelope(rows),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["id"],
        response_filter=[{"path": "/kind", "op": "eq", "value": "task"}],
    )
    artifact_id = result["responseMeta"]["artifact"]["id"]
    assert result["response"][0] == {"id": 0}

    # A field the inline view dropped is still queryable from the artifact.
    page = store.query(
        artifact_id,
        owner="operator",
        environment="sandbox",
        response_path="/response",
        fields=["secret"],
        response_filter=None,
        response_page_size=1,
        cursor=None,
    )
    assert page["response"][0]["secret"] == "s" * 400
    assert page["responseMeta"]["totalCount"] == 200
    assert "FULL provider envelope" in result["responseMeta"]["warning"]


def test_artifact_descriptor_and_scoped_delete(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({"sensitive": "stored-only"}),
        owner="operator",
        environment="sandbox",
    )

    descriptor = store.describe(artifact["id"], owner="operator", environment="sandbox")
    assert descriptor["descriptorVersion"] == 1
    assert descriptor["contractVersion"] == 2
    assert descriptor["rawReadable"] is False
    assert artifact["uri"] == f"nango-mcp://artifact/{artifact['id']}"
    content, metadata = store.read_authorized(
        artifact["id"],
        owner="operator",
        environments=frozenset({"sandbox"}),
    )
    assert b"stored-only" in content
    assert metadata["environment"] == "sandbox"
    assert descriptor["responseRoot"] == "/response"
    assert "stored-only" not in json.dumps(descriptor)
    with pytest.raises(PermissionError):
        store.describe(artifact["id"], owner="other", environment="sandbox")
    assert store.delete(artifact["id"], owner="operator", environment="sandbox") is True
    assert store.delete(artifact["id"], owner="operator", environment="sandbox") is False


def test_filter_path_that_never_resolves_is_an_error_not_a_complete_zero(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({"rows": [{"status": "success"} for _ in range(10)]}),
        owner="operator",
        environment="sandbox",
    )
    with pytest.raises(ValueError, match="did not resolve on any"):
        store.query(
            artifact["id"],
            owner="operator",
            environment="sandbox",
            response_path="/response/rows",
            fields=None,
            response_filter=[{"path": "/data/status", "op": "eq", "value": "error"}],
            response_page_size=20,
            cursor=None,
        )

    # A genuine zero-match stays a clean, honest zero.
    page = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/rows",
        fields=None,
        response_filter=[{"path": "/status", "op": "eq", "value": "error"}],
        response_page_size=20,
        cursor=None,
    )
    assert page["response"] == []
    assert page["responseMeta"]["complete"] is True
    assert page["responseMeta"]["filtersApplied"][0]["resolvedOn"] == 10
    assert page["responseMeta"]["filtersApplied"][0]["matched"] == 0


def test_small_response_is_backward_compatible_and_has_metadata(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"id": "one", "name": "Example"}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
    )
    assert result["response"] == {"id": "one", "name": "Example"}
    assert result["responseMeta"]["truncated"] is False
    assert result["responseMeta"]["artifact"] is None
    assert len(serialized_bytes(result)) < HARD_RESULT_BUDGET_BYTES


def test_proxy_infers_unambiguous_array_envelope_for_projection(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"fields": [{"id": "one", "name": "Example"}]}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["id", "name"],
    )
    assert result["response"] == [{"id": "one", "name": "Example"}]
    assert result["responseMeta"]["inferredResponsePath"] == "/response/fields"
    assert result["responseMeta"]["fieldsResolved"] == {"id": 1, "name": 1}


def test_proxy_infers_empty_array_envelope_without_claiming_fields_resolved(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"fields": []}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["id", "name"],
    )
    assert result["response"] == []
    assert result["responseMeta"]["inferredResponsePath"] == "/response/fields"
    assert result["responseMeta"]["fieldsResolved"] == {"id": 0, "name": 0}


def test_proxy_keeps_root_projection_when_root_fields_match(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"id": "root", "items": [{"id": "child"}]}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["id"],
    )
    assert result["response"] == {"id": "root"}
    assert "inferredResponsePath" not in result["responseMeta"]


def test_proxy_infers_unambiguous_array_envelope_for_filtering(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"rows": [{"kind": "keep"}, {"kind": "drop"}]}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        response_filter=[{"path": "/kind", "op": "eq", "value": "keep"}],
    )
    assert result["response"] == [{"kind": "keep"}]
    assert result["responseMeta"]["inferredResponsePath"] == "/response/rows"


def test_proxy_does_not_guess_between_multiple_array_children(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    with pytest.raises(ValueError, match="fields matched no properties"):
        bound_proxy_response(
            _envelope({"rows": [{"id": 1}], "errors": []}),
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            fields=["id"],
        )


def test_proxy_infers_below_an_explicit_envelope_path(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"data": {"rows": [{"id": 1}], "returned": 1}}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        response_path="/response/data",
        fields=["id"],
    )
    assert result["response"] == [{"id": 1}]
    assert result["responseMeta"]["inferredResponsePath"] == "/response/data/rows"


def test_list_is_paginated_with_opaque_cursor_and_projection(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    envelope = _envelope([{"id": index, "large": "x" * 100} for index in range(55)])
    first = bound_proxy_response(
        envelope,
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["/id"],
        response_page_size=20,
    )
    assert first["response"] == [{"/id": index} for index in range(20)]
    assert first["responseMeta"]["returnedCount"] == 20
    assert first["responseMeta"]["totalCount"] == 55
    assert first["responseMeta"]["nextCursor"]

    second = bound_proxy_response(
        envelope,
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["/id"],
        response_page_size=20,
        response_cursor=first["responseMeta"]["nextCursor"],
    )
    assert second["response"][0] == {"/id": 20}


def test_projection_accepts_plain_root_field_names(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope([{"id": "one", "name": "First"}, {"id": "two", "name": "Second"}]),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        fields=["id", "name"],
    )
    assert result["response"] == [
        {"id": "one", "name": "First"},
        {"id": "two", "name": "Second"},
    ]
    assert result["responseMeta"]["complete"] is True
    assert result["responseMeta"]["remainingCount"] == 0


def test_projection_rejects_an_all_missing_view(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    with pytest.raises(ValueError, match="matched no properties"):
        bound_proxy_response(
            _envelope([{"id": "one"}, {"id": "two"}]),
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            fields=["unknown"],
        )


def test_large_response_is_artifact_backed_and_query_only(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope({"records": [{"id": index, "body": "z" * 1000} for index in range(80)]}),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
    )
    artifact = result["responseMeta"]["artifact"]
    assert result["responseMeta"]["truncated"] is True
    assert result["responseMeta"]["complete"] is False
    assert result["responseMeta"]["warning"].startswith("INCOMPLETE RESULT")
    assert artifact["responseRoot"] == "/response"
    assert artifact["queryTool"] == "query_response_artifact"
    assert "read_tool" not in artifact
    assert artifact["byteLength"] > 32 * 1024
    assert len(serialized_bytes(result)) < HARD_RESULT_BUDGET_BYTES

    page = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response/records", fields=["id"], response_filter=None,
        response_page_size=5, cursor=None,
    )
    assert page["response"] == [{"id": index} for index in range(5)]
    assert page["responseMeta"]["nextCursor"]
    with pytest.raises(PermissionError):
        store.query(
            artifact["id"], owner="different", environment="sandbox",
            response_path="/response/records", fields=["id"], response_filter=None,
            response_page_size=5, cursor=None,
        )


def test_artifact_text_search_returns_bounded_contexts(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({
            "records": [
                {"body": f"{'x' * 20_000}Needle{'y' * 20_000}"},
                {"body": "needle again"},
            ]
        }),
        owner="operator",
        environment="sandbox",
    )
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response/records", fields=None, response_filter=None,
        response_page_size=20, cursor=None, text_search={"query": "needle"},
    )

    assert len(result["response"]) == 2
    assert result["response"][0]["pointer"] == "/response/records/0/body"
    assert result["response"][0]["offset"] == 20_000
    assert len(result["response"][0]["context"]) <= 240
    assert result["responseMeta"]["complete"] is True
    assert len(serialized_bytes(result)) < INLINE_BUDGET_BYTES


def test_keyed_object_errors_include_executable_entry_mode_request(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({
            "Node A": {"id": "a", "type": "code"},
            "Node B": {"id": "b", "type": "webhook"},
        }),
        owner="operator",
        environment="sandbox",
    )

    with pytest.raises(ValueError, match="OBJECT_MODE_REQUIRED") as error:
        store.query(
            artifact["id"], owner="operator", environment="sandbox",
            response_path="/response", fields=["id", "type"],
            response_filter=[{"path": "/from", "op": "eq", "value": "Node A"}],
            response_page_size=20, cursor=None,
        )
    assert '"objectMode":"entries"' in str(error.value)
    assert '"/value/id"' in str(error.value)
    assert '"path":"/key"' in str(error.value)


def test_artifact_query_is_provider_agnostic_filtered_projected_and_paginated(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    document = _envelope({
        "items": [
            {
                "id": index,
                "state": {"name": "open" if index % 2 else "closed"},
                "score": index,
                "provider_specific_payload": "x" * 2000,
            }
            for index in range(30)
        ],
        "last_page": True,
    })
    artifact = store.write(document, owner="operator", environment="sandbox")

    first = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/items",
        fields=["/id", "/state/name"],
        response_filter=[
            {"path": "/state/name", "op": "eq", "value": "open"},
            {"path": "/score", "op": "gte", "value": 5},
        ],
        response_page_size=5,
        cursor=None,
    )

    assert first["response"] == [
        {"/id": index, "/state/name": "open"} for index in (5, 7, 9, 11, 13)
    ]
    assert first["responseMeta"]["returnedCount"] == 5
    assert first["responseMeta"]["totalCount"] == 13
    assert first["responseMeta"]["nextCursor"]

    second = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/items",
        fields=["/id", "/state/name"],
        response_filter=[
            {"path": "/state/name", "op": "eq", "value": "open"},
            {"path": "/score", "op": "gte", "value": 5},
        ],
        response_page_size=5,
        cursor=first["responseMeta"]["nextCursor"],
    )
    assert second["response"][0]["/id"] == 15


def test_artifact_query_cursor_is_bound_to_artifact_owner_client_and_view(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    first_artifact = store.write(_envelope({"rows": list(range(5))}), owner="operator", environment="sandbox")
    # Distinct content: artifact ids are content-addressed per caller scope, so identical
    # payloads deliberately collapse to a single handle.
    second_artifact = store.write(_envelope({"rows": list(range(5, 10))}), owner="operator", environment="sandbox")
    first = store.query(
        first_artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/rows",
        fields=None,
        response_filter=None,
        response_page_size=2,
        cursor=None,
    )

    with pytest.raises(ValueError, match="does not match"):
        store.query(
            second_artifact["id"],
            owner="operator",
            environment="sandbox",
            response_path="/response/rows",
            fields=None,
            response_filter=None,
            response_page_size=2,
            cursor=first["responseMeta"]["nextCursor"],
        )
    with pytest.raises(PermissionError):
        store.query(
            first_artifact["id"],
            owner="different",
            environment="sandbox",
            response_path="/response/rows",
            fields=None,
            response_filter=None,
            response_page_size=2,
            cursor=None,
        )


def test_artifact_query_makes_progress_when_one_item_exceeds_inline_budget(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({"rows": [{"id": 1, "body": "x" * (40 * 1024)}, {"id": 2}]}),
        owner="operator",
        environment="sandbox",
    )
    page = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/rows",
        fields=None,
        response_filter=None,
        response_page_size=20,
        cursor=None,
    )
    assert page["response"][0]["id"] == 1
    assert page["responseMeta"]["returnedCount"] == 1
    assert page["responseMeta"]["truncationReason"] == "item_size_limit"
    assert page["responseMeta"]["nextCursor"]


def test_artifact_query_handles_optional_provider_fields(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({"rows": [{"id": 1, "optional": "present"}, {"id": 2}]}),
        owner="operator",
        environment="sandbox",
    )
    page = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/rows",
        fields=["/id", "/optional"],
        response_filter=[{"path": "/optional", "op": "exists", "value": False}],
        response_page_size=20,
        cursor=None,
    )
    # A pointer that does not resolve is OMITTED, not nulled: `/optional: None` was
    # indistinguishable from a stored null, so a page of them read as "unset everywhere"
    # when the pointer was simply wrong. fields_resolved makes the miss visible instead.
    assert page["response"] == [{"/id": 2}]
    assert page["responseMeta"]["fieldsResolved"] == {"/id": 1, "/optional": 0}
    assert "/optional" in page["responseMeta"]["warning"]
    assert page["responseMeta"]["truncated"] is False


def test_artifact_query_accepts_plain_fields_and_reports_remaining_count(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        _envelope({"tasks": [{"id": index, "status": {"status": "lead"}} for index in range(17)]}),
        owner="operator",
        environment="sandbox",
    )
    page = store.query(
        artifact["id"],
        owner="operator",
        environment="sandbox",
        response_path="/response/tasks",
        fields=["id", "status"],
        response_filter=None,
        response_page_size=3,
        cursor=None,
    )
    assert page["response"] == [
        {"id": 0, "status": {"status": "lead"}},
        {"id": 1, "status": {"status": "lead"}},
        {"id": 2, "status": {"status": "lead"}},
    ]
    assert page["responseMeta"]["complete"] is False
    assert page["responseMeta"]["returnedCount"] == 3
    assert page["responseMeta"]["totalCount"] == 17
    assert page["responseMeta"]["remainingCount"] == 14
    assert page["responseMeta"]["nextCursor"]


def test_filter_rejects_unbounded_or_invalid_predicates(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope([{"kind": "task", "id": 1}, {"kind": "note", "id": 2}]),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        response_filter=[{"path": "/kind", "op": "eq", "value": "task"}],
    )
    assert result["response"] == [{"kind": "task", "id": 1}]
    with pytest.raises(ValueError, match="at most 10"):
        bound_proxy_response(
            _envelope([]),
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            response_filter=[{"path": "/id", "value": 1}] * 11,
        )


def test_filter_requires_path_and_rejects_legacy_field_key(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    envelope = _envelope([{"subject": "Toronto piano"}])

    with pytest.raises(ValueError, match="requires path"):
        bound_proxy_response(
            envelope,
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            response_filter=[{"op": "contains", "value": "Toronto"}],
        )

    with pytest.raises(ValueError, match="unsupported keys: field"):
        bound_proxy_response(
            envelope,
            owner="operator",
            environment="sandbox",
            cursor_key="cursor-key",
            store=store,
            response_filter=[{"field": "subject", "op": "contains", "value": "Toronto"}],
        )


def test_filter_icontains_is_case_insensitive(tmp_path) -> None:
    store = ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 1024 * 1024)
    result = bound_proxy_response(
        _envelope([{"subject": "Example Project"}, {"subject": "Storage quote"}]),
        owner="operator",
        environment="sandbox",
        cursor_key="cursor-key",
        store=store,
        response_filter=[{"path": "/subject", "op": "icontains", "value": "pRoJeCt"}],
    )

    assert result["response"] == [{"subject": "Example Project"}]


def _store(tmp_path):
    return ArtifactStore(str(tmp_path), "/downloads", "cursor-key", 3600, 8 * 1024 * 1024)


# Provider-relative pointers are a generic usability hazard when callers omit /response.
# Shape descriptions and primary paths make that mistake self-correcting rather than a guess.
_SAMPLE_RECORD = {
    "id": "sample-record",
    "name": "Example Person",
    "custom_fields": [
        {"id": "cf1", "name": "Service date", "value": "2026-09-12"},
        {"id": "cf2", "name": "Origin", "value": "Example City"},
    ],
    "status": {"status": "new", "color": "#f00"},
}


def test_primary_paths_are_real_pointers_from_the_first_response(tmp_path) -> None:
    """The caller sees usable pointers on the minting response, not just a rule about them."""
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")

    assert artifact["responseRoot"] == "/response"
    assert "/response" in artifact["primaryPaths"]
    # Arrays first: those are what a caller pages.
    assert artifact["primaryPaths"][1] == "/response/custom_fields"
    assert "/response/status" in artifact["primaryPaths"]
    # The bare provider key must never be advertised, since that is the failing form.
    assert "/custom_fields" not in artifact["primaryPaths"]


def test_every_primary_path_resolves(tmp_path) -> None:
    """A pointer we advertise must not raise; that would be worse than advertising none."""
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")
    for pointer in artifact["primaryPaths"]:
        result = store.query(
            artifact["id"], owner="operator", environment="sandbox",
            response_path=pointer, fields=None, response_filter=None,
            response_page_size=20, cursor=None,
        )
        assert result["responsePath"] == pointer


def test_describe_returns_shape_not_values(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response", fields=None, response_filter=None,
        response_page_size=20, cursor=None, describe=True,
    )

    shape = result["shape"]
    assert result["responseMeta"]["contractVersion"] == 2
    assert "artifact" not in result["responseMeta"]
    assert shape["type"] == "object"
    assert shape["keyCount"] == 4
    names = {key["name"]: key for key in shape["keys"]}
    assert names["custom_fields"]["type"] == "array"
    assert names["custom_fields"]["length"] == 2
    assert names["custom_fields"]["pointer"] == "/response/custom_fields"
    assert names["status"]["type"] == "object"
    # Values must not leak into a shape description.
    assert "Example Person" not in json.dumps(result)
    assert "2026-09-12" not in json.dumps(result)


def test_describe_of_an_array_shows_length_and_item_shape(tmp_path) -> None:
    """Enough to page an array without first reading an item out of it."""
    store = _store(tmp_path)
    artifact = store.write(
        _envelope({"tasks": [{"id": i, "name": f"task {i}"} for i in range(37)]}),
        owner="operator", environment="sandbox",
    )
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response/tasks", fields=None, response_filter=None,
        response_page_size=20, cursor=None, describe=True,
    )
    shape = result["shape"]
    assert (shape["type"], shape["length"]) == ("array", 37)
    assert {k["name"] for k in shape["itemKeys"]} == {"id", "name"}
    assert {k["pointer"] for k in shape["itemKeys"]} == {"/id", "/name"}
    assert result["responseMeta"]["totalCount"] == 2


def test_describe_of_the_root_names_the_response_wrapper(tmp_path) -> None:
    """Describing the root is how a caller discovers that data hides under /response."""
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="", fields=None, response_filter=None,
        response_page_size=20, cursor=None, describe=True,
    )
    pointers = {key["pointer"] for key in result["shape"]["keys"]}
    assert "/response" in pointers


def test_describe_refuses_to_be_combined(tmp_path) -> None:
    """Silently ignoring these would present an unfiltered shape as if it were filtered."""
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")
    for kwargs in (
        {"fields": ["/id"]},
        {"response_filter": [{"path": "/id", "value": 1}]},
        {"object_mode": "entries"},
    ):
        base = {
            "fields": None, "response_filter": None, "response_page_size": 20,
            "cursor": None, "object_mode": None,
        }
        base.update(kwargs)
        with pytest.raises(ValueError, match="cannot be combined"):
            store.query(
                artifact["id"], owner="operator", environment="sandbox",
                response_path="/response", describe=True, **base,
            )


def test_describe_stays_small_on_a_wide_object(tmp_path) -> None:
    """A shape must not become the payload it exists to avoid loading."""
    store = _store(tmp_path)
    wide = {f"field_{i}": "v" * 500 for i in range(400)}
    artifact = store.write(_envelope(wide), owner="operator", environment="sandbox")
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response", fields=None, response_filter=None,
        response_page_size=20, cursor=None, describe=True,
    )
    assert result["shape"]["keyCount"] == 400
    assert len(result["shape"]["keys"]) == 20
    assert result["responseMeta"]["nextCursor"]
    assert result["responseMeta"]["serializedBytes"] < 8000


def test_describe_pages_wide_objects_with_absolute_escaped_pointers(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(
        _envelope({"a/b": 1, "c~d": 2, "third": 3}),
        owner="operator", environment="sandbox",
    )
    first = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response", fields=None, response_filter=None,
        response_page_size=2, cursor=None, describe=True,
    )
    assert [key["pointer"] for key in first["shape"]["keys"]] == [
        "/response/a~1b", "/response/c~0d",
    ]
    second = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response", fields=None, response_filter=None,
        response_page_size=2, cursor=first["responseMeta"]["nextCursor"], describe=True,
    )
    assert [key["pointer"] for key in second["shape"]["keys"]] == ["/response/third"]
    assert second["responseMeta"]["complete"] is True


def test_object_entries_support_generic_key_filters(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope({
        "first": {"status": "open"},
        "second": {"status": "closed"},
        "third": {"status": "open"},
    }), owner="operator", environment="sandbox")
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/response", fields=["key", "/value/status"],
        response_filter=[{"path": "/key", "op": "in", "value": ["first", "third"]}],
        response_page_size=20, cursor=None, object_mode="entries",
    )
    assert result["response"] == [
        {"key": "first", "/value/status": "open"},
        {"key": "third", "/value/status": "open"},
    ]
    assert result["responseMeta"]["contractVersion"] == 2
    assert "artifact" not in result["responseMeta"]


def test_compact_threshold_avoids_pretty_print_artifacts_and_caps_previews(tmp_path) -> None:
    store = _store(tmp_path)
    inline = _envelope([
        {"id": index, "label": f"row-{index}", "active": True}
        for index in range(440)
    ])
    assert len(serialized_bytes(inline)) < INLINE_BUDGET_BYTES
    assert len(json.dumps(inline, ensure_ascii=False, indent=2).encode()) > INLINE_BUDGET_BYTES
    result = bound_proxy_response(
        inline, owner="operator", environment="sandbox", cursor_key="cursor-key", store=store,
    )
    assert result["responseMeta"]["artifact"] is None

    oversized = _envelope([{"id": index, "body": "x" * 1000} for index in range(80)])
    bounded = bound_proxy_response(
        oversized, owner="operator", environment="sandbox", cursor_key="cursor-key", store=store,
    )
    assert bounded["responseMeta"]["artifact"] is not None
    assert len(serialized_bytes(bounded)) <= 8 * 1024


def test_provider_relative_pointer_resolves_to_canonical_path(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope(_SAMPLE_RECORD), owner="operator", environment="sandbox")
    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/custom_fields", fields=None, response_filter=None,
        response_page_size=20, cursor=None,
    )

    assert result["responsePath"] == "/response/custom_fields"
    assert result["responseMeta"]["inferredResponsePath"] == "/response/custom_fields"


def test_missing_pointer_reports_attempted_canonical_path(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope({"items": []}), owner="operator", environment="sandbox")

    with pytest.raises(ValueError, match=r'INVALID_RESPONSE_PATH: "/notPresent"') as raised:
        store.query(
            artifact["id"], owner="operator", environment="sandbox",
            response_path="/notPresent", fields=None, response_filter=None,
            response_page_size=20, cursor=None,
        )

    assert 'tried "/response/notPresent"' in str(raised.value)
    assert "Available children:" in str(raised.value)


def test_relative_and_canonical_paths_share_cursor_view(tmp_path) -> None:
    store = _store(tmp_path)
    envelope = _envelope({"items": [{"id": index} for index in range(3)]})
    first = bound_proxy_response(
        envelope, owner="operator", environment="sandbox", cursor_key="cursor-key",
        store=store, response_path="/items", fields=["id"], response_page_size=1,
    )
    second = bound_proxy_response(
        envelope, owner="operator", environment="sandbox", cursor_key="cursor-key",
        store=store, response_path="/response/items", fields=["id"], response_page_size=1,
        response_cursor=first["responseMeta"]["nextCursor"],
    )

    assert first["response"] == [{"id": 0}]
    assert second["response"] == [{"id": 1}]


def test_exact_document_pointer_wins_and_warns_only_for_envelope_collisions(tmp_path) -> None:
    store = _store(tmp_path)
    envelope = {
        **_envelope({"items": [{"id": "provider"}], "status": "provider-status"}),
        "items": [{"id": "document"}],
    }

    exact = bound_proxy_response(
        envelope, owner="operator", environment="sandbox", cursor_key="cursor-key",
        store=store, response_path="/items", fields=["id"],
    )
    collision = bound_proxy_response(
        envelope, owner="operator", environment="sandbox", cursor_key="cursor-key",
        store=store, response_path="/status",
    )
    provider = bound_proxy_response(
        envelope, owner="operator", environment="sandbox", cursor_key="cursor-key",
        store=store, response_path="/response/status",
    )

    assert exact["response"] == [{"id": "document"}]
    assert collision["response"] == 200
    assert 'provider data also exists at "/response/status"' in collision["responseMeta"]["warning"]
    assert "provider-status" not in collision["responseMeta"]["warning"]
    assert provider["response"] == "provider-status"
    assert provider["responseMeta"].get("warning") is None


def test_redundant_collection_prefix_is_normalized_only_when_unambiguous(tmp_path) -> None:
    result = bound_proxy_response(
        _envelope({"items": [{"id": "item-1", "name": "Example"}]}),
        owner="operator", environment="sandbox", cursor_key="cursor-key", store=_store(tmp_path),
        fields=["/items/id", "/items/name"],
    )

    assert result["response"] == [{"/items/id": "item-1", "/items/name": "Example"}]
    assert result["responseMeta"]["inferredResponsePath"] == "/response/items"
    assert "interpreted fields relative" in result["responseMeta"]["warning"]

    mixed = bound_proxy_response(
        _envelope({"items": [{"id": "item-1", "name": "Example"}]}),
        owner="operator", environment="sandbox", cursor_key="cursor-key", store=_store(tmp_path),
        fields=["/items/id", "/name"],
    )
    assert mixed["response"] == [{"/name": "Example"}]
    assert mixed["responseMeta"]["fieldsResolved"]["/items/id"] == 0
    assert "interpreted fields relative" not in mixed["responseMeta"]["warning"]


def test_omitted_response_path_uses_artifact_response_root(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope([{"id": 1}]), owner="operator", environment="sandbox")

    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path=None, fields=None, response_filter=None,
        response_page_size=20, cursor=None,
    )

    assert artifact["responseRoot"] == "/response"
    assert result["responsePath"] == "/response"
    assert result["response"] == [{"id": 1}]


def test_slash_pointer_selects_empty_key_property_not_document_root(tmp_path) -> None:
    store = _store(tmp_path)
    document = {"": {"selected": True}, "response": {"selected": False}}
    artifact = store.write(document, owner="operator", environment="sandbox")

    result = store.query(
        artifact["id"], owner="operator", environment="sandbox",
        response_path="/", fields=None, response_filter=None,
        response_page_size=20, cursor=None,
    )

    assert result["response"] == {"selected": True}


def test_artifact_descriptor_uses_v2_and_iso_expiry(tmp_path) -> None:
    artifact = _store(tmp_path).write(_envelope([]), owner="operator", environment="sandbox")

    assert _store(tmp_path).describe(artifact["id"], owner="operator", environment="sandbox")["contractVersion"] == 2
    assert artifact["expiresAt"].endswith("Z")
    assert "expires_at" not in artifact
    assert artifact["queryTool"] == "query_response_artifact"


def test_v1_artifact_and_cursor_require_reminting(tmp_path) -> None:
    store = _store(tmp_path)
    artifact = store.write(_envelope([{"id": 1}, {"id": 2}]), owner="operator", environment="sandbox")
    _, meta_path = store._paths(artifact["id"])
    metadata = json.loads(meta_path.read_text("utf-8"))
    metadata["contractVersion"] = 1
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="mint a v2 artifact"):
        store.describe(artifact["id"], owner="operator", environment="sandbox")

    fresh = bound_proxy_response(
        _envelope([{"id": 1}, {"id": 2}]),
        owner="operator", environment="sandbox", cursor_key="cursor-key", store=store,
        response_page_size=1,
    )
    token = fresh["responseMeta"]["nextCursor"]
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    state = json.loads(raw[:-32])
    state["contractVersion"] = 1
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(b"cursor-key", payload, hashlib.sha256).digest()
    legacy = base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    with pytest.raises(ValueError, match="mint a v2 cursor"):
        bound_proxy_response(
            _envelope([{"id": 1}, {"id": 2}]),
            owner="operator", environment="sandbox", cursor_key="cursor-key", store=store,
            response_page_size=1, response_cursor=legacy,
        )
