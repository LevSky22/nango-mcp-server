from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INLINE_BUDGET_BYTES = 32 * 1024
PREVIEW_BUDGET_BYTES = 8 * 1024
HARD_RESULT_BUDGET_BYTES = 128 * 1024
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
ARTIFACT_QUOTA_BYTES = 1024 * 1024 * 1024
_MISSING = object()
_ARRAY_INDEX = re.compile(r"(?:0|[1-9][0-9]*)")


def serialized_bytes(value: Any) -> bytes:
    """Compact encoding, for storage and hashing."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def emitted_bytes(value: Any) -> int:
    """Size of the compact JSON text returned in the explicit MCP result."""
    return len(serialized_bytes(value))


def _escape_token(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class JsonPointerResolutionError(ValueError):
    def __init__(
        self,
        requested_path: str,
        resolved_path: str,
        failed_token: str,
        selected_value: Any,
    ) -> None:
        super().__init__(f"JSON pointer does not exist: {requested_path}")
        self.requested_path = requested_path
        self.resolved_path = resolved_path
        self.failed_token = failed_token
        self.selected_value = selected_value


def _pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("responsePath and fields must use RFC 6901 JSON pointers")
    current = value
    resolved_tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and _ARRAY_INDEX.fullmatch(token) and int(token) < len(current):
            current = current[int(token)]
        else:
            resolved_path = "/" + "/".join(resolved_tokens) if resolved_tokens else ""
            raise JsonPointerResolutionError(pointer, resolved_path, raw, current)
        resolved_tokens.append(raw)
    return current


def _invalid_response_path(error: JsonPointerResolutionError, response_root: str) -> ValueError:
    location = "the document root" if error.resolved_path == "" else f'"{error.resolved_path}"'
    return ValueError(
        f'INVALID_RESPONSE_PATH: "{error.requested_path}" does not exist. '
        f'{location} resolves to {_type_name(error.selected_value)} and has no '
        f'"{error.failed_token}" child. Available children: '
        f'{_describe_available_keys(error.selected_value)}. Retry without responsePath '
        f'to use responseRoot={json.dumps(response_root)}, or call with describe=true '
        "at a valid parent pointer."
    )


def _field_pointer(field: str) -> str:
    """Accept ergonomic root field names while retaining RFC 6901 pointers."""
    if not isinstance(field, str) or not field:
        raise ValueError("fields entries must be non-empty strings")
    if field.startswith("/"):
        return field
    return "/" + _escape_token(field)


_VALID_OPS = {"eq", "ne", "in", "contains", "icontains", "lt", "lte", "gt", "gte", "exists"}
_COMPARISON_OPS = {"lt", "lte", "gt", "gte"}


def _describe_available_keys(sample: Any) -> str:
    """Human-readable key list, so a wrong pointer is self-correcting."""
    if isinstance(sample, list):
        return f"an array of {len(sample)} items"
    if isinstance(sample, dict):
        keys = list(sample)
        shown = ["/" + _escape_token(key) for key in keys[:30]]
        if not shown:
            return "(no properties)"
        suffix = f", … ({len(keys)} total)" if len(keys) > len(shown) else ""
        return ", ".join(shown) + suffix
    return f"a {type(sample).__name__} value with no properties"


def _single_array_child(value: Any) -> tuple[str, list[Any]] | None:
    """Return an unambiguous one-level collection without knowing its provider schema."""
    if not isinstance(value, dict):
        return None
    arrays = [(str(key), child) for key, child in value.items() if isinstance(child, list)]
    if len(arrays) != 1:
        return None
    key, child = arrays[0]
    return f"/{_escape_token(key)}", child


def _join_response_path(base: str | None, child: str) -> str:
    return child if base in {None, ""} else f"{base}{child}"


_PRIMARY_PATHS_LIMIT = 12


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _size_hint(value: Any) -> dict[str, int]:
    if isinstance(value, list):
        return {"length": len(value)}
    if isinstance(value, dict):
        return {"keyCount": len(value)}
    return {}


def _key_shape(name: str, value: Any, pointer: str) -> dict[str, Any]:
    return {"pointer": pointer, "name": name, "type": _type_name(value), **_size_hint(value)}


def _shape_page(value: Any, *, base: str, offset: int, page_size: int) -> tuple[dict[str, Any], int, int]:
    """Return one pageable, value-free shape page."""
    shape: dict[str, Any] = {"pointer": base, "type": _type_name(value), **_size_hint(value)}
    keys: list[dict[str, Any]] = []
    if isinstance(value, dict):
        entries = list(value.items())
        page = [
            _key_shape(str(name), child, f"{base}/{_escape_token(str(name))}")
            for name, child in entries[offset: offset + page_size]
        ]
        shape["keys"] = page
        shape["note"] = "keys pointers are absolute RFC 6901 pointers from the artifact root"
        return shape, len(entries), len(page)
    elif isinstance(value, list):
        merged: dict[str, dict[str, Any]] = {}
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            for name, child in item.items():
                merged.setdefault(str(name), _key_shape(str(name), child, "/" + _escape_token(str(name))))
        keys = list(merged.values())
        shape["itemType"] = _type_name(value[0]) if value else None
        shape["itemKeys"] = keys[offset: offset + page_size]
        shape["sampledItems"] = min(len(value), 20)
        shape["note"] = "itemKeys pointers are relative to each item"
    return shape, len(keys), len(keys[offset: offset + page_size])


def _primary_paths(document: Any) -> list[str]:
    """Pointers that exist, so a caller never has to guess one.

    Provider payloads bury the interesting collections one level under `/response`, and a
    caller holding a pointer copied out of a shaped inline view has no way to tell that it
    is not a real pointer. Arrays come first because those are what callers page.
    """
    root = document
    prefix = ""
    paths: list[str] = []
    if isinstance(document, dict) and "response" in document:
        paths.append("/response")
        root = document["response"]
        prefix = "/response"
    if isinstance(root, list):
        return paths or [""]
    if isinstance(root, dict):
        arrays = [k for k, v in root.items() if isinstance(v, list)]
        objects = [k for k, v in root.items() if isinstance(v, dict)]
        for key in (*arrays, *objects):
            if len(paths) >= _PRIMARY_PATHS_LIMIT:
                break
            paths.append(f"{prefix}/{_escape_token(key)}")
    return paths


def _evaluate_filter(item: Any, rule: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Returns (pointer_resolved, operands_comparable, matched) for one predicate."""
    if not isinstance(rule, dict):
        raise ValueError("each filters entry must be an object")
    allowed_keys = {"path", "op", "value"}
    unknown_keys = sorted(set(rule) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            "filters entry has unsupported keys: "
            f"{', '.join(unknown_keys)}; allowed keys are path, op, and value"
        )
    if "path" not in rule:
        raise ValueError(
            "each filters entry requires path; use an RFC 6901 pointer "
            "relative to each selected item (an explicit empty path filters the item itself)"
        )
    op = str(rule.get("op", "eq"))
    if op not in _VALID_OPS:
        raise ValueError(
            "filters op must be eq, ne, in, contains, icontains, lt, lte, gt, gte, or exists"
        )
    path = str(rule.get("path", ""))
    try:
        actual = _pointer(item, path)
    except ValueError:
        actual = _MISSING
    expected = rule.get("value")

    if op == "exists":
        should_exist = True if expected is None else expected
        if not isinstance(should_exist, bool):
            raise ValueError("filters exists value must be boolean when provided")
        return True, True, (actual is not _MISSING) == should_exist

    if actual is _MISSING:
        return False, False, False

    if op == "eq":
        return True, True, actual == expected
    if op == "ne":
        return True, True, actual != expected
    if op == "in":
        if not isinstance(expected, list):
            raise ValueError("filters in value must be an array")
        return True, True, actual in expected
    if op == "contains":
        if not isinstance(actual, (str, list, tuple, set, dict)):
            return True, True, False
        try:
            return True, True, expected in actual
        except TypeError:
            # e.g. `"abc" contains 5` — a type mismatch, not a crash.
            return True, True, False
    if op == "icontains":
        if not isinstance(actual, str) or not isinstance(expected, str):
            return True, False, False
        return True, True, expected.casefold() in actual.casefold()

    # lt / lte / gt / gte. Strings compare lexicographically, which is right for
    # ISO-8601 timestamps but meaningless across types, so report a mismatched pair as
    # not-comparable rather than silently failing every item.
    comparable = (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ) or (isinstance(actual, str) and isinstance(expected, str))
    if not comparable:
        return True, False, False
    matched = {
        "lt": actual < expected,
        "lte": actual <= expected,
        "gt": actual > expected,
        "gte": actual >= expected,
    }[op]
    return True, True, matched


def _largest_fitting_page(items: list[Any], offset: int, page_size: int, wrap) -> list[Any]:
    """
    Largest slice starting at `offset` whose wrapped form fits the inline budget.

    Serialized size grows monotonically with the element count, so bisect rather than
    dropping one element at a time — the latter re-serializes the whole candidate page on
    every step (~100 serializations for a full page of large items instead of ~7).
    """
    high = min(page_size, max(len(items) - offset, 0))
    if high == 0:
        return []
    fits = lambda n: emitted_bytes(wrap(items[offset: offset + n])) <= INLINE_BUDGET_BYTES
    if fits(high):
        return items[offset: offset + high]
    low = 0
    while high - low > 1:
        mid = (low + high) // 2
        if fits(mid):
            low = mid
        else:
            high = mid
    return items[offset: offset + low]


def _apply_filters(items: list[Any], filters: list[dict[str, Any]]) -> tuple[list[Any], list[dict[str, Any]]]:
    """
    Filters a list, reporting how often each pointer resolved. A pointer that resolves on
    nothing is a query bug, not an empty result set, and must not read as a complete zero.
    """
    stats = [
        {"path": str(rule.get("path", "")), "op": str(rule.get("op", "eq")), "resolvedOn": 0, "matched": 0}
        for rule in filters
    ]
    comparable_on = [0] * len(filters)
    kept: list[Any] = []

    for item in items:
        keep = True
        # No short-circuit: every predicate is evaluated so the counts stay honest.
        for index, rule in enumerate(filters):
            resolved, comparable, matched = _evaluate_filter(item, rule)
            if resolved:
                stats[index]["resolvedOn"] += 1
            if comparable:
                comparable_on[index] += 1
            if matched:
                stats[index]["matched"] += 1
            else:
                keep = False
        if keep:
            kept.append(item)

    if items:
        unresolved = [stat["path"] for stat in stats if stat["resolvedOn"] == 0]
        if unresolved:
            raise ValueError(
                f"filters path did not resolve on any of the {len(items)} selected items: "
                f"{', '.join(unresolved)}. Each item exposes: {_describe_available_keys(items[0])}. "
                "Filter paths are RFC 6901 pointers relative to each item, not to the response root."
            )
        incomparable = [
            f"{stat['path']} {stat['op']}"
            for index, stat in enumerate(stats)
            if stat["op"] in (_COMPARISON_OPS | {"icontains"}) and comparable_on[index] == 0
        ]
        if incomparable:
            raise ValueError(
                f"filters comparison never had comparable operands: {', '.join(incomparable)}. "
                "Both the stored value and the supplied value must be numbers, or both strings."
            )

    return kept, stats


def _project(item: Any, fields: list[str], resolved: dict[str, int]) -> dict[str, Any]:
    """
    A pointer that does not resolve is OMITTED rather than set to None, so a missing
    field stays distinguishable from a real null value.
    """
    projected: dict[str, Any] = {}
    for field in fields:
        pointer = _field_pointer(field)
        try:
            projected[field] = _pointer(item, pointer)
            resolved[field] = resolved.get(field, 0) + 1
        except ValueError:
            continue
    return projected


def _project_selection(selected: Any, fields: list[str]) -> tuple[Any, dict[str, int]]:
    resolved: dict[str, int] = {field: 0 for field in fields}
    if isinstance(selected, list):
        projected = [_project(item, fields, resolved) for item in selected]
        if selected and not any(resolved.values()):
            raise ValueError(
                f"fields matched no properties on any of the {len(selected)} selected items. "
                f"Each item exposes: {_describe_available_keys(selected[0])}. "
                "Use root names such as 'id' or RFC 6901 pointers such as '/state/name'."
            )
        return projected, resolved
    projected_one = _project(selected, fields, resolved)
    if not any(resolved.values()):
        raise ValueError(
            f"fields matched no properties. The selected value exposes: "
            f"{_describe_available_keys(selected)}. Use root names such as 'id' or RFC 6901 "
            "pointers such as '/state/name'."
        )
    return projected_one, resolved


def _completion_metadata(
    *,
    truncated: bool,
    returned_count: int | None,
    total_count: int | None,
    offset: int = 0,
) -> dict[str, Any]:
    remaining_count = None
    if total_count is not None and returned_count is not None:
        remaining_count = max(total_count - offset - returned_count, 0)
    return {
        "complete": not truncated,
        "remainingCount": remaining_count,
        "warning": (
            None
            if not truncated
            else "INCOMPLETE RESULT: do not treat the visible response as the full result; "
            "request nextCursor only if more matching data is needed, or query the response artifact."
        ),
    }


def _sign(payload: bytes, key: str) -> str:
    state = json.loads(payload)
    if isinstance(state, dict):
        state = {"contractVersion": 1, **state}
        payload = serialized_bytes(state)
    signature = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64url(payload + signature)


def _unsign(token: str, key: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload, supplied = raw[:-32], raw[-32:]
        expected = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError
        if decoded.get("contractVersion") != 1:
            raise ValueError
        return decoded
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid or expired response cursor") from exc


def _compact_summary(
    value: Any,
    depth: int = 0,
    *,
    max_depth: int = 4,
    max_items: int = 3,
    max_keys: int = 20,
    max_chars: int = 1000,
) -> Any:
    """
    Lossy summary for previews. Two properties matter for correctness: a truncated list
    carries an explicit remainder marker, so 3 items are never mistaken for the whole
    collection; and scalars survive at every depth, so a count never turns into the
    string "[nested value omitted]".
    """
    if isinstance(value, dict):
        if depth >= max_depth:
            return {"_omittedObject": len(value)}
        keys = list(value)[:max_keys]
        result: dict[str, Any] = {
            str(key): _compact_summary(
                value[key], depth + 1,
                max_depth=max_depth, max_items=max_items, max_keys=max_keys, max_chars=max_chars,
            )
            for key in keys
        }
        if len(value) > len(keys):
            result["_omittedFields"] = len(value) - len(keys)
        return result
    if isinstance(value, list):
        if depth >= max_depth:
            return {"_omittedArray": len(value)}
        kept: list[Any] = [
            _compact_summary(
                item, depth + 1,
                max_depth=max_depth, max_items=max_items, max_keys=max_keys, max_chars=max_chars,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            kept.append({"_omittedItems": len(value) - max_items})
        return kept
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"… [{len(value)} chars total]"
    return value


_COMPACT_LADDER = (
    {"max_depth": 4, "max_items": 3, "max_keys": 20, "max_chars": 1000},
    {"max_depth": 3, "max_items": 2, "max_keys": 12, "max_chars": 400},
    {"max_depth": 2, "max_items": 1, "max_keys": 8, "max_chars": 200},
    {"max_depth": 1, "max_items": 1, "max_keys": 5, "max_chars": 100},
)


def _compact_to_budget(value: Any, budget: int, wrap) -> Any:
    """
    Compacts until the assembled reply fits `budget`. Plain _compact_summary is not
    size-bounded (20 keys x 3 items x depth 4 x 1000-char strings reaches megabytes),
    which previously let the hard-limit guard raise with no recovery path.
    """
    compacted: Any = None
    for limits in _COMPACT_LADDER:
        compacted = _compact_summary(value, **limits)
        if len(serialized_bytes(wrap(compacted))) <= budget:
            return compacted
    return {
        "_summaryUnavailable": "value is too large to summarize within the inline budget",
        "_nextStep": "narrow responsePath, project fewer fields, or use textSearch for large strings",
    }


# Holds exactly one parsed artifact: callers page through a single artifact, and the store
# object is rebuilt per request so this cannot live on the instance. Without it, every page
# re-read and re-parsed the whole artifact.
_PARSED: dict[str, Any] | None = None
_PARSE_TTL_SECONDS = 300
_LAST_PRUNE = 0.0
_TEXT_SEARCH_MAX_MATCHES = 20
_TEXT_SEARCH_CONTEXT_CHARS = 240


def _search_string_values(
    selected: Any,
    response_path: str,
    text_search: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    query = text_search.get("query") if isinstance(text_search, dict) else None
    if not isinstance(query, str) or not 1 <= len(query) <= 500:
        raise ValueError("textSearch.query must contain between 1 and 500 characters")
    case_sensitive = bool(text_search.get("caseSensitive", False))
    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    truncated = False

    def visit(value: Any, pointer: str) -> None:
        nonlocal truncated
        if truncated:
            return
        if isinstance(value, str):
            haystack = value if case_sensitive else value.casefold()
            start = 0
            while start <= len(haystack):
                offset = haystack.find(needle, start)
                if offset < 0:
                    break
                if len(matches) >= _TEXT_SEARCH_MAX_MATCHES:
                    truncated = True
                    return
                flank = max((_TEXT_SEARCH_CONTEXT_CHARS - len(query)) // 2, 0)
                context_start = max(0, offset - flank)
                context_end = min(len(value), context_start + _TEXT_SEARCH_CONTEXT_CHARS)
                matches.append({
                    "pointer": pointer,
                    "offset": offset,
                    "context": value[context_start:context_end],
                    "contextStart": context_start,
                    "contextEnd": context_end,
                })
                start = offset + max(len(needle), 1)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}")
                if truncated:
                    return
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{pointer}/{_escape_token(str(key))}")
                if truncated:
                    return

    visit(selected, "" if response_path == "" else response_path.rstrip("/"))
    return matches, truncated


def _is_keyed_object(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) < 2:
        return False
    object_values = sum(isinstance(child, dict) for child in value.values())
    return object_values >= len(value) / 2


def _object_mode_required(
    artifact_id: str,
    response_path: str,
    response_page_size: int,
    fields: list[str] | None,
    filters: list[dict[str, Any]] | None,
) -> ValueError:
    suggestion: dict[str, Any] = {
        "artifactId": artifact_id,
        "responsePath": response_path,
        "objectMode": "entries",
        "pageSize": response_page_size,
    }
    if filters:
        suggestion["filters"] = [
            {
                **predicate,
                "path": (
                    "/key" if predicate.get("path") == "/from"
                    else predicate.get("path") if str(predicate.get("path", "")).startswith("/value/") or predicate.get("path") == "/key"
                    else f"/value{predicate.get('path', '')}"
                ),
            }
            for predicate in filters
        ]
    if fields:
        suggestion["fields"] = [
            "key" if field in {"key", "/key"}
            else field if field.startswith("/value/")
            else f"/value{_field_pointer(field)}"
            for field in fields
        ]
    elif filters:
        suggestion["fields"] = ["key", "value"]
    return ValueError(
        f"OBJECT_MODE_REQUIRED: {response_path or '/'} selects a keyed JSON object, not an array. "
        "Query its entries and filter /key or fields beneath /value. "
        f"suggestedRequest={json.dumps(suggestion, ensure_ascii=False, separators=(',', ':'))}"
    )


def _load_document(artifact_id: str, data_path: Path) -> Any:
    global _PARSED
    mtime = data_path.stat().st_mtime
    now = time.time()
    if (
        _PARSED is not None
        and _PARSED["artifactId"] == artifact_id
        and _PARSED["mtime"] == mtime
        and _PARSED["expiresAt"] > now
    ):
        return _PARSED["document"]

    # Drop the previous document before parsing the next so both are never live at once.
    _PARSED = None
    try:
        # read_bytes, not read_text: json.loads decodes internally, so read_text would add
        # a full extra copy of the artifact as a str.
        document = json.loads(data_path.read_bytes())
    except json.JSONDecodeError as exc:
        raise ValueError("response artifact does not contain valid JSON") from exc
    _PARSED = {
        "artifactId": artifact_id,
        "mtime": mtime,
        "expiresAt": now + _PARSE_TTL_SECONDS,
        "document": document,
    }
    return document


class ArtifactStore:
    def __init__(self, root: str, result_root: str, key: str, ttl_seconds: int, max_bytes: int) -> None:
        self.root = Path(root) if root else None
        self.result_root = result_root.rstrip("/")
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes

    @property
    def available(self) -> bool:
        return self.root is not None

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        if self.root is None:
            raise RuntimeError("response artifact storage is not configured")
        return self.root / f"response-{artifact_id}.json", self.root / f"response-{artifact_id}.meta.json"

    def prune(self, *, force: bool = False) -> None:
        global _LAST_PRUNE
        if self.root is None:
            return
        if not force and time.time() - _LAST_PRUNE < 60:
            return
        _LAST_PRUNE = time.time()
        cutoff = time.time() - self.ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        data_entries: list[tuple[Path, os.stat_result]] = []
        for entry in self.root.glob("response-*"):
            try:
                if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                elif entry.is_file() and not entry.is_symlink() and not entry.name.endswith(".meta.json"):
                    data_entries.append((entry, entry.stat()))
            except FileNotFoundError:
                pass
        total = sum(stat.st_size for _, stat in data_entries)
        for entry, stat in sorted(data_entries, key=lambda item: item[1].st_mtime):
            if total <= ARTIFACT_QUOTA_BYTES:
                break
            entry.unlink(missing_ok=True)
            entry.with_name(entry.name.replace(".json", ".meta.json")).unlink(missing_ok=True)
            total -= stat.st_size

    def write(self, value: Any, *, owner: str, environment: str) -> dict[str, Any]:
        content = serialized_bytes(value)
        if len(content) > self.max_bytes:
            raise ValueError(f"provider response exceeds the {self.max_bytes}-byte artifact limit")
        digest = hashlib.sha256(content).hexdigest()
        # A random 256-bit capability prevents content correlation and makes stdio
        # handles safe to treat as unguessable resource identifiers.
        artifact_id = _b64url(secrets.token_bytes(32))
        data_path, meta_path = self._paths(artifact_id)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if not data_path.exists():
            self.prune()
        expires_at_epoch = int(time.time()) + self.ttl_seconds
        expires_at = datetime.fromtimestamp(expires_at_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        metadata = {
            "contractVersion": 1,
            "owner": owner,
            "environment": environment,
            "mediaType": "application/json",
            "byteLength": len(content),
            "sha256": digest,
            "expiresAt": expires_at,
            "responseRoot": "/response",
            "primaryPaths": _primary_paths(value),
            "sourceTruncated": False,
        }
        self._atomic_write(data_path, content)
        self._atomic_write(meta_path, serialized_bytes(metadata))
        result = {
            "id": artifact_id,
            "uri": f"nango-mcp://artifact/{artifact_id}",
            **{key: metadata[key] for key in ("mediaType", "byteLength", "sha256", "expiresAt")},
            "queryTool": "query_response_artifact",
            "responseRoot": metadata["responseRoot"],
            # Emitted here, on the very first response, because this is where a caller
            # decides its next pointer. `responseRoot` alone was already present and was
            # still read past: real pointers are harder to ignore than a rule about them.
            "primaryPaths": metadata["primaryPaths"],
            "sourceTruncated": metadata["sourceTruncated"],
        }
        return result

    def _load(
        self,
        artifact_id: str,
        *,
        owner: str,
        environment: str,
    ) -> tuple[Path, dict[str, Any]]:
        if not artifact_id or not all(char.isalnum() or char in "-_" for char in artifact_id):
            raise ValueError("invalid artifact id")
        data_path, meta_path = self._paths(artifact_id)
        # Distinct outcomes, per the MCP stateful-tools guidance that a call against an
        # expired or unknown handle should say which, so the caller can recover by
        # re-minting instead of guessing. "not found or has expired" told it neither.
        try:
            metadata = json.loads(meta_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError(
                "response artifact handle is unknown: it was never created, or it was pruned. "
                "re-run the tool that produced it to mint a new artifact"
            ) from exc
        if metadata.get("contractVersion") != 1:
            raise ValueError(
                "response artifact uses an unsupported contract version; "
                "re-run the tool that produced it"
            )
        if not data_path.exists():
            # Previously surfaced as an unhandled FileNotFoundError on first read.
            meta_path.unlink(missing_ok=True)
            raise ValueError(
                "response artifact body is missing while its metadata survives; "
                "re-run the tool that produced it to mint a new artifact"
            )
        if metadata.get("owner") != owner or metadata.get("environment") != environment:
            raise PermissionError("response artifact belongs to a different caller or environment")
        try:
            expires_at = datetime.fromisoformat(str(metadata.get("expiresAt", "")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "response artifact metadata has an invalid expiry; re-run the tool that produced it"
            ) from exc
        if expires_at.timestamp() <= time.time():
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise ValueError(
                f"response artifact handle expired at {metadata.get('expiresAt')}; "
                "re-run the tool that produced it to mint a new artifact"
            )
        return data_path, metadata

    def describe(self, artifact_id: str, *, owner: str, environment: str) -> dict[str, Any]:
        """Return bounded metadata only; never parse or expose the stored payload."""
        _, metadata = self._load(artifact_id, owner=owner, environment=environment)
        return {
            "descriptorVersion": 1,
            "contractVersion": 1,
            "id": artifact_id,
            "mediaType": metadata["mediaType"],
            "byteLength": metadata["byteLength"],
            "sha256": metadata["sha256"],
            "expiresAt": metadata["expiresAt"],
            "queryTool": "query_response_artifact",
            "responseRoot": metadata["responseRoot"],
            "primaryPaths": metadata.get("primaryPaths", []),
            "sourceTruncated": bool(metadata.get("sourceTruncated", False)),
            "rawReadable": True,
            "guidance": (
                "Use resources/read for the complete immutable representation, or "
                "query_response_artifact for a bounded structured view."
            ),
        }

    def read(self, artifact_id: str, *, owner: str, environment: str) -> tuple[bytes, dict[str, Any]]:
        data_path, metadata = self._load(artifact_id, owner=owner, environment=environment)
        return data_path.read_bytes(), metadata

    def read_authorized(
        self,
        artifact_id: str,
        *,
        owner: str,
        environments: frozenset[str],
    ) -> tuple[bytes, dict[str, Any]]:
        _, meta_path = self._paths(artifact_id)
        try:
            metadata = json.loads(meta_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError("response artifact handle is unknown or expired") from exc
        environment = str(metadata.get("environment") or "")
        if environment not in environments:
            raise PermissionError("response artifact is outside the caller scope")
        return self.read(artifact_id, owner=owner, environment=environment)

    def delete(self, artifact_id: str, *, owner: str, environment: str) -> bool:
        """Idempotently delete an artifact after enforcing its caller/environment binding."""
        data_path, meta_path = self._paths(artifact_id)
        if not data_path.exists() and not meta_path.exists():
            return False
        self._load(artifact_id, owner=owner, environment=environment)
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        global _PARSED
        if _PARSED and _PARSED.get("artifactId") == artifact_id:
            _PARSED = None
        return True

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.part")
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def query(
        self,
        artifact_id: str,
        *,
        owner: str,
        environment: str,
        response_path: str | None,
        fields: list[str] | None,
        response_filter: list[dict[str, Any]] | None,
        response_page_size: int,
        cursor: str | None,
        describe: bool = False,
        object_mode: str | None = None,
        text_search: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Select, filter, project, and paginate structured JSON inside an artifact.

        With `describe`, return the shape at `response_path` instead of its values.
        """
        if not 1 <= response_page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"pageSize must be between 1 and {MAX_PAGE_SIZE}")
        if describe:
            # Refuse rather than silently ignore: a shape that looked filtered or projected
            # would be read as evidence about the data, which is exactly the wrong lesson.
            for name, supplied in (
                ("fields", fields),
                ("filters", response_filter),
                ("objectMode", object_mode),
                ("textSearch", text_search),
            ):
                if supplied:
                    raise ValueError(
                        "describe returns the shape at responsePath and cannot be combined "
                        f"with {name}; describe first, then query with {name}"
                    )
        filters = response_filter or []
        if len(filters) > 10:
            raise ValueError("filters accepts at most 10 predicates")
        if fields and len(fields) > 50:
            raise ValueError("fields accepts at most 50 JSON pointers")

        data_path, metadata = self._load(
            artifact_id,
            owner=owner,
            environment=environment,
        )
        document = _load_document(artifact_id, data_path)
        response_path = response_path if response_path is not None else str(metadata["responseRoot"])
        try:
            selected = _pointer(document, response_path)
        except JsonPointerResolutionError as error:
            raise _invalid_response_path(error, str(metadata["responseRoot"])) from error
        if text_search is not None:
            if describe or fields or filters or object_mode is not None or cursor:
                raise ValueError(
                    "textSearch cannot be combined with describe, fields, filters, objectMode, or cursor"
                )
            matches, truncated = _search_string_values(selected, response_path, text_search)
            result = {
                "artifactId": artifact_id,
                "responsePath": response_path,
                "response": matches,
                "responseMeta": {
                    "contractVersion": 1,
                    "truncated": truncated,
                    "complete": not truncated,
                    "truncationReason": "match_limit" if truncated else None,
                    "returnedCount": len(matches),
                    "totalCount": None if truncated else len(matches),
                    "remainingCount": None,
                    "nextCursor": None,
                    "serializedBytes": 0,
                    "pageUnit": "items",
                    "warning": (
                        f"More than {_TEXT_SEARCH_MAX_MATCHES} matches exist; narrow responsePath or use a more specific literal."
                        if truncated else None
                    ),
                },
            }
            result["responseMeta"]["serializedBytes"] = emitted_bytes(result)
            return result
        if describe:
            base = "" if response_path == "" else response_path.rstrip("/")
            view_hash = hashlib.sha256(serialized_bytes({
                "contractVersion": 1,
                "artifactId": artifact_id,
                "responsePath": response_path,
                "describe": True,
                "pageSize": response_page_size,
            })).hexdigest()
            offset = 0
            if cursor:
                state = _unsign(cursor, self.key)
                expected = {
                    "artifactId": artifact_id,
                    "owner": owner,
                    "environment": environment,
                    "view_hash": view_hash,
                }
                if any(state.get(key) != value for key, value in expected.items()):
                    raise ValueError("response cursor does not match this artifact shape query")
                offset = int(state.get("offset", 0))
            shape, total_keys, returned_keys = _shape_page(
                selected, base=base, offset=offset, page_size=response_page_size
            )
            if offset > total_keys:
                raise ValueError("response cursor is past the end of the shape")
            next_offset = offset + returned_keys
            next_cursor = None
            if next_offset < total_keys:
                next_cursor = _sign(serialized_bytes({
                    "artifactId": artifact_id,
                    "owner": owner,
                    "environment": environment,
                    "offset": next_offset,
                    "view_hash": view_hash,
                }), self.key)
            result = {
                "artifactId": artifact_id,
                "responsePath": response_path,
                "shape": shape,
                "responseMeta": {
                    "contractVersion": 1,
                    "truncated": next_cursor is not None,
                    "truncationReason": "page_limit" if next_cursor else None,
                    "returnedCount": returned_keys,
                    "totalCount": total_keys,
                    "nextCursor": next_cursor,
                    "serializedBytes": 0,
                    "pageUnit": "entries",
                    **_completion_metadata(
                        truncated=next_cursor is not None,
                        returned_count=returned_keys,
                        total_count=total_keys,
                        offset=offset,
                    ),
                },
            }
            result["responseMeta"]["serializedBytes"] = emitted_bytes(result)
            return result
        if object_mode is not None:
            if object_mode != "entries":
                raise ValueError("objectMode must be entries when provided")
            if not isinstance(selected, dict):
                raise ValueError("objectMode=entries requires responsePath to select a JSON object")
            selected = [{"key": key, "value": value} for key, value in selected.items()]
        filter_stats: list[dict[str, Any]] | None = None
        if filters:
            if not isinstance(selected, list):
                if _is_keyed_object(selected):
                    raise _object_mode_required(
                        artifact_id, response_path, response_page_size, fields, filters
                    )
                raise ValueError("filters require responsePath to select a JSON array")
            selected, filter_stats = _apply_filters(selected, filters)
        fields_resolved: dict[str, int] | None = None
        if fields:
            try:
                selected, fields_resolved = _project_selection(selected, fields)
            except ValueError as error:
                if "fields matched no properties" in str(error) and _is_keyed_object(selected):
                    raise _object_mode_required(
                        artifact_id, response_path, response_page_size, fields, filters
                    ) from error
                raise

        view_hash = hashlib.sha256(serialized_bytes({
            "contractVersion": 1,
            "artifactId": artifact_id,
            "responsePath": response_path,
            "objectMode": object_mode,
            "fields": fields,
            "filters": response_filter,
            "pageSize": response_page_size,
        })).hexdigest()
        offset = 0
        if cursor:
            state = _unsign(cursor, self.key)
            expected = {
                "artifactId": artifact_id,
                "owner": owner,
                "environment": environment,
                "view_hash": view_hash,
            }
            if any(state.get(key) != value for key, value in expected.items()):
                raise ValueError("response cursor does not match this artifact query")
            offset = int(state.get("offset", 0))

        total_count = len(selected) if isinstance(selected, list) else None
        next_cursor = None
        reason = None
        returned_count = total_count
        output = selected
        if isinstance(selected, list):
            page = _largest_fitting_page(selected, offset, response_page_size, lambda p: p)
            if not page and offset < len(selected):
                page = [
                    _compact_to_budget(
                        selected[offset], INLINE_BUDGET_BYTES, lambda value: [value]
                    )
                ]
                reason = "item_size_limit"
            returned_count = len(page)
            output = page
            next_offset = offset + returned_count
            if next_offset < len(selected):
                next_cursor = _sign(serialized_bytes({
                    "artifactId": artifact_id,
                    "owner": owner,
                    "environment": environment,
                    "offset": next_offset,
                    "view_hash": view_hash,
                }), self.key)
                reason = reason or (
                    "page_limit" if returned_count == response_page_size else "size_limit"
                )
        elif emitted_bytes(selected) > INLINE_BUDGET_BYTES:
            output = _compact_to_budget(selected, INLINE_BUDGET_BYTES, lambda value: value)
            reason = "size_limit"

        truncated = bool(reason or next_cursor)
        result = {
            "artifactId": artifact_id,
            "responsePath": response_path,
            "response": output,
            "responseMeta": {
                "contractVersion": 1,
                "truncated": truncated,
                "truncationReason": reason,
                "returnedCount": returned_count,
                "totalCount": total_count,
                "nextCursor": next_cursor,
                "serializedBytes": 0,
                **({"pageUnit": "items"} if isinstance(selected, list) else {}),
                **_completion_metadata(
                    truncated=truncated,
                    returned_count=returned_count,
                    total_count=total_count,
                    offset=offset,
                ),
            },
        }
        if filter_stats is not None:
            result["responseMeta"]["filtersApplied"] = filter_stats
        if fields_resolved is not None:
            result["responseMeta"]["fieldsResolved"] = fields_resolved
            missing = [field for field, count in fields_resolved.items() if count == 0]
            # Only meaningful when something was actually projected: an empty selection
            # makes every count zero, and calling that "never resolved" is a false alarm.
            if missing and any(fields_resolved.values()):
                note = (
                    "fields did not resolve on any selected item and were omitted from every "
                    f"result: {', '.join(missing)}."
                )
                existing = result["responseMeta"].get("warning")
                result["responseMeta"]["warning"] = f"{existing} {note}" if existing else note
        emitted = emitted_bytes(result)
        result["responseMeta"]["serializedBytes"] = emitted
        if emitted > HARD_RESULT_BUDGET_BYTES:
            raise RuntimeError("bounded artifact query unexpectedly exceeded its hard serialized-size limit")
        return result


def bound_proxy_response(
    envelope: dict[str, Any],
    *,
    owner: str,
    environment: str,
    cursor_key: str,
    store: ArtifactStore,
    response_mode: str = "auto",
    response_path: str | None = None,
    fields: list[str] | None = None,
    response_filter: list[dict[str, Any]] | None = None,
    response_page_size: int = DEFAULT_PAGE_SIZE,
    response_cursor: str | None = None,
) -> dict[str, Any]:
    if response_mode not in {"auto", "summary", "full", "artifact"}:
        raise ValueError("responseMode must be auto, summary, full, or artifact")
    if not 1 <= response_page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"pageSize must be between 1 and {MAX_PAGE_SIZE}")
    filters = response_filter or []
    if len(filters) > 10:
        raise ValueError("filters accepts at most 10 predicates")

    # Measured before selection: the artifact stores the unreduced envelope, so later
    # queries are not silently limited to whatever this call happened to project.
    artifact_bytes = serialized_bytes(envelope)

    effective_response_path = response_path if response_path is not None else "/response"
    try:
        selected = _pointer(envelope, effective_response_path)
    except JsonPointerResolutionError as error:
        raise _invalid_response_path(error, "/response") from error
    inferred_response_path: str | None = None
    filter_stats: list[dict[str, Any]] | None = None
    fields_resolved: dict[str, int] | None = None
    if filters:
        if not isinstance(selected, list):
            inferred = _single_array_child(selected)
            if inferred is None:
                raise ValueError("filters require responsePath to select a JSON array")
            child_path, selected = inferred
            inferred_response_path = _join_response_path(effective_response_path, child_path)
        selected, filter_stats = _apply_filters(selected, filters)
    if fields:
        if len(fields) > 50:
            raise ValueError("fields accepts at most 50 JSON pointers")
        try:
            selected, fields_resolved = _project_selection(selected, fields)
        except ValueError as error:
            inferred = _single_array_child(selected)
            if inferred is None or "fields matched no properties" not in str(error):
                raise
            child_path, selected = inferred
            inferred_response_path = _join_response_path(effective_response_path, child_path)
            selected, fields_resolved = _project_selection(selected, fields)

    full_value = {**envelope, "response": selected}
    oversized = len(artifact_bytes) > INLINE_BUDGET_BYTES
    artifact = None
    if oversized or response_mode == "artifact":
        if store.available:
            artifact = store.write(envelope, owner=owner, environment=environment)
        elif response_mode in {"full", "artifact"} or not isinstance(selected, list):
            # A list can still be paged with cursors, but anything else has no recovery
            # path at all without an artifact, so refuse rather than silently truncate.
            raise RuntimeError(
                "response artifact storage is required for this result but is not configured; "
                "set NANGO_MCP_DOWNLOAD_ROOT so oversized responses remain retrievable"
            )

    offset = 0
    view_hash = hashlib.sha256(serialized_bytes({
        "responseMode": response_mode,
        "responsePath": effective_response_path,
        "fields": fields,
        "filters": response_filter,
        "pageSize": response_page_size,
    })).hexdigest()
    if response_cursor:
        state = _unsign(response_cursor, cursor_key)
        if state.get("owner") != owner or state.get("environment") != environment:
            raise PermissionError("response cursor belongs to a different caller or environment")
        if state.get("view_hash") != view_hash:
            raise ValueError("response cursor does not match these response controls")
        offset = int(state.get("offset", 0))

    total_count = len(selected) if isinstance(selected, list) else None
    next_cursor = None
    returned_count = total_count
    output_response: Any = selected
    reason = None
    if isinstance(selected, list):
        page = _largest_fitting_page(
            selected, offset, response_page_size, lambda p: {**envelope, "response": p}
        )
        if not page and offset < len(selected):
            # A single oversized element still has to make forward progress. Without
            # this, next_offset == offset and the caller follows a cursor pointing at
            # the same position forever, receiving an empty list every time.
            page = [
                _compact_to_budget(
                    selected[offset],
                    INLINE_BUDGET_BYTES,
                    lambda value: {**envelope, "response": [value]},
                )
            ]
            reason = "item_size_limit"
        returned_count = len(page)
        output_response = page
        next_offset = offset + returned_count
        if next_offset < len(selected):
            next_cursor = _sign(serialized_bytes({"owner": owner, "environment": environment, "offset": next_offset, "view_hash": view_hash}), cursor_key)
            reason = reason or ("page_limit" if returned_count == response_page_size else "size_limit")
    elif response_mode == "summary" or emitted_bytes(full_value) > INLINE_BUDGET_BYTES:
        output_response = _compact_to_budget(
            selected, INLINE_BUDGET_BYTES, lambda value: {**envelope, "response": value}
        )
        reason = "size_limit"

    if response_mode == "artifact":
        output_response = None
        reason = "artifact_requested"
    truncated = bool(reason or next_cursor)
    result = {
        **envelope,
        "response": output_response,
        "responseMeta": {
            "contractVersion": 1,
            "truncated": truncated,
            "truncationReason": reason,
            "returnedCount": returned_count,
            "totalCount": total_count,
            "nextCursor": next_cursor,
            "serializedBytes": 0,
            **({"pageUnit": "items"} if isinstance(selected, list) else {}),
            "artifact": artifact,
            **_completion_metadata(
                truncated=truncated,
                returned_count=returned_count,
                total_count=total_count,
                offset=offset,
            ),
        },
    }
    if filter_stats is not None:
        result["responseMeta"]["filtersApplied"] = filter_stats
    if inferred_response_path is not None:
        result["responseMeta"]["inferredResponsePath"] = inferred_response_path
    if fields_resolved is not None:
        result["responseMeta"]["fieldsResolved"] = fields_resolved
        missing = [field for field, count in fields_resolved.items() if count == 0]
        if missing:
            note = (
                "fields did not resolve on any selected item and were omitted from every "
                f"result: {', '.join(missing)}."
            )
            existing = result["responseMeta"].get("warning")
            result["responseMeta"]["warning"] = f"{existing} {note}" if existing else note
    if artifact is not None and (response_path is not None or inferred_response_path or filters or fields):
        note = (
            "The stored artifact holds the FULL provider envelope; responsePath, "
            "filters and fields shaped only this inline view, so artifact queries "
            "start from the unreduced response."
        )
        existing = result["responseMeta"].get("warning")
        result["responseMeta"]["warning"] = f"{existing} {note}" if existing else note
    if artifact is not None and emitted_bytes(result) > PREVIEW_BUDGET_BYTES:
        result["response"] = _compact_to_budget(
            result["response"],
            PREVIEW_BUDGET_BYTES,
            lambda value: {**result, "response": value},
        )
    emitted = emitted_bytes(result)
    result["responseMeta"]["serializedBytes"] = emitted
    if emitted > HARD_RESULT_BUDGET_BYTES:
        raise RuntimeError("bounded MCP result unexpectedly exceeded its hard serialized-size limit")
    return result
