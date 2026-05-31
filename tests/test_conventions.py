import pytest

from nango_mcp.conventions import (
    connection_audit_findings,
    connection_audit_issues,
    convention_metadata,
    convention_tags,
    imported_connection_id,
)


def test_builds_stable_connection_id_and_recommended_tags() -> None:
    connection_id = imported_connection_id("prod", "microsoft-mail", "service@example.com")
    tags = convention_tags(
        "prod",
        "service@example.com",
        "shared_mailbox",
        "mailbox_readwrite",
        organization_id="org_123",
        display_name="Service Mailbox",
    )

    assert connection_id == "prod__microsoft-mail__service-at-example-com"
    assert tags["end_user_id"] == "service@example.com"
    assert tags["end_user_email"] == "service@example.com"
    assert tags["organization_id"] == "org_123"
    assert tags["end_user_display_name"] == "Service Mailbox"
    assert tags["environment"] == "prod"
    assert tags["owner_kind"] == "shared_mailbox"
    assert tags["purpose"] == "mailbox_readwrite"


def test_rejects_unknown_owner_kind_or_purpose() -> None:
    with pytest.raises(ValueError):
        convention_tags("prod", "service", "admin", "mailbox_readwrite")

    with pytest.raises(ValueError):
        convention_tags("prod", "service", "shared_mailbox", "everything")


def test_metadata_uses_configurable_namespace_and_optional_oauth_owner() -> None:
    metadata = convention_metadata(
        "prod",
        "service",
        "shared_mailbox",
        "mailbox_readwrite",
        namespace="my_app",
        oauth_app_owner="operator",
    )

    assert metadata == {
        "my_app": {
            "environment": "prod",
            "principal": "service",
            "owner_kind": "shared_mailbox",
            "purpose": "mailbox_readwrite",
            "oauth_app_owner": "operator",
        }
    }


def test_audit_requires_end_user_id_and_recommends_nango_context() -> None:
    connection = {
        "tags": {
            "environment": "prod",
            "owner_kind": "company_account",
            "purpose": "task_management",
        },
        "metadata": {},
    }

    findings = connection_audit_findings(connection, "prod")

    assert findings["required_issues"] == ["missing_tag:end_user_id"]
    assert "missing_tag:end_user_email" in findings["recommendations"]
    assert "missing_tag:organization_id" in findings["recommendations"]
    assert "missing_metadata:nango_mcp" in findings["recommendations"]


def test_audit_accepts_minimum_recommended_connection() -> None:
    connection = {
        "tags": {
            "end_user_id": "user_123",
            "end_user_email": "user@example.com",
            "organization_id": "org_123",
            "environment": "prod",
            "owner_kind": "company_account",
            "purpose": "task_management",
        },
        "metadata": convention_metadata("prod", "user_123", "company_account", "task_management"),
    }

    assert connection_audit_issues(connection, "prod") == []
    assert connection_audit_findings(connection, "prod")["recommendations"] == []
