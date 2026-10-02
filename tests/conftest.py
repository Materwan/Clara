from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

from clara.llm import LlmChunk, ToolCall
from clara.memory import Memory
from clara.providers import ProviderManager
from clara.settings import Settings


class FakeBackend:
    """Plays scripted rounds: each round is a list of LlmChunk. Records what it was sent."""

    def __init__(self, *rounds: list[LlmChunk], model: str = "fake"):
        self.model = model
        self.rounds = list(rounds)
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def list_models(self) -> list[str]:
        return ["fake", "fake-big", "other-model"]

    async def verify(self) -> None:
        await self.list_models()

    async def stream(self, messages, tools) -> AsyncIterator[LlmChunk]:
        self.calls.append(([dict(m) for m in messages], tools))
        for chunk in self.rounds.pop(0):
            yield chunk


def say(*pieces: str) -> list[LlmChunk]:
    return [LlmChunk(text=piece) for piece in pieces] + [LlmChunk(prompt_tokens=10, completion_tokens=3)]


def call(name: str, **arguments) -> list[LlmChunk]:
    return [LlmChunk(tool_calls=[ToolCall(name, arguments)])]


@pytest.fixture
def memory(tmp_path: Path):
    mem = Memory(tmp_path / "clara.sqlite")
    yield mem
    mem.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "CLARA_TOKENS": "terminal:secret-cli,discord:secret-discord",
            "CLARA_ADMIN_TOKENS": "ops:secret-admin",
            "OLLAMA_API_KEY": "key-123",
            "CLARA_LOCAL_MODEL": "fake",
            "CLARA_CLOUD_MODEL": "fake-big",
            "CLARA_DATA_DIR": str(tmp_path / "data"),
            "CLARA_SYSTEM_PROMPT_FILE": str(tmp_path / "missing.md"),
        }
    )


def fake_providers(settings: Settings, backend: FakeBackend | None = None) -> ProviderManager:
    """A ProviderManager whose backends are fakes (the given one, or one per provider/model)."""
    return ProviderManager.from_settings(
        settings, factory=lambda config, model: backend or FakeBackend(model=model)
    )
