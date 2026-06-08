from __future__ import annotations

import re
from typing import Any


VALID_OWNER_KINDS = {"user", "shared_mailbox", "service_account", "company_account", "app_account"}
VALID_PURPOSES = {"mailbox_readwrite", "task_management", "accounting", "documents", "messaging", "crm_sync"}
SUGGESTED_OAUTH_APP_OWNERS = {"customer", "operator", "third_party", "unknown"}


def principal_slug(principal: str) -> str:
    raw = principal.strip().lower()
    raw = raw.replace("@", "-at-")
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-") or "principal"


def imported_connection_id(environment: str, provider_config_key: str, principal: str) -> str:
    return f"{environment}__{provider_config_key}__{principal_slug(principal)}"


def convention_tags(
    environment: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    *,
    email: str | None = None,
    organization_id: str | None = None,
    display_name: str | None = None,
) -> dict[str, str]:
    owner_kind = owner_kind.strip()
    purpose = purpose.strip()
    if owner_kind not in VALID_OWNER_KINDS:
        raise ValueError(f"owner_kind must be one of {sorted(VALID_OWNER_KINDS)}")
    if purpose not in VALID_PURPOSES:
        raise ValueError(f"purpose must be one of {sorted(VALID_PURPOSES)}")

    principal_value = principal.strip()
    if not principal_value:
        raise ValueError("principal is required")

    tags = {
        "end_user_id": principal_value,
        "environment": environment,
        "owner_kind": owner_kind,
        "purpose": purpose,
    }
    if email:
        tags["end_user_email"] = email.strip()
    elif "@" in principal_value:
        tags["end_user_email"] = principal_value
    if organization_id:
        tags["organization_id"] = organization_id.strip()
    if display_name:
        tags["end_user_display_name"] = display_name.strip()
    return tags


def convention_metadata(
    environment: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    *,
    namespace: str = "nango_mcp",
    oauth_app_owner: str | None = None,
) -> dict[str, Any]:
    if oauth_app_owner and not oauth_app_owner.strip():
        oauth_app_owner = None
    payload: dict[str, Any] = {
        "environment": environment,
        "principal": principal,
        "owner_kind": owner_kind,
        "purpose": purpose,
    }
    if oauth_app_owner:
        payload["oauth_app_owner"] = oauth_app_owner.strip()
    return {namespace: payload}


def connection_audit_findings(
    connection: dict[str, Any],
    environment: str,
    *,
    metadata_namespace: str = "nango_mcp",
) -> dict[str, list[str]]:
    tags = connection.get("tags") or {}
    metadata = connection.get("metadata") or {}
    required_issues: list[str] = []
    recommendations: list[str] = []

    if not tags.get("end_user_id"):
        required_issues.append("missing_tag:end_user_id")
    if tags.get("owner_kind") and tags.get("owner_kind") not in VALID_OWNER_KINDS:
        required_issues.append("invalid_tag:owner_kind")
    if tags.get("purpose") and tags.get("purpose") not in VALID_PURPOSES:
        required_issues.append("invalid_tag:purpose")

    if not tags.get("end_user_email"):
        recommendations.append("missing_tag:end_user_email")
    if not tags.get("organization_id"):
        recommendations.append("missing_tag:organization_id")
    if tags.get("environment") != environment:
        recommendations.append("missing_or_wrong_tag:environment")
    if not isinstance(metadata.get(metadata_namespace), dict):
        recommendations.append(f"missing_metadata:{metadata_namespace}")

    return {"required_issues": required_issues, "recommendations": recommendations}


def connection_audit_issues(connection: dict[str, Any], environment: str) -> list[str]:
    return connection_audit_findings(connection, environment)["required_issues"]
