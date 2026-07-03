"""Tests for tlumi.resolve: unified resource resolution."""

from __future__ import annotations

import pytest
from pulumi.automation import Stack

from tlumi.errors import WorkspaceError
from tlumi.resolve import display_from_urn, name_from_urn, resolve_resource, type_from_urn

_RESOURCES = [
    {"urn": "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket", "type": "aws:s3:BucketV2"},
    {"urn": "urn:pulumi:default::proj::aws:ec2:Instance::web-server", "type": "aws:ec2:Instance"},
    {"urn": "urn:pulumi:default::proj::aws:rds:Instance::web-server", "type": "aws:rds:Instance"},
]


# ---------------------------------------------------------------------------
# name_from_urn
# ---------------------------------------------------------------------------


def test_name_from_urn_standard():
    urn = "urn:pulumi:default::myproject::aws:s3:BucketV2::my-bucket"
    assert name_from_urn(urn) == "my-bucket"


def test_name_from_urn_nested_type():
    urn = "urn:pulumi:default::myproject::aws:s3:BucketV2$aws:s3:BucketPolicy::my-policy"
    assert name_from_urn(urn) == "my-policy"


def test_name_from_urn_empty():
    assert name_from_urn("") == ""


def test_name_from_urn_no_separators():
    assert name_from_urn("just-a-name") == "just-a-name"


# ---------------------------------------------------------------------------
# type_from_urn
# ---------------------------------------------------------------------------


def test_type_from_urn_standard():
    urn = "urn:pulumi:default::myproject::aws:s3:BucketV2::my-bucket"
    assert type_from_urn(urn) == "aws:s3:BucketV2"


def test_type_from_urn_short():
    assert type_from_urn("name-only") == "name-only"


# ---------------------------------------------------------------------------
# display_from_urn
# ---------------------------------------------------------------------------


def test_display_from_urn_standard():
    urn = "urn:pulumi:default::myproject::aws:s3:BucketV2::my-bucket"
    assert display_from_urn(urn) == "aws:s3:BucketV2 (my-bucket)"


def test_display_from_urn_short():
    assert display_from_urn("name-only") == "name-only"


# ---------------------------------------------------------------------------
# resolve_resource
# ---------------------------------------------------------------------------


def test_resolve_by_full_urn():
    found = resolve_resource(_RESOURCES, "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket")
    assert found["type"] == "aws:s3:BucketV2"


def test_resolve_by_type_name():
    found = resolve_resource(_RESOURCES, "aws:s3:BucketV2::my-bucket")
    assert found["type"] == "aws:s3:BucketV2"


def test_resolve_by_name_only_unique():
    found = resolve_resource(_RESOURCES, "my-bucket")
    assert found["type"] == "aws:s3:BucketV2"


def test_resolve_not_found():
    with pytest.raises(WorkspaceError, match="Resource not found"):
        resolve_resource(_RESOURCES, "nonexistent")


def test_resolve_type_name_not_found():
    with pytest.raises(WorkspaceError, match="Resource not found"):
        resolve_resource(_RESOURCES, "aws:lambda:Function::missing")


def test_resolve_name_ambiguous():
    with pytest.raises(WorkspaceError, match="Ambiguous name") as exc_info:
        resolve_resource(_RESOURCES, "web-server")
    msg = exc_info.value.message
    assert "aws:ec2:Instance::web-server" in msg
    assert "aws:rds:Instance::web-server" in msg
    assert exc_info.value.hint == "Use type::name format to disambiguate."


def test_resolve_type_name_child_resource():
    """type::name resolves child resources using canonical type from state."""
    resources = [
        {
            "urn": "urn:pulumi:default::proj::pkg:index:Parent$aws:s3:Bucket::my-bucket",
            "type": "aws:s3:Bucket",
        },
    ]
    found = resolve_resource(resources, "aws:s3:Bucket::my-bucket")
    assert found["type"] == "aws:s3:Bucket"


def test_resolve_type_name_child_resource_urn_type_also_works():
    """type::name with full URN-derived type still works."""
    resources = [
        {
            "urn": "urn:pulumi:default::proj::pkg:index:Parent$aws:s3:Bucket::my-bucket",
            "type": "aws:s3:Bucket",
        },
    ]
    found = resolve_resource(resources, "pkg:index:Parent$aws:s3:Bucket::my-bucket")
    assert found["type"] == "aws:s3:Bucket"


def test_resolve_type_name_child_resource_ambiguous():
    """type::name raises ambiguity error when multiple child resources match."""
    resources = [
        {
            "urn": "urn:pulumi:default::proj::pkg:index:ParentA$aws:s3:Bucket::shared",
            "type": "aws:s3:Bucket",
        },
        {
            "urn": "urn:pulumi:default::proj::pkg:index:ParentB$aws:s3:Bucket::shared",
            "type": "aws:s3:Bucket",
        },
    ]
    with pytest.raises(WorkspaceError, match="Ambiguous"):
        resolve_resource(resources, "aws:s3:Bucket::shared")


def test_resolve_empty_list():
    with pytest.raises(WorkspaceError, match="Resource not found"):
        resolve_resource([], "anything")


# ---------------------------------------------------------------------------
# Names containing "::" (Pulumi permits them; parse type::name at the FIRST ::)
# ---------------------------------------------------------------------------

_DC_RESOURCES = [
    {
        "urn": "urn:pulumi:default::proj::aws:s3:Bucket::web::primary",
        "type": "aws:s3:Bucket",
    },
]


def test_resolve_name_containing_double_colon():
    """A resource whose name contains '::' resolves by its bare name."""
    found = resolve_resource(_DC_RESOURCES, "web::primary")
    assert found["urn"] == "urn:pulumi:default::proj::aws:s3:Bucket::web::primary"


def test_resolve_type_name_with_double_colon_name():
    """type::name resolves when the name segment itself contains '::'."""
    found = resolve_resource(_DC_RESOURCES, "aws:s3:Bucket::web::primary")
    assert found["urn"] == "urn:pulumi:default::proj::aws:s3:Bucket::web::primary"


def test_resolve_full_urn_with_double_colon_name():
    """Full-URN lookup still works for names containing '::'."""
    urn = "urn:pulumi:default::proj::aws:s3:Bucket::web::primary"
    assert resolve_resource(_DC_RESOURCES, urn)["urn"] == urn


def test_resolve_double_colon_ambiguous_across_interpretations():
    """An identifier matching both a type::name and a literal name is ambiguous."""
    resources = [
        # Interpretation 1: type "aws:s3:Bucket", name "shared"
        {"urn": "urn:pulumi:default::proj::aws:s3:Bucket::shared", "type": "aws:s3:Bucket"},
        # Interpretation 2: a resource literally named "aws:s3:Bucket::shared"
        {
            "urn": "urn:pulumi:default::proj::random:index:Pet::aws:s3:Bucket::shared",
            "type": "random:index:Pet",
        },
    ]
    with pytest.raises(WorkspaceError, match="Ambiguous") as exc_info:
        resolve_resource(resources, "aws:s3:Bucket::shared")
    assert exc_info.value.hint == "Use the full URN to disambiguate."


def test_resolve_type_name_double_colon_not_found():
    with pytest.raises(WorkspaceError, match="Resource not found"):
        resolve_resource(_DC_RESOURCES, "aws:s3:Bucket::web::missing")


# ---------------------------------------------------------------------------
# F7: duplicate-URN entries during a create-before-delete replacement
# ---------------------------------------------------------------------------

_DUP_URN = "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket"


def test_resolve_duplicate_urn_prefers_live_by_name():
    """A create-before-delete replacement leaves two same-URN entries; name-only
    resolution returns the live one instead of an unsatisfiable ambiguity error."""
    resources = [
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "old", "delete": True},
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "live"},
    ]
    assert resolve_resource(resources, "my-bucket")["id"] == "live"


def test_resolve_duplicate_urn_prefers_live_by_full_urn():
    """Exact-URN resolution prefers the live entry over the condemned old copy."""
    resources = [
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "old", "delete": True},
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "live"},
    ]
    assert resolve_resource(resources, _DUP_URN)["id"] == "live"


def test_resolve_duplicate_urn_pending_replacement_via_type_name():
    """pendingReplacement entries are condemned; type::name resolves the live one
    regardless of list order."""
    resources = [
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "live"},
        {"urn": _DUP_URN, "type": "aws:s3:BucketV2", "id": "old", "pendingReplacement": True},
    ]
    assert resolve_resource(resources, "aws:s3:BucketV2::my-bucket")["id"] == "live"


def test_resolve_distinct_urns_still_ambiguous():
    """Dedup must NOT collapse genuinely distinct resources that share a name."""
    with pytest.raises(WorkspaceError, match="Ambiguous name"):
        resolve_resource(_RESOURCES, "web-server")


# ---------------------------------------------------------------------------
# resolve_targets: empty URN
# ---------------------------------------------------------------------------


def test_resolve_targets_empty_urn_raises():
    """resolve_targets raises WorkspaceError when matched resource has no URN."""
    from unittest.mock import patch

    from tlumi.resolve import resolve_targets

    # Mock resolve_resource to return a resource missing the URN field
    with patch("tlumi.resolve.resolve_resource", return_value={"type": "aws:s3:BucketV2"}):
        with pytest.raises(WorkspaceError, match="has no URN in state"):
            resolve_targets(None, ["my-bucket"], resources=[])


def test_resolve_targets_with_valid_resources():
    """resolve_targets resolves name-only identifiers to full URNs."""
    from tlumi.resolve import resolve_targets

    resources = [
        {"urn": "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket", "type": "aws:s3:BucketV2"},
    ]
    result = resolve_targets(None, ["my-bucket"], resources=resources)
    assert result == ["urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket"]


# ---------------------------------------------------------------------------
# R7-2: resolve_targets state-loading path (resources=None)
# ---------------------------------------------------------------------------


def test_resolve_targets_loads_state_when_resources_none():
    """resolve_targets calls safe_export_stack when resources is None."""
    from unittest.mock import MagicMock, patch

    from tlumi.resolve import resolve_targets

    mock_stack = MagicMock(spec=Stack)
    mock_state = MagicMock()
    mock_state.deployment = {
        "resources": [
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
            {"type": "aws:s3:BucketV2", "urn": "urn:pulumi:default::p::aws:s3:BucketV2::my-bucket"},
        ]
    }

    with patch("tlumi.resolve.safe_export_stack", return_value=mock_state) as mock_export:
        result = resolve_targets(mock_stack, ["my-bucket"])

    mock_export.assert_called_once_with(mock_stack)
    assert result == ["urn:pulumi:default::p::aws:s3:BucketV2::my-bucket"]


def test_resolve_targets_empty_deployment():
    """resolve_targets with None deployment results in empty resource list."""
    from unittest.mock import MagicMock, patch

    from tlumi.resolve import resolve_targets

    mock_stack = MagicMock(spec=Stack)
    mock_state = MagicMock()
    mock_state.deployment = None

    with patch("tlumi.resolve.safe_export_stack", return_value=mock_state):
        with pytest.raises(WorkspaceError, match="Resource not found"):
            resolve_targets(mock_stack, ["my-bucket"])


def test_resolve_targets_full_urn_skips_resolution():
    """Full URN identifiers bypass state loading entirely."""
    from tlumi.resolve import resolve_targets

    full_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    # No stack needed since full URNs skip resolution
    result = resolve_targets(None, [full_urn])
    assert result == [full_urn]
