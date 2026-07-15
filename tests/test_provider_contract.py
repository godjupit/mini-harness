from __future__ import annotations

import asyncio
import json

from mini_openharness.models import ModelReply, ToolCall
from mini_openharness.provider import ProviderComplete, ProviderError, ProviderTextDelta
from mini_openharness.provider_contract import (
    ContractConfig,
    main,
    merge_reports,
    render_markdown,
    run_provider_contract,
)


class ContractProvider:
    def __init__(self, token: str) -> None:
        self.replies = [
            ModelReply(
                tool_calls=(
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": "provider-contract.txt", "content": token},
                    ),
                ),
                input_tokens=10,
                output_tokens=2,
            ),
            ModelReply(content="TURN1_OK", input_tokens=15, output_tokens=3),
            ModelReply(
                tool_calls=(
                    ToolCall("read-1", "read_file", {"path": "provider-contract.txt"}),
                ),
                input_tokens=20,
                output_tokens=2,
            ),
            ModelReply(content=f"TURN2_OK {token}", input_tokens=25, output_tokens=4),
        ]
        self.closed = False

    async def stream(self, messages, tools, *, cancel_event=None):
        del messages, tools, cancel_event
        reply = self.replies.pop(0)
        if reply.content:
            yield ProviderTextDelta(reply.content)
        yield ProviderComplete(reply)

    async def close(self):
        self.closed = True


def test_two_turn_contract_records_tools_stream_and_usage(tmp_path):
    config = ContractConfig(
        case="openai-chat",
        api_mode="chat",
        model="test-model",
        base_url="https://example.test/v1",
        workspace=tmp_path,
    )
    provider = ContractProvider("CONTRACT_OPENAI_CHAT")

    report = asyncio.run(run_provider_contract(config, provider=provider))

    assert report["status"] == "passed"
    assert report["usage"] == {"input_tokens": 70, "output_tokens": 11}
    assert report["stream"]["text_delta_events"] == 2
    assert [turn["tool_calls"][0]["name"] for turn in report["turns"]] == [
        "write_file",
        "read_file",
    ]
    assert all(report["checks"].values())
    assert provider.closed is True


def test_missing_key_writes_explicit_skipped_result(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    output = tmp_path / "result.json"

    exit_code = main(
        [
            "run",
            "--case",
            "deepseek-chat",
            "--api-mode",
            "chat",
            "--model",
            "deepseek-v4-flash",
            "--base-url",
            "https://api.deepseek.com",
            "--workspace",
            str(tmp_path / "workspace"),
            "--output",
            str(output),
            "--api-key-env",
            "MISSING_PROVIDER_KEY",
            "--allow-missing-key",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["status"] == "skipped"
    assert "MISSING_PROVIDER_KEY" in report["error"]["message"]


def test_merge_report_and_markdown_summary(tmp_path):
    passed = {
        "case": "openai-responses",
        "status": "passed",
        "provider": {"protocol": "responses", "model": "gpt-4.1-mini"},
        "turns": [{"tool_calls": [{"name": "write_file"}]}],
        "stream": {"text_delta_events": 4},
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "duration_ms": 100,
    }
    skipped = {
        "case": "deepseek-chat",
        "status": "skipped",
        "provider": {"protocol": "chat", "model": "deepseek-v4-flash"},
    }
    (tmp_path / "a.json").write_text(json.dumps(passed), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(skipped), encoding="utf-8")

    matrix = merge_reports(list(tmp_path.glob("*.json")))
    markdown = render_markdown(matrix)

    assert matrix["summary"] == {"total": 2, "passed": 1, "failed": 0, "skipped": 1}
    assert "openai-responses" in markdown
    assert "write_file" in markdown


def test_merge_records_invalid_json_as_failed_contract(tmp_path):
    broken = tmp_path / "openai-chat.json"
    broken.write_text("not-json", encoding="utf-8")

    matrix = merge_reports([broken])

    assert matrix["summary"] == {"total": 1, "passed": 0, "failed": 1, "skipped": 0}
    assert matrix["results"][0]["error"]["type"] == "InvalidContractArtifact"


def test_live_failure_is_redacted_and_provider_is_closed(tmp_path):
    class SecretFailingProvider:
        closed = False

        async def stream(self, messages, tools, *, cancel_event=None):
            del messages, tools, cancel_event
            raise ProviderError("Bearer private-token sk-abcdefghijklmnop")
            yield

        async def close(self):
            self.closed = True

    config = ContractConfig(
        case="openai-responses",
        api_mode="responses",
        model="test-model",
        base_url="https://api.openai.com/v1",
        workspace=tmp_path,
    )
    provider = SecretFailingProvider()

    report = asyncio.run(run_provider_contract(config, provider=provider))

    assert report["status"] == "failed"
    assert "private-token" not in report["error"]["message"]
    assert "abcdefghijklmnop" not in report["error"]["message"]
    assert report["error"]["type"] == "ContractTurnError"
    assert report["error"]["message"] == "turn 1 failed: [REDACTED] [REDACTED]"
    assert len(report["turns"]) == 1
    assert provider.closed is True
