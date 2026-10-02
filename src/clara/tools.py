"""Tools the model can call while answering.

Every call runs in a `ToolContext` naming the person who is talking, so a tool
can only touch *that* person's memory: the model cannot write notes about, or
erase, anybody else.

To add a tool, write a function `(context, **arguments) -> str` and register
it in `default_toolbox()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .memory import Memory, Person
from .reminders import REPEATS, ReminderError, ReminderService

log = logging.getLogger(__name__)

RECALL_LIMIT = 10


@dataclass(frozen=True)
class ToolContext:
    person: Person
    memory: Memory
    reminders: ReminderService | None = None
    timezone: str | None = None  # IANA name of the person's clock, when the client said it
    surface: str = ""  # where the person is talking from, and in which conversation:
    user_id: str = ""  # a reminder remembers them, to write its announcement there
    conversation: str = ""

    @property
    def origin(self) -> tuple[str, str, str]:
        return (self.surface, self.user_id, self.conversation)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[..., str]
    parameters: dict[str, dict]
    required: tuple[str, ...] = ()

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": list(self.required),
                },
            },
        }


class Toolbox:
    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}
        self.schemas = [tool.schema for tool in tools]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def run(self, name: str, context: ToolContext, arguments: dict[str, Any]) -> str:
        """Run a tool; whatever goes wrong comes back as text for the model to read."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}."
        try:
            return tool.function(context, **arguments)
        except (ValueError, TypeError) as error:
            return f"Error: {error}"
        except Exception:
            log.exception("tool %s crashed", name)
            return "Error: the tool failed."


def _remember(context: ToolContext, fact: str) -> str:
    stored = context.memory.add_fact(context.person.id, str(fact))
    if stored is None:
        return "Already known."
    return f"Remembered (id {stored.id})."


def _forget(context: ToolContext, fact_id: Any) -> str:
    try:
        number = int(fact_id)
    except (TypeError, ValueError):
        raise ValueError("fact_id must be the number shown in brackets.") from None
    if context.memory.delete_fact(context.person.id, number):
        return "Forgotten."
    return "No such fact for this person."


def _recall_facts(context: ToolContext, query: str) -> str:
    found = context.memory.search_facts(context.person.id, str(query), RECALL_LIMIT)
    if not found:
        return "No matching fact."
    return "\n".join(f"[{fact.id}] {fact.text}" for fact in found)


def _remind(context: ToolContext, text: str, when: str, repeat: str = "") -> str:
    if context.reminders is None:
        raise ValueError("Reminders are not available.")
    try:
        reminder = context.reminders.create(
            context.person, str(text), str(when), str(repeat or ""), context.timezone, context.origin
        )
    except ReminderError as error:
        raise ValueError(str(error)) from None
    again = f", then {reminder.repeat}" if reminder.repeat else ""
    return f"Reminder {reminder.id} set for {reminder.due_at.isoformat(timespec='seconds')} (UTC){again}."


def _list_reminders(context: ToolContext) -> str:
    if context.reminders is None:
        raise ValueError("Reminders are not available.")
    found = context.reminders.upcoming(context.person)
    if not found:
        return "No reminder set."
    return "\n".join(
        f"[{r.id}] {r.due_at.isoformat(timespec='seconds')} (UTC){' ' + r.repeat if r.repeat else ''}: {r.text}"
        for r in found
    )


def _cancel_reminder(context: ToolContext, reminder_id: Any) -> str:
    if context.reminders is None:
        raise ValueError("Reminders are not available.")
    try:
        number = int(reminder_id)
    except (TypeError, ValueError):
        raise ValueError("reminder_id must be the number shown in brackets.") from None
    return "Cancelled." if context.reminders.cancel(context.person, number) else "No such reminder of yours."


def default_toolbox() -> Toolbox:
    return Toolbox(
        [
            Tool(
                name="remember",
                description=(
                    "Save one durable fact about the person you are talking to "
                    "(preference, project, relative, constraint). One short sentence."
                ),
                function=_remember,
                parameters={"fact": {"type": "string", "description": "The fact to remember."}},
                required=("fact",),
            ),
            Tool(
                name="forget",
                description="Delete a remembered fact, by the id shown in brackets.",
                function=_forget,
                parameters={"fact_id": {"type": "integer", "description": "Id of the fact."}},
                required=("fact_id",),
            ),
            Tool(
                name="recall_facts",
                description=(
                    "Search the remembered facts of this person for words (any case), when the facts "
                    "shown to you say that older ones are not shown. Returns up to 10 facts, newest first."
                ),
                function=_recall_facts,
                parameters={"query": {"type": "string", "description": "Words to look for."}},
                required=("query",),
            ),
            Tool(
                name="remind",
                description=(
                    "Set a reminder: at that time its text is shown on EVERY client connected to Clara, "
                    "not only this person's, so write it for any reader."
                ),
                function=_remind,
                parameters={
                    "text": {"type": "string", "description": "What to announce."},
                    "when": {
                        "type": "string",
                        "description": "Local date and time, ISO 8601 without offset: 2026-10-05T09:00.",
                    },
                    "repeat": {"type": "string", "enum": list(REPEATS), "description": "Optional."},
                },
                required=("text", "when"),
            ),
            Tool(
                name="list_reminders",
                description="List this person's reminders that have not fired yet, with their ids.",
                function=_list_reminders,
                parameters={},
            ),
            Tool(
                name="cancel_reminder",
                description="Cancel one of this person's reminders, by id.",
                function=_cancel_reminder,
                parameters={"reminder_id": {"type": "integer", "description": "Id of the reminder."}},
                required=("reminder_id",),
            ),
        ]
    )
