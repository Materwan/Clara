"""The operator commands (/provider, /status, /people...).

One registry, two front ends: the console embedded in the server and the
remote `clara-admin` console (through `POST /v1/admin/command`) run exactly
the same handlers. A handler gets the raw argument text and returns text.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .agent import Agent
from .memory import Memory, Person
from .providers import ProviderError, ProviderManager
from .settings import Settings

log = logging.getLogger(__name__)

SURFACE_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")


class CommandError(Exception):
    """A mistake of the operator: shown as is, not logged."""


@dataclass(frozen=True)
class CommandResult:
    output: str = ""
    quit: bool = False  # the console should close


@dataclass
class CommandContext:
    settings: Settings
    memory: Memory
    agent: Agent
    providers: ProviderManager
    started_at: float
    listen: str  # "127.0.0.1:8765"


Handler = Callable[[CommandContext, str], "Awaitable[CommandResult | str]"]
Choices = Callable[[CommandContext], list[str]]


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    handler: Handler
    choices: Choices | None = None  # suggestions for the first argument


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def command(self, name: str, usage: str, summary: str, choices: Choices | None = None):
        def register(handler: Handler) -> Handler:
            self._commands[name] = Command(name, usage, summary, handler, choices)
            return handler

        return register

    @property
    def names(self) -> list[str]:
        return list(self._commands)

    def describe(self, context: CommandContext) -> list[dict]:
        """What a console needs to offer completion."""
        return [
            {
                "name": command.name,
                "usage": command.usage,
                "summary": command.summary,
                "choices": command.choices(context) if command.choices else [],
            }
            for command in self._commands.values()
        ]

    async def execute(self, line: str, context: CommandContext) -> CommandResult:
        parts = line.strip().lstrip("/").split(None, 1)  # the leading "/" is optional
        if not parts:
            return CommandResult()
        name, arguments = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        command = self._commands.get(name)
        if command is None:
            return CommandResult(f"Unknown command: {name}. Type /help.")
        try:
            result = await command.handler(context, arguments)
        except CommandError as error:
            return CommandResult(f"! {error}")
        except Exception:
            log.exception("command %s failed", name)
            return CommandResult("! The command failed (details in the server log).")
        return result if isinstance(result, CommandResult) else CommandResult(result)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()
             for row in [headers, *rows]]
    return "\n".join(lines)


def duration(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def find_person(memory: Memory, reference: str) -> Person:
    """A person from `3` (id), `discord:1234` (account) or `Erwan` (name)."""
    reference = reference.strip()
    if not reference:
        raise CommandError("Which person? Give an id, a surface:user account, or a name.")
    if reference.isdigit():
        person = memory.person_by_id(int(reference))
    elif ":" in reference:
        surface, _, external_id = reference.partition(":")
        person = memory.find_person(surface.lower(), external_id)
    else:
        matches = memory.people_named(reference)
        if len(matches) > 1:
            ids = ", ".join(str(person.id) for person in matches)
            raise CommandError(f"{len(matches)} people are called {reference}: use their id ({ids}).")
        person = matches[0] if matches else None
    if person is None:
        raise CommandError(f"Nobody matches {reference!r}. See /people.")
    return person


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
registry = CommandRegistry()


@registry.command("help", "[command]", "List the commands", lambda ctx: registry.names)
async def help_command(ctx: CommandContext, args: str) -> str:
    descriptions = registry.describe(ctx)
    if args:
        name = args.lstrip("/").lower()
        for entry in descriptions:
            if entry["name"] == name:
                return f"/{name} {entry['usage']}".rstrip() + f"\n  {entry['summary']}"
        raise CommandError(f"No command called {name}.")
    return table(
        ["command", "what it does"],
        [[f"/{e['name']} {e['usage']}".rstrip(), e["summary"]] for e in descriptions],
    )


@registry.command("quit", "", "Close this console (the embedded one also stops the server)")
async def quit_command(ctx: CommandContext, args: str) -> CommandResult:
    return CommandResult(quit=True)


@registry.command("status", "", "Provider, model, activity and memory size")
async def status_command(ctx: CommandContext, args: str) -> str:
    stats = ctx.agent.stats
    people, facts = ctx.memory.counts()
    config = ctx.providers.config
    rows = [
        ["Provider", f"{config.id} ({config.label}) at {config.host}"],
        ["Model", ctx.providers.model],
        ["Server", f"{ctx.listen}, up {duration(time.monotonic() - ctx.started_at)}"],
        ["Turns", f"{stats.active} running, {stats.turns} since start"],
        ["Tokens", f"{stats.prompt_tokens:,} prompt / {stats.completion_tokens:,} completion"],
        ["Memory", f"{people} people, {facts} facts ({ctx.settings.db_path})"],
    ]
    return "\n".join(f"{label:<9}{value}" for label, value in rows)


@registry.command(
    "provider",
    "[local|cloud]",
    "Show the providers, or switch where the model runs (saved across restarts)",
    lambda ctx: list(ctx.providers.configs),
)
async def provider_command(ctx: CommandContext, args: str) -> str:
    providers = ctx.providers
    if args:
        try:
            config = providers.switch(args)
        except ProviderError as error:
            raise CommandError(str(error)) from None
        problem = await providers.check()
        lines = [f"Now using {config.id} ({config.label}), model {providers.model}."]
        lines.append("Reachable." if problem is None else f"! Not reachable: {problem} (still selected)")
        return "\n".join(lines)

    rows = []
    for config in providers.configs.values():
        note = "" if config.usable else "(no OLLAMA_API_KEY)"
        if config.needs_key and config.usable:
            note = "(key set)"
        rows.append(
            [
                "*" if config.id == providers.active else "",
                config.id,
                config.label,
                config.host,
                providers.model_of(config.id),
                note,
            ]
        )
    return table(["", "id", "provider", "host", "model", ""], rows)


@registry.command("model", "[name]", "Show or change the model of the active provider")
async def model_command(ctx: CommandContext, args: str) -> str:
    providers = ctx.providers
    try:
        available = await providers.list_models()
    except Exception as error:
        available = None
        unreachable = f"{type(error).__name__}: {str(error)[:200]}"

    if not args:
        lines = [f"{providers.config.id} model: {providers.model}"]
        if available is None:
            lines.append(f"! Cannot list models: {unreachable}")
        else:
            lines += [("* " if name == providers.model else "  ") + name for name in available]
        return "\n".join(lines)

    if available is not None and args not in available:
        shown = ", ".join(available[:15]) + (" ..." if len(available) > 15 else "")
        raise CommandError(f"{args!r} is not offered by {providers.config.id}. Available: {shown}")
    try:
        providers.set_model(args)
    except ProviderError as error:
        raise CommandError(str(error)) from None
    note = "" if available is not None else f"\n! Not checked, provider unreachable: {unreachable}"
    return f"{providers.config.id} now uses {args}.{note}"


@registry.command("people", "", "Everybody Clara knows, with their accounts")
async def people_command(ctx: CommandContext, args: str) -> str:
    summaries = ctx.memory.summaries()
    if not summaries:
        return "Nobody yet."
    return table(
        ["id", "name", "facts", "accounts"],
        [
            [str(s.person.id), s.person.name, str(s.facts), ", ".join(s.accounts)]
            for s in summaries
        ],
    )


@registry.command(
    "facts",
    "<person> [add <text> | del <id>]",
    "List, add or delete what Clara knows about a person (id, surface:user or name)",
)
async def facts_command(ctx: CommandContext, args: str) -> str:
    reference, _, action = args.partition(" ")
    person = find_person(ctx.memory, reference)
    verb, _, rest = action.strip().partition(" ")
    rest = rest.strip()

    if verb == "add":
        try:
            fact = ctx.memory.add_fact(person.id, rest)
        except ValueError as error:
            raise CommandError(str(error)) from None
        return "Stored." if fact else "Already known."
    if verb == "del":
        if not rest.isdigit():
            raise CommandError("Usage: /facts <person> del <fact id>")
        if not ctx.memory.delete_fact(person.id, int(rest)):
            raise CommandError(f"{person.name} has no fact {rest}.")
        return "Deleted."
    if verb:
        raise CommandError("Usage: /facts <person> [add <text> | del <id>]")

    facts = ctx.memory.facts(person.id, 1000)
    lines = [f"{person.name} (id {person.id})"]
    lines += [f"  [{fact.id}] {fact.text}" for fact in facts] or ["  (no facts)"]
    return "\n".join(lines)


@registry.command(
    "link",
    "<surface:user> <person>",
    "Make an account belong to a person (merges them if it already has its own)",
)
async def link_command(ctx: CommandContext, args: str) -> str:
    parts = args.split(None, 1)
    if len(parts) != 2 or ":" not in parts[0]:
        raise CommandError("Usage: /link <surface:user> <person>   e.g. /link discord:1234 Erwan")
    surface, _, external_id = parts[0].partition(":")
    surface = surface.lower()
    if not SURFACE_PATTERN.match(surface) or not external_id:
        raise CommandError("The account looks like surface:user, e.g. discord:1234.")
    target = find_person(ctx.memory, parts[1])
    ctx.memory.link_account(surface, external_id, target)
    accounts = ", ".join(f"{s}:{e}" for s, e in ctx.memory.accounts_of(target.id))
    return f"{target.name} now has: {accounts}"
