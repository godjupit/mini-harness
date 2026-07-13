import os

from mini_openharness.cli import _load_environment


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
