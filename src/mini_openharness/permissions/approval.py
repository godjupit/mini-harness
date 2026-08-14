"""Human approval flows for ask decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from mini_openharness.permissions.types import (
    PermissionBehavior,
    PermissionDecision,
    PermissionRequest,
)


ApprovalCallback = Callable[[PermissionRequest, PermissionDecision], Awaitable[bool]]


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    remember: bool = False


class ApprovalHandler:
    def __init__(self, callback: ApprovalCallback | None = None) -> None:
        self.callback = callback

    async def request(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ApprovalResult:
        # 非 ASK 决策不需要审批，视为已通过。
        if decision.behavior != PermissionBehavior.ASK:
            return ApprovalResult(approved=True)
        # 没有审批回调时 fail closed，拒绝。
        if self.callback is None:
            return ApprovalResult(approved=False)
        approved = await self.callback(request, decision)
        return ApprovalResult(approved=approved, remember=False)
