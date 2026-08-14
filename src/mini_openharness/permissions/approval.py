"""Approval flows for ask decisions: human or an independent reviewer agent."""

from __future__ import annotations

import asyncio
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
    async def request(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ApprovalResult:
        raise NotImplementedError


class HumanApprovalHandler(ApprovalHandler):
    """DEFAULT mode: ASK goes to a human callback, fail closed without one."""

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


class AgentApprovalHandler(ApprovalHandler):
    """AUTO_REVIEW mode: an independent reviewer decides approve/reject.

    The reviewer only answers approve/reject for the current ASK; it cannot
    override DENY, change rules, or execute tools. Any failure, timeout, or
    unparseable output fails closed to reject.
    """

    def __init__(self, reviewer: ApprovalCallback, *, timeout: float = 60.0) -> None:
        self.reviewer = reviewer
        self.timeout = timeout

    async def request(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ApprovalResult:
        if decision.behavior != PermissionBehavior.ASK:
            return ApprovalResult(approved=True)
        try:
            approved = await asyncio.wait_for(
                self.reviewer(request, decision),
                timeout=self.timeout,
            )
        except Exception:
            return ApprovalResult(approved=False)
        if not isinstance(approved, bool):
            return ApprovalResult(approved=False)
        return ApprovalResult(approved=approved, remember=False)
