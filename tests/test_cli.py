import os

import mini_openharness.cli as cli
from mini_openharness.cli import _load_environment, build_run_parser
from mini_openharness.provider import ProviderError


def test_local_dotenv_is_loaded_without_overriding_shell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=from-file\nOPENAI_MODEL=from-file-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "from-shell")

    _load_environment()

    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert os.environ["OPENAI_MODEL"] == "from-shell"


def test_cli_defaults_to_responses_and_sandbox_shell_is_opt_in(monkeypatch):
    monkeypatch.delenv("OPENAI_API_MODE", raising=False)

    defaults = build_run_parser().parse_args([])
    sandboxed = build_run_parser().parse_args(["--sandbox-shell"])

    assert defaults.api_mode == "responses"
    assert defaults.sandbox_shell is False
    assert sandboxed.sandbox_shell is True


def test_cli_accepts_hook_configuration():
    args = build_run_parser().parse_args(["--hooks-config", "hooks.json"])

    assert args.hooks_config == "hooks.json"


def test_cli_provider_error_returns_nonzero_and_hints_on_responses_404(
    tmp_path, monkeypatch, capsys
):
    class MissingResponsesEndpoint:
        def __init__(self, **kwargs):
            del kwargs

        async def complete(self, messages, tools):
            del messages, tools
            raise ProviderError("HTTP 404: not found")

        async def close(self):
            pass

    monkeypatch.setattr(cli, "OpenAIResponsesProvider", MissingResponsesEndpoint)

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--api-key",
            "test-key",
            "--api-mode",
            "responses",
            "--no-trace",
            "probe",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: HTTP 404" in captured.err
    assert "--api-mode chat" in captured.err
