"""Tiny JSON session store with atomic replacement."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mini_openharness.models import Message


class SessionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, messages: list[Message]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps([message.to_dict() for message in messages], ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def load(self) -> list[Message]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Session file must contain a JSON array")
        return [Message.from_dict(item) for item in data]
