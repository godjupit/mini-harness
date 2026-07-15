"""Live provider contract matrix runner and report merger.

The live path intentionally uses AgentLoop and real file tools so a passing
result proves streaming, tool-call serialization, tool-result round trips,
multi-turn history, and usage reporting through the production adapters.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mini_openharness import __version__
from mini_openharness.engine import AgentEvent, AgentLoop
from mini_openharness.permissions import PermissionPolicy
from mini_openharness.provider import OpenAICompatibleProvider, OpenAIResponsesProvider
from mini_openharness.tools import ReadFileTool, ToolRegistry, WriteFileTool


SCHEMA_VERSION = 1
CONTRACT_FILE = "provider-contract.txt"
_SECRET_PATTERN = re.compile(
    r"(?i)(?:Bearer\s+)[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{12,}\b"
)


class ContractTurnError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContractConfig:
    case: str
    api_mode: str
    model: str
    base_url: str
    workspace: Path
    timeout_seconds: float = 180.0
    max_retries: int = 2
    max_steps: int = 6

    def __post_init__(self) -> None:
        if self.api_mode not in {"responses", "chat"}:
            raise ValueError("api_mode must be responses or chat")
        if not self.case or not self.model or not self.base_url:
            raise ValueError("case, model, and base_url are required")


async def run_provider_contract(
    config: ContractConfig,
    *,
    api_key: str = "",
    provider: Any | None = None,
) -> dict[str, Any]:
    """Run a deterministic two-turn contract through the production agent loop."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    report = _base_report(config, started_at)
    config.workspace.mkdir(parents=True, exist_ok=True)
    proof_path = config.workspace / CONTRACT_FILE
    proof_path.unlink(missing_ok=True)
    contract_token = "CONTRACT_" + re.sub(r"[^A-Za-z0-9]+", "_", config.case).upper()

    if provider is None:
        if not api_key:
            raise ValueError("api_key is required for a live provider contract")
        provider_class = (
            OpenAIResponsesProvider
            if config.api_mode == "responses"
            else OpenAICompatibleProvider
        )
        provider = provider_class(
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    tools = ToolRegistry()
    tools.register(ReadFileTool())
    tools.register(WriteFileTool())
    loop = AgentLoop(
        provider=provider,
        tools=tools,
        workspace=config.workspace,
        system_prompt=(
            "You are running a deterministic provider contract. Follow the requested "
            "tool names, arguments, and final marker exactly; do not call extra tools."
        ),
        max_steps=config.max_steps,
        permission_policy=PermissionPolicy(default_mutation="allow"),
    )

    try:
        turn1 = await _run_turn(
            loop,
            (
                f"Call write_file exactly once with path {CONTRACT_FILE} and content exactly "
                f"{contract_token}. After the tool result, reply exactly TURN1_OK."
            ),
            index=1,
        )
        report["turns"].append(turn1)
        if turn1["status"] != "completed":
            raise ContractTurnError("turn 1 failed: " + "; ".join(turn1["errors"]))
        turn2 = await _run_turn(
            loop,
            (
                f"Using the prior turn, call read_file exactly once for {CONTRACT_FILE}. "
                f"After the tool result, reply exactly TURN2_OK {contract_token}."
            ),
            index=2,
        )
        report["turns"].append(turn2)
        if turn2["status"] != "completed":
            raise ContractTurnError("turn 2 failed: " + "; ".join(turn2["errors"]))
        artifact_content = proof_path.read_text(encoding="utf-8") if proof_path.is_file() else ""
        report["artifact"] = {
            "path": CONTRACT_FILE,
            "exists": proof_path.is_file(),
            "bytes": len(artifact_content.encode("utf-8")),
            "sha256": hashlib.sha256(artifact_content.encode("utf-8")).hexdigest(),
        }
        report["checks"] = _contract_checks(
            report["turns"], artifact_content=artifact_content, contract_token=contract_token
        )
        report["usage"] = {
            "input_tokens": sum(turn["usage"]["input_tokens"] for turn in report["turns"]),
            "output_tokens": sum(turn["usage"]["output_tokens"] for turn in report["turns"]),
        }
        report["stream"] = {
            "text_delta_events": sum(
                turn["stream"]["text_delta_events"] for turn in report["turns"]
            ),
            "provider_retries": sum(
                turn["stream"]["provider_retries"] for turn in report["turns"]
            ),
        }
        failed_checks = [name for name, passed in report["checks"].items() if not passed]
        if failed_checks:
            turn_errors = [error for turn in report["turns"] for error in turn.get("errors", [])]
            details = []
            if turn_errors:
                details.append("turn errors: " + "; ".join(turn_errors))
            details.append("failed checks: " + ", ".join(failed_checks))
            report["status"] = "failed"
            report["error"] = {
                "type": "ContractCheckError",
                "message": " | ".join(details),
            }
        else:
            report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {
            "type": type(exc).__name__,
            "message": _safe_error(str(exc)),
        }
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            await close()
        report["duration_ms"] = int((time.monotonic() - started) * 1000)
    return report


async def _run_turn(loop: AgentLoop, prompt: str, *, index: int) -> dict[str, Any]:
    before_input = loop.input_tokens
    before_output = loop.output_tokens
    events: list[AgentEvent] = []
    async for event in loop.run(prompt):
        events.append(event)
    tool_starts = [event for event in events if event.kind == "tool_start"]
    tool_ends = [event for event in events if event.kind == "tool_end"]
    deltas = [event.message for event in events if event.kind == "assistant_delta"]
    assistants = [event.message for event in events if event.kind == "assistant"]
    done = next((event for event in reversed(events) if event.kind == "done"), None)
    errors = [
        _safe_error(event.message)
        for event in events
        if event.kind == "error" or (event.kind == "tool_end" and event.data.get("is_error"))
    ]
    return {
        "index": index,
        "status": "completed" if done is not None and not errors else "failed",
        "model_steps": int(done.data["steps"]) if done is not None else None,
        "tool_calls": [
            {"name": event.data["name"], "arguments": event.data["input"]}
            for event in tool_starts
        ],
        "tool_results": [
            {
                "name": event.data["name"],
                "is_error": bool(event.data["is_error"]),
                "elapsed_ms": int(event.data["elapsed_ms"]),
            }
            for event in tool_ends
        ],
        "assistant_text": "".join(deltas) if deltas else (assistants[-1] if assistants else ""),
        "stream": {
            "text_delta_events": len(deltas),
            "provider_retries": sum(event.kind == "provider_retry" for event in events),
        },
        "usage": {
            "input_tokens": loop.input_tokens - before_input,
            "output_tokens": loop.output_tokens - before_output,
        },
        "errors": errors,
    }


def _contract_checks(
    turns: list[dict[str, Any]], *, artifact_content: str, contract_token: str
) -> dict[str, bool]:
    first = turns[0] if len(turns) > 0 else {}
    second = turns[1] if len(turns) > 1 else {}
    first_tools = [item["name"] for item in first.get("tool_calls", [])]
    second_tools = [item["name"] for item in second.get("tool_calls", [])]
    return {
        "two_turns_completed": len(turns) == 2
        and all(turn.get("status") == "completed" for turn in turns),
        "write_tool_called": "write_file" in first_tools,
        "read_tool_called": "read_file" in second_tools,
        "tool_results_successful": all(
            not result["is_error"]
            for turn in turns
            for result in turn.get("tool_results", [])
        ),
        "artifact_exact": artifact_content == contract_token,
        "turn1_marker": "TURN1_OK" in first.get("assistant_text", ""),
        "turn2_marker": (
            "TURN2_OK" in second.get("assistant_text", "")
            and contract_token in second.get("assistant_text", "")
        ),
        "streaming_text_observed": sum(
            turn.get("stream", {}).get("text_delta_events", 0) for turn in turns
        )
        > 0,
        "usage_reported": sum(
            turn.get("usage", {}).get("input_tokens", 0)
            + turn.get("usage", {}).get("output_tokens", 0)
            for turn in turns
        )
        > 0,
    }


def skipped_report(config: ContractConfig, reason: str) -> dict[str, Any]:
    report = _base_report(config, datetime.now(timezone.utc).isoformat())
    report.update(
        {
            "status": "skipped",
            "duration_ms": 0,
            "error": {"type": "MissingCredential", "message": reason},
        }
    )
    return report


def merge_reports(paths: list[Path]) -> dict[str, Any]:
    results = []
    for path in sorted(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "case": path.stem,
                    "status": "failed",
                    "provider": {"protocol": "unknown", "model": "unknown"},
                    "error": {
                        "type": "InvalidContractArtifact",
                        "message": _safe_error(str(exc)),
                    },
                }
            )
            continue
        if "case" in payload and "status" in payload:
            results.append(payload)
    counts = {
        status: sum(item.get("status") == status for item in results)
        for status in ("passed", "failed", "skipped")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"total": len(results), **counts},
        "results": sorted(results, key=lambda item: str(item.get("case", ""))),
    }


def render_markdown(matrix: dict[str, Any]) -> str:
    summary = matrix["summary"]
    lines = [
        "# Provider contract matrix",
        "",
        (
            f"Total: {summary['total']} · Passed: {summary['passed']} · "
            f"Failed: {summary['failed']} · Skipped: {summary['skipped']}"
        ),
        "",
        "| Case | Status | Protocol | Model | Tools | Stream deltas | Usage | Duration |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for result in matrix["results"]:
        tools = ", ".join(
            call["name"] for turn in result.get("turns", []) for call in turn.get("tool_calls", [])
        ) or "-"
        usage = result.get("usage", {})
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        lines.append(
            "| {case} | {status} | {protocol} | {model} | {tools} | {deltas} | "
            "{tokens} | {duration}ms |".format(
                case=result.get("case", "-"),
                status=result.get("status", "-"),
                protocol=result.get("provider", {}).get("protocol", "-"),
                model=result.get("provider", {}).get("model", "-"),
                tools=tools,
                deltas=result.get("stream", {}).get("text_delta_events", 0),
                tokens=tokens,
                duration=result.get("duration_ms", 0),
            )
        )
    return "\n".join(lines) + "\n"


def _base_report(config: ContractConfig, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case": config.case,
        "status": "running",
        "started_at": started_at,
        "duration_ms": None,
        "provider": {
            "protocol": config.api_mode,
            "model": config.model,
            "base_url": _safe_base_url(config.base_url),
            "adapter": (
                "OpenAIResponsesProvider"
                if config.api_mode == "responses"
                else "OpenAICompatibleProvider"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "mini_openharness": __version__,
            "github_actions": os.getenv("GITHUB_ACTIONS") == "true",
        },
        "turns": [],
        "checks": {},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "stream": {"text_delta_events": 0, "provider_retries": 0},
        "artifact": {"path": CONTRACT_FILE, "exists": False, "bytes": 0, "sha256": ""},
        "error": None,
    }


def _safe_base_url(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _safe_error(value: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", value).replace("\r", " ").replace("\n", " ")[:1000]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or merge live provider contracts")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--case", required=True)
    run.add_argument("--api-mode", choices=("responses", "chat"), required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--api-key-env", default="PROVIDER_API_KEY")
    run.add_argument("--allow-missing-key", action="store_true")
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--max-steps", type=int, default=6)
    merge = commands.add_parser("merge")
    merge.add_argument("--input-dir", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--markdown", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "merge":
        matrix = merge_reports(list(args.input_dir.rglob("*.json")))
        _write_json(args.output, matrix)
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(matrix), encoding="utf-8")
        return 0

    config = ContractConfig(
        case=args.case,
        api_mode=args.api_mode,
        model=args.model,
        base_url=args.base_url,
        workspace=args.workspace.resolve(),
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        max_steps=args.max_steps,
    )
    api_key = os.getenv(args.api_key_env, "")
    if not api_key:
        report = skipped_report(config, f"environment variable {args.api_key_env} is not set")
        _write_json(args.output, report)
        print(json.dumps({"case": config.case, "status": "skipped"}))
        return 0 if args.allow_missing_key else 2
    try:
        report = asyncio.run(run_provider_contract(config, api_key=api_key))
    except BaseException as exc:
        report = _base_report(config, datetime.now(timezone.utc).isoformat())
        report.update(
            {
                "status": "failed",
                "duration_ms": 0,
                "error": {"type": type(exc).__name__, "message": _safe_error(str(exc))},
            }
        )
    _write_json(args.output, report)
    print(
        json.dumps(
            {
                "case": report["case"],
                "status": report["status"],
                "model": report["provider"]["model"],
                "protocol": report["provider"]["protocol"],
                "usage": report["usage"],
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
