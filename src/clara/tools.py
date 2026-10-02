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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    person: Person
    memory: Memory


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
        ]
    )
