"""Permission evaluation engine: orchestrates safety, rules, mode, and defaults."""

from __future__ import annotations

from mini_openharness.permissions.rules import find_matching_rule
from mini_openharness.permissions.safety import check_safety
from mini_openharness.permissions.types import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionRequest,
)


class PermissionEngine:
    def __init__(self, context: PermissionContext) -> None:
        self.context = context

    def authorize(
        self,
        request: PermissionRequest,
    ) -> PermissionDecision:

        # 1. 安全边界：DENY / ASK 直接返回，PASS 才继续
        decision = self._check_safety(request)
        if decision:
            return decision

        # 2. 显式禁止
        decision = self._check_deny_rules(request)
        if decision:
            return decision

        # 3. 显式 ask
        decision = self._check_ask_rules(request)
        if decision:
            return decision

        # 4. 显式 allow
        decision = self._check_allow_rules(request)
        if decision:
            return decision

        # 5. 默认决策
        return self._default_decision(request)
    
    def _check_safety(self, request: PermissionRequest) -> PermissionDecision | None:
        result = check_safety(request, self.context)
        if result.safe:
            if (
                result.behavior == PermissionBehavior.ALLOW
                and find_matching_rule(request, self.context.rules.deny) is None
            ):
                return PermissionDecision(
                    PermissionBehavior.ALLOW,
                    result.reason or "routine command is safe",
                )
            return None
        if result.behavior == PermissionBehavior.ASK:
            if find_matching_rule(request, self.context.rules.deny) is not None:
                return None  # 显式 deny 规则优先于 safety 的 ASK
            return PermissionDecision(result.behavior, result.reason)
        return PermissionDecision(result.behavior, result.reason)

    def _check_deny_rules(self, request: PermissionRequest) -> PermissionDecision | None:
        rule = find_matching_rule(request, self.context.rules.deny)
        if rule is None:
            return None
        return PermissionDecision(
            PermissionBehavior.DENY,
            f"denied by rule tool={rule.tool} pattern={rule.pattern}",
            rule,
        )

    def _check_ask_rules(self, request: PermissionRequest) -> PermissionDecision | None:
        rule = find_matching_rule(request, self.context.rules.ask)
        if rule is None:
            return None
        return PermissionDecision(
            PermissionBehavior.ASK,
            f"ask by rule tool={rule.tool} pattern={rule.pattern}",
            rule,
        )

    def _check_allow_rules(self, request: PermissionRequest) -> PermissionDecision | None:
        rule = find_matching_rule(request, self.context.rules.allow)
        if rule is None:
            return None
        return PermissionDecision(
            PermissionBehavior.ALLOW,
            f"allowed by rule tool={rule.tool} pattern={rule.pattern}",
            rule,
        )

    def _default_decision(self, request: PermissionRequest) -> PermissionDecision:
        if request.effect in {"read", "compute"}:
            return PermissionDecision(
                PermissionBehavior.ALLOW,
                f"{request.effect} tools are allowed by default",
            )
        return PermissionDecision(
            PermissionBehavior.ASK,
            "write/remote/unknown tools default to ask",
        )
