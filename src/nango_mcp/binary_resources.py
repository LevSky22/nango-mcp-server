from __future__ import annotations

import base64
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _identifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


class BinaryResourceStore:
    def __init__(self, root: str, ttl_seconds: int) -> None:
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds

    def _paths(self, resource_id: str) -> tuple[Path, Path]:
        if not resource_id or not all(char.isalnum() or char in "-_" for char in resource_id):
            raise ValueError("invalid download resource id")
        return self.root / f"download-{resource_id}.bin", self.root / f"download-{resource_id}.meta.json"

    def ingest(
        self,
        source: Path,
        *,
        owner: str,
        environment: str,
        content_type: str,
        byte_length: int,
        sha256: str,
        suggested_name: str | None = None,
    ) -> dict[str, Any]:
        resource_id = _identifier()
        data_path, meta_path = self._paths(resource_id)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.replace(source, data_path)
        os.chmod(data_path, 0o600)
        expires_epoch = int(time.time()) + self.ttl_seconds
        metadata = {
            "owner": owner,
            "environment": environment,
            "contentType": content_type.split(";", 1)[0].strip().lower() or "application/octet-stream",
            "byteLength": byte_length,
            "sha256": sha256,
            "suggestedName": suggested_name,
            "expiresAt": datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        temporary = meta_path.with_name(f".{meta_path.name}.{secrets.token_hex(4)}.part")
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                output.write(json.dumps(metadata, separators=(",", ":")).encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, meta_path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "id": resource_id,
            "uri": f"nango-mcp://download/{resource_id}",
            **{key: metadata[key] for key in ("contentType", "byteLength", "sha256", "suggestedName", "expiresAt")},
        }

    def read_authorized(
        self,
        resource_id: str,
        *,
        owner: str,
        environments: frozenset[str],
    ) -> tuple[bytes, dict[str, Any]]:
        data_path, meta_path = self._paths(resource_id)
        try:
            metadata = json.loads(meta_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError("download resource is unknown or expired") from exc
        if metadata.get("owner") != owner or metadata.get("environment") not in environments:
            raise PermissionError("download resource is outside the caller scope")
        expiry = datetime.fromisoformat(str(metadata["expiresAt"]).replace("Z", "+00:00")).timestamp()
        if expiry <= time.time() or not data_path.exists():
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise ValueError("download resource has expired")
        return data_path.read_bytes(), metadata
