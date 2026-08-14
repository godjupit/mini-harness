"""Rule-based permission decisions with optional human approval."""

from mini_openharness.permissions.approval import ApprovalHandler, ApprovalResult
from mini_openharness.permissions.engine import PermissionEngine
from mini_openharness.permissions.rules import (
    build_default_rules,
    find_matching_rule,
    load_rules_from_json,
    match_rule,
)
from mini_openharness.permissions.safety import SafetyResult
from mini_openharness.permissions.types import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionRules,
)

__all__ = [
    "ApprovalHandler",
    "ApprovalResult",
    "PermissionBehavior",
    "PermissionContext",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRequest",
    "PermissionRule",
    "PermissionRules",
    "SafetyResult",
    "build_default_rules",
    "find_matching_rule",
    "load_rules_from_json",
    "match_rule",
]
