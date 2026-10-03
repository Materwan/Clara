"""Where the model runs, switchable while the server is running.

    local  "Local host"     Ollama on this machine (or any host in OLLAMA_HOST)
    cloud  "Ollama API key" ollama.com, authenticated with OLLAMA_API_KEY

`ProviderManager` is itself an `LlmBackend`: the agent talks to it, and each
model round goes to whichever provider is active at that moment. The choice
(and the model picked for each provider) is saved in `runtime.json` so it
survives a restart. The API key is never saved or shown; it only ever comes
from the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from .llm import LlmBackend, LlmChunk, OllamaBackend
from .settings import Settings
from .traffic import TrafficLog

log = logging.getLogger(__name__)

T = TypeVar("T")

CHECK_TIMEOUT = 8.0


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    label: str
    host: str
    default_model: str
    api_key: str | None
    needs_key: bool
    context_window: int = 32_768  # tokens: what the percentages of the context are relative to
    request_context: bool = False  # ask the server for exactly that window (local Ollama only)

    @property
    def usable(self) -> bool:
        return bool(self.api_key) or not self.needs_key


BackendFactory = Callable[[ProviderConfig, str], LlmBackend]

_ALIASES = {
    "local": "local",
    "localhost": "local",
    "local-host": "local",
    "cloud": "cloud",
    "apikey": "cloud",
    "api-key": "cloud",
    "ollama-api-key": "cloud",
}


def default_factory(config: ProviderConfig, model: str) -> LlmBackend:
    return OllamaBackend(
        model,
        host=config.host,
        api_key=config.api_key,
        num_ctx=config.context_window if config.request_context else None,
    )


def configs_from_settings(settings: Settings) -> dict[str, ProviderConfig]:
    return {
        "local": ProviderConfig(
            "local",
            "Local host",
            settings.local_host,
            settings.local_model,
            None,
            False,
            settings.local_context_window,
            request_context=True,
        ),
        "cloud": ProviderConfig(
            "cloud",
            "Ollama API key",
            settings.cloud_host,
            settings.cloud_model,
            settings.ollama_api_key,
            True,
            settings.cloud_context_window,
        ),
    }


class ProviderManager:
    def __init__(
        self,
        configs: dict[str, ProviderConfig],
        default: str,
        state_path: Path | None = None,
        factory: BackendFactory = default_factory,
    ):
        self.configs = configs
        self.state_path = state_path
        self._factory = factory
        self.traffic: TrafficLog | None = None  # where the calls to the provider are logged
        self._models = {name: config.default_model for name, config in configs.items()}
        self.active = default
        self._load_state()
        self._backend = self._factory(self.config, self.model)

    @classmethod
    def from_settings(
        cls, settings: Settings, factory: BackendFactory = default_factory
    ) -> ProviderManager:
        return cls(
            configs_from_settings(settings),
            settings.default_provider,
            settings.runtime_state_file,
            factory,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def config(self) -> ProviderConfig:
        return self.configs[self.active]

    @property
    def model(self) -> str:
        return self._models[self.active]

    @property
    def context_window(self) -> int:
        return self.config.context_window

    def model_of(self, provider: str) -> str:
        return self._models[provider]

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, model in (state.get("models") or {}).items():
                if name in self._models and isinstance(model, str) and model.strip():
                    self._models[name] = model.strip()
            saved = state.get("provider")
            if saved in self.configs:
                if self.configs[saved].usable:
                    self.active = saved
                else:
                    log.warning("Saved provider %r has no API key any more; using %r", saved, self.active)
        except (OSError, ValueError, AttributeError):
            log.warning("Ignoring unreadable %s", self.state_path)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        state = {"provider": self.active, "models": self._models}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)  # atomic: never a half-written file

    # ------------------------------------------------------------------
    # Changing provider or model
    # ------------------------------------------------------------------
    def resolve(self, reference: str) -> str:
        provider = _ALIASES.get(reference.strip().lower())
        if provider is None:
            raise ProviderError(f"Unknown provider {reference!r}. Choose: {', '.join(self.configs)}.")
        return provider

    def switch(self, reference: str) -> ProviderConfig:
        provider = self.resolve(reference)
        config = self.configs[provider]
        if not config.usable:
            raise ProviderError(f"{config.label} needs OLLAMA_API_KEY in the server environment.")
        self.active = provider
        self._rebuild()
        return config

    def set_model(self, model: str) -> None:
        model = model.strip()
        if not model:
            raise ProviderError("The model name is empty.")
        self._models[self.active] = model
        self._rebuild()

    def _rebuild(self) -> None:
        self._backend = self._factory(self.config, self.model)
        self._save_state()

    # ------------------------------------------------------------------
    # LlmBackend: delegate to the active provider
    # ------------------------------------------------------------------
    @property
    def peer(self) -> str:
        """The provider, as named in the traffic log."""
        return f"ollama:{self.active}"

    def stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[LlmChunk]:
        stream = self._backend.stream(messages, tools)
        if self.traffic is None:
            return stream
        return self.traffic.model_stream(stream, self.peer, self.config.host, self.model, messages, tools)

    def _logged(self, operation: str, call: Awaitable[T]) -> Awaitable[T]:
        if self.traffic is None:
            return call
        return self.traffic.model_call(operation, self.peer, self.config.host, call)

    async def list_models(self) -> list[str]:
        return await asyncio.wait_for(self._logged("list_models", self._backend.list_models()), CHECK_TIMEOUT)

    async def check(self) -> str | None:
        """None if the active provider is usable (reachable, key accepted), else a short reason."""
        try:
            await asyncio.wait_for(self._logged("verify", self._backend.verify()), CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return "no answer"
        except Exception as error:
            return f"{type(error).__name__}: {str(error)[:200]}"
        return None
