"""The brain: one turn of conversation, whatever client it comes from.

    resolve the person -> wait for the conversation's turn -> build the prompt
    -> stream the model, running tools -> store the exchange

`Agent.turn()` is the only entry point and yields events (dicts), so the same
code serves streaming clients (events as they come) and plain ones (last event).
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

from .llm import LlmBackend
from .memory import Memory, Person
from .prompt import SystemPrompt
from .tools import Toolbox, ToolContext

MAX_TOOL_ROUNDS = 5
MAX_FACTS_IN_PROMPT = 100


@dataclass(frozen=True)
class ChatRequest:
    surface: str  # "cli", "discord", "web"...: where the person is talking from
    user_id: str  # the person's id on that surface
    user_name: str | None
    message: str
    conversation: str | None = None  # default: a private thread per account

    @property
    def conversation_id(self) -> str:
        return self.conversation or f"{self.surface}:{self.user_id}"


@dataclass
class AgentStats:
    """Counters since the server started (shown by the console's /status)."""

    turns: int = 0
    active: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Agent:
    def __init__(
        self,
        memory: Memory,
        backend: LlmBackend,
        toolbox: Toolbox,
        prompt: SystemPrompt,
        history_messages: int = 20,
        max_concurrent_llm: int = 2,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ):
        self.memory = memory
        self.backend = backend
        self.toolbox = toolbox
        self.prompt = prompt
        self.history_messages = history_messages
        self.max_tool_rounds = max_tool_rounds
        self.stats = AgentStats()
        self._llm_slots = asyncio.Semaphore(max_concurrent_llm)
        # One lock per conversation: two messages of the same thread are answered in
        # order, different threads run in parallel. Unused locks are garbage collected.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _conversation_lock(self, conversation: str) -> asyncio.Lock:
        lock = self._locks.get(conversation)
        if lock is None:
            lock = self._locks[conversation] = asyncio.Lock()
        return lock

    def _build_messages(self, request: ChatRequest, person: Person) -> list[dict]:
        facts = self.memory.facts(person.id, MAX_FACTS_IN_PROMPT)
        system = self.prompt.render(person, request.surface, facts, datetime.now().astimezone())
        messages: list[dict] = [{"role": "system", "content": system}]
        for stored in self.memory.history(request.conversation_id, self.history_messages):
            content = stored.content
            if stored.role == "user" and stored.person_id != person.id and stored.author:
                content = f"{stored.author}: {content}"  # someone else spoke in this thread
            messages.append({"role": stored.role, "content": content})
        messages.append({"role": "user", "content": request.message})
        return messages

    async def turn(self, request: ChatRequest) -> AsyncIterator[dict]:
        """Run one turn. Events: `token`, `tool`, then a final `done`."""
        self.stats.active += 1
        self.stats.turns += 1
        try:
            async for event in self._turn(request):
                if event["type"] == "done":
                    self.stats.prompt_tokens += event["usage"]["prompt_tokens"]
                    self.stats.completion_tokens += event["usage"]["completion_tokens"]
                yield event
        finally:
            self.stats.active -= 1

    async def _turn(self, request: ChatRequest) -> AsyncIterator[dict]:
        person = self.memory.resolve(request.surface, request.user_id, request.user_name)
        conversation = request.conversation_id
        context = ToolContext(person, self.memory)

        async with self._conversation_lock(conversation), self._llm_slots:
            messages = self._build_messages(request, person)
            reply_parts: list[str] = []
            tools_used: list[str] = []
            prompt_tokens = completion_tokens = 0

            for round_number in range(self.max_tool_rounds + 1):
                offer_tools = round_number < self.max_tool_rounds  # the last round must answer
                text_parts: list[str] = []
                calls = []
                async for chunk in self.backend.stream(
                    messages, self.toolbox.schemas if offer_tools else None
                ):
                    prompt_tokens += chunk.prompt_tokens
                    completion_tokens += chunk.completion_tokens
                    calls.extend(chunk.tool_calls)
                    if chunk.text:
                        text_parts.append(chunk.text)
                        yield {"type": "token", "text": chunk.text}
                reply_parts.extend(text_parts)

                if not (offer_tools and calls):
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "tool_calls": [
                            {"function": {"name": call.name, "arguments": call.arguments}}
                            for call in calls
                        ],
                    }
                )
                for call in calls:
                    yield {"type": "tool", "name": call.name}
                    tools_used.append(call.name)
                    output = self.toolbox.run(call.name, context, call.arguments)
                    messages.append({"role": "tool", "tool_name": call.name, "content": output})

            reply = "".join(reply_parts).strip()
            if reply:  # an empty answer would only pollute the history
                self.memory.add_exchange(conversation, person.id, request.message, reply)

        yield {
            "type": "done",
            "reply": reply,
            "conversation": conversation,
            "person": {"id": person.id, "name": person.name},
            "tools": tools_used,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
