"""edit_file tool: exact, unique, snapshot-guarded localized replacement."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from mini_openharness.tools.base import (
    ResourceAccess,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    _is_runtime_secret,
    _safe_path,
)


class EditFileTool:
    name = "edit_file"
    description = (
        "Apply a localized replacement to an existing UTF-8 workspace file. Prefer "
        "edit_file when modifying an existing file; do not rewrite the whole file. "
        "Read the relevant section first (read_file with start_line/end_line) so the "
        "runtime can reject stale edits. old_text must match the current file exactly "
        "and uniquely; include enough surrounding context to make it match once."
    )
    read_only = False
    descriptor = ToolDescriptor(effect="write", destructive=True, path_argument="path")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "expected_sha256": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{64}$",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_path = str(arguments["path"])
        try:
            path = _safe_path(context.workspace, raw_path)
        except ValueError as exc:
            return ToolResult.fail(
                str(exc),
                code="invalid_path",
                stage="validate",
            )
        if _is_runtime_secret(context.workspace, path):
            return ToolResult.fail(
                f"Editing protected runtime secret is not allowed: {raw_path}",
                code="protected_file",
                stage="execute",
            )
        if not path.is_file():
            return ToolResult.fail(
                f"File not found: {raw_path}",
                code="file_not_found",
                stage="execute",
            )

        expected_hash = arguments.get("expected_sha256")
        if expected_hash is None and context.file_snapshots is not None:
            snapshot = context.file_snapshots.get(path)
            expected_hash = snapshot.sha256 if snapshot is not None else None
        if expected_hash is None:
            return ToolResult.fail(
                f"Read {raw_path} before editing it, or provide expected_sha256",
                code="file_not_read",
                stage="validate",
            )

        data = await asyncio.to_thread(path.read_bytes)
        current_hash = hashlib.sha256(data).hexdigest()
        if current_hash != str(expected_hash).lower():
            return ToolResult.fail(
                f"File changed since it was read: {raw_path}",
                code="file_changed",
                stage="execute",
                detail={"expected_sha256": expected_hash, "actual_sha256": current_hash},
            )

        content = data.decode("utf-8")
        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])
        occurrences = content.count(old_text)
        if occurrences == 0:
            return ToolResult.fail(
                f"old_text was not found in {raw_path}. Re-read the relevant section "
                "(read_file with start_line/end_line) and retry with exact text "
                "copied from the current file.",
                code="match_not_found",
                stage="execute",
            )
        if occurrences > 1:
            return ToolResult.fail(
                f"old_text appears {occurrences} times in {raw_path}; include more "
                "surrounding context in old_text so it matches exactly once, or "
                "re-read the file and retry.",
                code="ambiguous_match",
                stage="execute",
                detail={"occurrences": occurrences},
            )

        replacements = 1
        updated = content.replace(old_text, new_text, 1)
        updated_data = updated.encode("utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
        try:
            await asyncio.to_thread(
                _atomic_replace_bytes,
                path,
                updated_data,
                mode,
                current_hash,
            )
        except _FileChangedDuringEdit:
            return ToolResult.fail(
                f"File changed while the edit was being prepared: {raw_path}",
                code="file_changed",
                stage="execute",
            )
        except OSError as exc:
            return ToolResult.fail(
                f"Atomic replace failed for {raw_path}: {exc}",
                code="atomic_replace_failed",
                stage="execute",
                detail={"exception_type": type(exc).__name__},
            )

        if context.file_snapshots is not None:
            snapshot = context.file_snapshots.record(path, updated_data)
            updated_hash = snapshot.sha256
        else:
            updated_hash = hashlib.sha256(updated_data).hexdigest()
        return ToolResult(
            f"Replaced {replacements} occurrence(s) in {raw_path}",
            metadata={"replacements": replacements, "sha256": updated_hash},
        )

    def resources(self, arguments: dict[str, Any], context: ToolContext):
        path = _safe_path(context.workspace, str(arguments["path"]))
        return (ResourceAccess(f"fs:{path}", "write"),)


class _FileChangedDuringEdit(RuntimeError):
    pass


def _atomic_replace_bytes(
    path: Path,
    data: bytes,
    mode: int,
    expected_sha256: str,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        latest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if latest_hash != expected_sha256:
            raise _FileChangedDuringEdit
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)
