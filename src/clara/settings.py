"""Configuration, read once from the environment (and a `.env` file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


class SettingsError(Exception):
    pass


def parse_tokens(raw: str) -> dict[str, str]:
    """`"terminal:abc,discord:def"` -> `{"abc": "terminal", "def": "discord"}`.

    A bare token (no `name:`) is accepted and named "client".
    """
    tokens: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, token = item.partition(":")
        if not separator:
            name, token = "client", item
        name, token = name.strip(), token.strip()
        if not token:
            raise SettingsError(f"Empty token for client {name!r} in CLARA_TOKENS")
        tokens[token] = name
    return tokens


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value < 1:
        raise SettingsError(f"{key} must be at least 1")
    return value


DEFAULT_LOCAL_HOST = "http://localhost:11434"
DEFAULT_CLOUD_HOST = "https://ollama.com"
PROVIDER_IDS = ("local", "cloud")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    tokens: dict[str, str] = field(repr=False)  # chat token -> client name
    admin_tokens: dict[str, str] = field(repr=False)  # console token -> admin name
    history_messages: int
    max_concurrent_llm: int
    system_prompt_file: Path
    # Language model providers (see providers.py)
    default_provider: str
    local_host: str
    local_model: str
    cloud_host: str
    cloud_model: str
    ollama_api_key: str | None = field(repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clara.sqlite"

    @property
    def runtime_state_file(self) -> Path:
        return self.data_dir / "runtime.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            load_dotenv()
            env = os.environ

        def text(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        tokens = parse_tokens(env.get("CLARA_TOKENS", ""))
        if not tokens:
            raise SettingsError(
                "CLARA_TOKENS is empty: the API refuses to start without credentials.\n"
                "Generate a token with:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
                "and put it in .env as CLARA_TOKENS=terminal:<token>"
            )
        admin_tokens = parse_tokens(env.get("CLARA_ADMIN_TOKENS", ""))
        if set(tokens) & set(admin_tokens):
            raise SettingsError("A token cannot be both a chat token and an admin token.")

        api_key = text("OLLAMA_API_KEY") or None
        default_provider = text("CLARA_PROVIDER", "local").lower()
        if default_provider not in PROVIDER_IDS:
            raise SettingsError(f"CLARA_PROVIDER must be one of {', '.join(PROVIDER_IDS)}")
        if default_provider == "cloud" and not api_key:
            raise SettingsError("CLARA_PROVIDER=cloud needs OLLAMA_API_KEY.")

        return cls(
            host=text("CLARA_HOST", "127.0.0.1"),
            port=_positive_int(env, "CLARA_PORT", 8765),
            data_dir=Path(text("CLARA_DATA_DIR", "data")),
            tokens=tokens,
            admin_tokens=admin_tokens,
            history_messages=_positive_int(env, "CLARA_HISTORY_MESSAGES", 20),
            max_concurrent_llm=_positive_int(env, "CLARA_MAX_CONCURRENT_LLM", 2),
            system_prompt_file=Path(text("CLARA_SYSTEM_PROMPT_FILE", "config/system_prompt.md")),
            default_provider=default_provider,
            local_host=text("OLLAMA_HOST", DEFAULT_LOCAL_HOST),
            local_model=text("CLARA_LOCAL_MODEL", "gemma4:31b-cloud"),
            cloud_host=text("CLARA_CLOUD_HOST", DEFAULT_CLOUD_HOST),
            cloud_model=text("CLARA_CLOUD_MODEL", "gpt-oss:120b"),
            ollama_api_key=api_key,
        )
