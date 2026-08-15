"""Permission rule definitions, loading, and matching."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from mini_openharness.permissions.types import (
    PermissionBehavior,
    PermissionRequest,
    PermissionRule,
    PermissionRules,
)


def build_default_rules() -> PermissionRules:
    """Build the built-in rule set for a fresh context."""
    return PermissionRules(
        deny=build_default_deny_rules(),
        ask=build_default_ask_rules(),
        allow=build_default_allow_rules(),
    )


def load_rules_from_json(path: str | Path) -> PermissionRules:
    """Load rules from a JSON config file.

    Accepts the legacy shape: ``{"rules": [{"tool", "path" | "command",
    "action"}]}``. ``path``/``command`` both map to ``pattern``.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    buckets = {"deny": [], "ask": [], "allow": []}
    for raw in payload.get("rules", []):
        action = str(raw["action"])
        behavior = PermissionBehavior(action)
        pattern = str(raw.get("command") or raw.get("path") or "*")
        buckets[action].append(
            PermissionRule(
                behavior=behavior,
                tool=str(raw.get("tool", "*")),
                pattern=pattern,
                source="config",
            )
        )
    return PermissionRules(
        deny=buckets["deny"],
        ask=buckets["ask"],
        allow=buckets["allow"],
    )


def build_default_deny_rules() -> list[PermissionRule]:
    return [
        PermissionRule(
            PermissionBehavior.DENY,
            tool="write_file",
            pattern="secrets/*",
            source="builtin",
        ),
        PermissionRule(
            PermissionBehavior.DENY,
            tool="edit_file",
            pattern="secrets/*",
            source="builtin",
        ),
    ]


def build_default_allow_rules() -> list[PermissionRule]:
    return [
        PermissionRule(
            PermissionBehavior.ALLOW,
            tool="read_file",
            pattern="*",
            source="builtin",
        ),
        # 默认放行全部 shell（破坏性命令仍被 safety 层 DENY，显式 allow 无法覆盖）。
        PermissionRule(
            PermissionBehavior.ALLOW,
            tool="sandbox_shell",
            pattern="*",
            source="builtin",
        ),
    ]


def build_default_ask_rules() -> list[PermissionRule]:
    return [
        PermissionRule(
            PermissionBehavior.ASK,
            tool="sandbox_shell",
            pattern="npm publish*",
            source="builtin",
        ),
    ]


def match_rule(request: PermissionRequest, rule: PermissionRule) -> bool:
    """Return whether ``rule`` matches ``request``."""
    if not fnmatch.fnmatch(request.tool_name, rule.tool):
        return False
    if rule.pattern == "*":
        return True
    target = _rule_target(request)
    return target is not None and fnmatch.fnmatch(target, rule.pattern)


def find_matching_rule(
    request: PermissionRequest,
    rules: list[PermissionRule],
) -> PermissionRule | None:
    """Return the first rule matching ``request``, or ``None``."""
    return next((rule for rule in rules if match_rule(request, rule)), None)


def _rule_target(request: PermissionRequest) -> str | None:
    """Pick the input value a rule pattern compares against."""
    if request.command is not None:
        return request.command
    if request.path is not None:
        return request.path
    for key in ("path", "file_path", "root"):
        value = request.input.get(key)
        if isinstance(value, str):
            return value
    return None
