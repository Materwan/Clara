"""Configuration, read once from the environment (and a `.env` file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


class SettingsError(Exception):
    pass


PLACEHOLDER_TOKEN = "change-me"  # what .env.example ships with
SURFACE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


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


def parse_client_surfaces(raw: str, clients: set[str]) -> dict[str, frozenset[str]]:
    """`"terminal=cli|console,discord=discord"` -> the surfaces each client may speak for."""
    allowed: dict[str, frozenset[str]] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, surfaces = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: expected name=surface|surface, got {item!r}")
        if name not in clients:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: {name!r} is not a client of CLARA_TOKENS")
        names = frozenset(part.strip() for part in surfaces.split("|") if part.strip())
        if not names:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: no surface for {name!r}")
        for surface in names:
            if not SURFACE_RE.match(surface):
                raise SettingsError(f"CLARA_CLIENT_SURFACES: bad surface {surface!r} (a-z, 0-9, _ -)")
        allowed[name] = names | allowed.get(name, frozenset())
    return allowed


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


def _flag(env: Mapping[str, str], key: str) -> bool:
    raw = env.get(key, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise SettingsError(f"{key} must be true or false, got {raw!r}")


def _non_negative_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value < 0:
        raise SettingsError(f"{key} cannot be negative")
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    tokens: dict[str, str] = field(repr=False)  # chat token -> client name
    admin_tokens: dict[str, str] = field(repr=False)  # console token -> admin name
    # client name -> surfaces it may speak for; a client with no entry may use any
    client_surfaces: dict[str, frozenset[str]]
    history_turns: int
    max_concurrent_llm: int
    max_tool_rounds: int
    tool_timeout: int  # seconds a client may take to run its tools (it may ask the user first)
    llm_first_token_timeout: int  # seconds the model may take to start answering
    llm_idle_timeout: int  # seconds the model may pause between two pieces of its answer
    compact_percent: int  # summarise a conversation when its context is this full (0 = never)
    keep_recent_turns: int  # turns a compaction leaves unsummarised
    facts_token_budget: int  # tokens of remembered facts shown to the model in each prompt
    purge_summarised: bool  # delete messages once a summary stands for them
    reminder_ai_timeout: int  # seconds Clara has to write the announcement of a reminder (0: announce the text)
    system_prompt_file: Path
    # Language model providers (see providers.py)
    default_provider: str
    local_host: str
    local_model: str
    local_context_window: int
    cloud_host: str
    cloud_model: str
    cloud_context_window: int
    ollama_api_key: str | None = field(repr=False)

    @property
    def unrestricted_clients(self) -> list[str]:
        """Clients allowed to speak for any surface (no CLARA_CLIENT_SURFACES entry)."""
        return sorted(set(self.tokens.values()) - set(self.client_surfaces))

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
        for token in (*tokens, *admin_tokens):
            if token.lower().startswith(PLACEHOLDER_TOKEN):
                raise SettingsError(
                    "A token still has the placeholder value of .env.example. Generate a real one with:\n"
                    '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
        if set(tokens) & set(admin_tokens):
            raise SettingsError("A token cannot be both a chat token and an admin token.")

        client_surfaces = parse_client_surfaces(
            env.get("CLARA_CLIENT_SURFACES", ""), set(tokens.values())
        )

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
            client_surfaces=client_surfaces,
            history_turns=_positive_int(env, "CLARA_HISTORY_TURNS", 20),
            max_concurrent_llm=_positive_int(env, "CLARA_MAX_CONCURRENT_LLM", 2),
            max_tool_rounds=_positive_int(env, "CLARA_MAX_TOOL_ROUNDS", 40),
            tool_timeout=_positive_int(env, "CLARA_TOOL_TIMEOUT", 900),
            llm_first_token_timeout=_positive_int(env, "CLARA_LLM_FIRST_TOKEN_TIMEOUT", 300),
            llm_idle_timeout=_positive_int(env, "CLARA_LLM_IDLE_TIMEOUT", 120),
            compact_percent=_non_negative_int(env, "CLARA_COMPACT_PERCENT", 80),
            keep_recent_turns=_non_negative_int(env, "CLARA_KEEP_RECENT_TURNS", 2),
            facts_token_budget=_positive_int(env, "CLARA_FACTS_TOKEN_BUDGET", 2000),
            purge_summarised=_flag(env, "CLARA_PURGE_SUMMARISED"),
            reminder_ai_timeout=_non_negative_int(env, "CLARA_REMINDER_AI_TIMEOUT", 60),
            system_prompt_file=Path(text("CLARA_SYSTEM_PROMPT_FILE", "config/system_prompt.md")),
            default_provider=default_provider,
            local_host=text("OLLAMA_HOST", DEFAULT_LOCAL_HOST),
            local_model=text("CLARA_LOCAL_MODEL", "llama3.2"),
            local_context_window=_positive_int(env, "CLARA_LOCAL_CONTEXT_WINDOW", 32_768),
            cloud_host=text("CLARA_CLOUD_HOST", DEFAULT_CLOUD_HOST),
            cloud_model=text("CLARA_CLOUD_MODEL", "gpt-oss:120b"),
            cloud_context_window=_positive_int(env, "CLARA_CLOUD_CONTEXT_WINDOW", 131_072),
            ollama_api_key=api_key,
        )
