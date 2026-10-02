"""The brain: one turn of conversation, whatever client it comes from.

    resolve the person -> wait for the conversation's turn -> build the prompt
    -> stream the model, running tools -> store the turn -> compact if too long

`Agent.turn()` is the only entry point and yields events (dicts), so the same
code serves streaming clients (events as they come) and plain ones (last event).

Tools come in two kinds. *Server tools* (`remember`, `forget`) run here. *Client
tools* are described by the client in its request and run on the client's machine
(files, shell...): when the model calls one, the turn emits a `tool_requests`
event and waits until the client posts the results (`submit_results`), then goes
on with the next model round. The client never has to open a second stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .compaction import (
    build_transcript,
    chunk_messages,
    estimate_message_tokens,
    estimate_tokens,
    summary_request,
    transcript_budget,
)
from .llm import LlmBackend, LlmChunk
from .memory import ConversationState, Memory, Person, StoredMessage, TurnRow
from .prompt import SystemPrompt
from .tools import Toolbox, ToolContext

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 40
MAX_FACTS_IN_PROMPT = 100
RECENT_TOOL_RESULTS_KEPT = 8  # older tool outputs are replaced by a note, to save context
OMITTED = "[output omitted to save context]"
DEFAULT_CONTEXT_WINDOW = 32_768


def now_in(timezone: str | None) -> datetime:
    """The current time in an IANA timezone ("Europe/Paris"), or the server's own."""
    return datetime.now(ZoneInfo(timezone)) if timezone else datetime.now().astimezone()


class NothingToCompact(Exception):
    """The conversation has no messages to summarise."""


class ClientToolTimeout(Exception):
    """The client did not send the results of its tools in time."""


class ModelTimeout(Exception):
    """The model stopped answering."""


async def with_idle_timeout(
    stream: AsyncIterator[LlmChunk], first: float, idle: float
) -> AsyncIterator[LlmChunk]:
    """The chunks of `stream`; ModelTimeout if none comes within `first` seconds, then `idle`
    seconds of each other (a model that has hung would hold its slot for ever)."""
    iterator = stream.__aiter__()
    wait = first
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), wait)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                what = "to start answering" if wait == first else "between two pieces of its answer"
                raise ModelTimeout(f"The model took more than {wait:g} seconds {what}.") from None
            wait = idle
            yield chunk
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


_END = object()


@dataclass(frozen=True)
class ChatRequest:
    surface: str  # "cli", "discord", "web"...: where the person is talking from
    user_id: str  # the person's id on that surface
    user_name: str | None
    message: str
    conversation: str | None = None  # default: a private thread per account
    tools: tuple[dict, ...] = ()  # client tools, as function schemas
    instructions: str = ""  # added to the system prompt (what the client is for)
    prefix: str = ""  # shown to the model before the message, never in summaries
    ephemeral: bool = False  # a one-shot job: no persona, no memory, nothing stored
    timezone: str | None = None  # IANA name for the date and time shown to the model (default: the server's)

    @property
    def conversation_id(self) -> str:
        return self.conversation or f"{self.surface}:{self.user_id}"


@dataclass
class _Pending:
    owner: str
    expected: frozenset[str]
    future: asyncio.Future


class Agent:
    def __init__(
        self,
        memory: Memory,
        backend: LlmBackend,
        toolbox: Toolbox,
        prompt: SystemPrompt,
        history_turns: int = 20,
        max_concurrent_llm: int = 2,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        context_window: int | Callable[[], int] = DEFAULT_CONTEXT_WINDOW,
        compact_percent: int = 80,
        keep_recent_turns: int = 2,
        transcript_chars: int | None = None,
        tool_timeout: float = 900.0,
        first_token_timeout: float = 300.0,
        idle_timeout: float = 120.0,
        clock: Callable[[str | None], datetime] = now_in,
    ):
        self.memory = memory
        self.backend = backend
        self.toolbox = toolbox
        self.prompt = prompt
        self.history_turns = history_turns
        self.max_tool_rounds = max_tool_rounds
        self.compact_percent = compact_percent
        self.keep_recent_turns = keep_recent_turns  # turns a compaction leaves as they are
        self._transcript_chars = transcript_chars  # characters per summary request (default: from the window)
        self.tool_timeout = tool_timeout
        self.first_token_timeout = first_token_timeout  # seconds before the model starts answering
        self.idle_timeout = idle_timeout  # seconds the model may pause once it has started
        self._clock = clock
        self._context_window = context_window
        self.stats = AgentStats()
        self.max_concurrent_llm = max_concurrent_llm
        self._llm_slots = asyncio.Semaphore(max_concurrent_llm)
        # One lock per conversation: two messages of the same thread are answered in
        # order, different threads run in parallel. Unused locks are garbage collected.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._pending: dict[str, _Pending] = {}

    def _conversation_lock(self, conversation: str) -> asyncio.Lock:
        lock = self._locks.get(conversation)
        if lock is None:
            lock = self._locks[conversation] = asyncio.Lock()
        return lock

    @property
    def window(self) -> int:
        return self._context_window() if callable(self._context_window) else self._context_window

    # ------------------------------------------------------------------
    # Client tools
    # ------------------------------------------------------------------
    def validate(self, request: ChatRequest) -> None:
        """Raise ValueError if the request's tools are unusable (checked before streaming)."""
        if request.timezone:
            try:
                ZoneInfo(request.timezone)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise ValueError(f"Unknown timezone: {request.timezone}") from None
        names: set[str] = set()
        for schema in request.tools:
            function = schema.get("function") if isinstance(schema, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("Each tool needs a function name.")
            if name in names or (not request.ephemeral and name in self.toolbox.names):
                raise ValueError(f"Tool name used twice, or reserved by the server: {name}")
            names.add(name)

    def submit_results(self, turn_id: str, owner: str, results: dict[str, str]) -> None:
        """Hand the client's tool results to the turn that waits for them.

        KeyError: no such turn waiting (or it belongs to another client).
        ValueError: the results do not match the calls that were requested.
        """
        pending = self._pending.get(turn_id)
        if pending is None or pending.owner != owner or pending.future.done():
            raise KeyError(turn_id)
        if set(results) != pending.expected:
            raise ValueError(f"Expected results for exactly: {', '.join(sorted(pending.expected))}")
        pending.future.set_result(results)

    async def _wait_for_client(self, turn_id: str) -> dict[str, str]:
        pending = self._pending[turn_id]
        try:
            return await asyncio.wait_for(pending.future, self.tool_timeout)
        except asyncio.TimeoutError:
            raise ClientToolTimeout("The client did not return its tool results in time.") from None
        finally:
            self._pending.pop(turn_id, None)

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------
    @staticmethod
    def _user_content(text: str, prefix: str = "", author: str = "") -> str:
        content = f"{prefix}\n\n{text}" if prefix else text
        return f"{author}: {content}" if author else content

    def _replay(self, stored: list[StoredMessage], person: Person) -> list[dict]:
        """Past messages as the model wants them. Old tool outputs are cut to save context."""
        tool_rows = [i for i, message in enumerate(stored) if message.role == "tool"]
        stale = set(tool_rows[: max(0, len(tool_rows) - RECENT_TOOL_RESULTS_KEPT)])
        messages: list[dict] = []
        for index, message in enumerate(stored):
            if message.role == "user":
                other = message.person_id != person.id and message.author
                content = self._user_content(message.content, message.prefix, message.author if other else "")
                messages.append({"role": "user", "content": content})
            elif message.role == "assistant":
                entry: dict = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    entry["tool_calls"] = message.tool_calls
                messages.append(entry)
            else:
                content = OMITTED if index in stale else message.content
                messages.append({"role": "tool", "tool_name": message.tool_name, "content": content})
        return messages

    def _build_messages(self, request: ChatRequest, person: Person, state: ConversationState) -> list[dict]:
        messages: list[dict] = []
        now = self._clock(request.timezone)
        if request.ephemeral:
            if request.instructions.strip():
                messages.append({"role": "system", "content": request.instructions.strip()})
        else:
            facts = self.memory.facts(person.id, MAX_FACTS_IN_PROMPT)
            system = self.prompt.render(
                person, request.surface, facts, now, request.instructions, state.summary,
            )
            messages.append({"role": "system", "content": system})
            stored = self.memory.history(request.conversation_id, self.history_turns, state.upto_id)
            messages.extend(self._replay(stored, person))
        content = self._user_content(request.message, request.prefix)
        if not request.ephemeral:
            # Only here, never stored: replayed history and system prompt stay identical between turns
            content = f"[time: {now.strftime('%H:%M')}]\n\n{content}"
        messages.append({"role": "user", "content": content})
        return messages

    # ------------------------------------------------------------------
    # A turn
    # ------------------------------------------------------------------
    async def turn(self, request: ChatRequest, owner: str = "") -> AsyncIterator[dict]:
        """Run one turn.

        Events: `turn` (its id), `token`, `tool` (a server tool ran), `tool_requests` (the client
        must run these and answer through `submit_results`), `usage` (one model round),
        `compacted`, `warning`, then a final `done`.
        """
        self.stats.active += 1
        self.stats.turns += 1
        try:
            # aclosing: if the client goes away, the turn is closed now (its lock and its
            # pending tool request released), not whenever the garbage collector gets to it
            async with contextlib.aclosing(self._turn(request, owner)) as events:
                async for event in events:
                    if event["type"] == "done":
                        self.stats.prompt_tokens += event["usage"]["prompt_tokens"]
                        self.stats.completion_tokens += event["usage"]["completion_tokens"]
                    yield event
        finally:
            self.stats.active -= 1

    async def _turn(self, request: ChatRequest, owner: str) -> AsyncIterator[dict]:
        self.validate(request)
        person = self.memory.resolve(request.surface, request.user_id, request.user_name)
        conversation = request.conversation_id
        ephemeral = request.ephemeral
        context = ToolContext(person, self.memory)
        client_tools = {schema["function"]["name"] for schema in request.tools}
        schemas = ([] if ephemeral else list(self.toolbox.schemas)) + list(request.tools)
        turn_id = uuid.uuid4().hex
        yield {"type": "turn", "id": turn_id}

        lock = contextlib.nullcontext() if ephemeral else self._conversation_lock(conversation)
        async with lock:
            state = ConversationState() if ephemeral else self.memory.state(conversation)
            if not ephemeral and self._history_overflows(conversation, state):
                # Older turns would fall out of the prompt without ever being summarised: summarise
                # them now, and keep half the history so this does not happen at every turn.
                try:
                    before, after = await self._compact_locked(
                        conversation, keep_recent_turns=max(1, self.history_turns // 2)
                    )
                    yield {"type": "compacted", "before": before, "after": after}
                except Exception as error:
                    log.warning("compaction of %s before the turn failed: %s", conversation, error)
                    yield {"type": "warning", "message": f"Could not compact the conversation: {error}"}
                state = self.memory.state(conversation)  # also after a failure: it may have advanced
            messages = self._build_messages(request, person, state)
            rows: list[TurnRow] = []
            reply_parts: list[str] = []
            tools_used: list[str] = []
            prompt_tokens = completion_tokens = context_tokens = 0

            for round_number in range(self.max_tool_rounds + 1):
                offer_tools = round_number < self.max_tool_rounds  # the last round must answer
                text_parts: list[str] = []
                calls = []
                round_prompt = round_completion = 0
                # aclosing: if the client goes away at a yield, the model task stops now
                async with contextlib.aclosing(self._model(messages, schemas if offer_tools else None)) as model:
                    async for chunk in model:
                        round_prompt += chunk.prompt_tokens
                        round_completion += chunk.completion_tokens
                        calls.extend(chunk.tool_calls)
                        if chunk.text:
                            text_parts.append(chunk.text)
                            yield {"type": "token", "text": chunk.text}
                prompt_tokens += round_prompt
                completion_tokens += round_completion
                context_tokens = round_prompt + round_completion or context_tokens
                yield {"type": "usage", "prompt_tokens": round_prompt, "completion_tokens": round_completion}
                text = "".join(text_parts)
                reply_parts.append(text)

                if not (offer_tools and calls):
                    if text.strip():
                        rows.append(TurnRow("assistant", text))
                    break

                ids = [f"call_{round_number}_{index}" for index in range(len(calls))]
                call_dicts = [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]
                messages.append({"role": "assistant", "content": text, "tool_calls": call_dicts})
                rows.append(TurnRow("assistant", text, call_dicts))

                results: dict[str, str] = {}
                remote = []
                for call_id, call in zip(ids, calls):
                    tools_used.append(call.name)
                    if call.name in client_tools:
                        remote.append((call_id, call))
                    else:
                        yield {"type": "tool", "name": call.name}
                        results[call_id] = self.toolbox.run(call.name, context, call.arguments)
                if remote:
                    self._pending[turn_id] = _Pending(
                        owner, frozenset(call_id for call_id, _ in remote),
                        asyncio.get_running_loop().create_future(),
                    )
                    try:
                        yield {
                            "type": "tool_requests",
                            "turn": turn_id,
                            "calls": [
                                {"id": call_id, "name": call.name, "arguments": call.arguments}
                                for call_id, call in remote
                            ],
                        }
                        results.update(await self._wait_for_client(turn_id))
                    finally:
                        self._pending.pop(turn_id, None)
                for call_id, call in zip(ids, calls):
                    messages.append({"role": "tool", "tool_name": call.name, "content": results[call_id]})
                    rows.append(TurnRow("tool", results[call_id], tool_name=call.name))

            reply = "".join(reply_parts).strip()
            if not ephemeral and (reply or rows):  # an empty answer would only pollute the history
                self.memory.add_turn(conversation, person.id, request.message, rows, request.prefix)
                self.memory.set_context_tokens(conversation, context_tokens)
                if self.compact_percent and 100 * context_tokens / self.window >= self.compact_percent:
                    try:
                        before, after = await self._compact_locked(conversation)
                        context_tokens = self.memory.state(conversation).context_tokens
                        yield {"type": "compacted", "before": before, "after": after}
                    except Exception as error:
                        log.warning("automatic compaction of %s failed: %s", conversation, error)
                        yield {"type": "warning", "message": f"Could not compact the conversation: {error}"}

        yield {
            "type": "done",
            "reply": reply,
            "conversation": conversation,
            "person": {"id": person.id, "name": person.name},
            "tools": tools_used,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "context": self._context_info(context_tokens),
            "model": getattr(self.backend, "model", ""),  # may change between turns (/provider)
            "provider": getattr(self.backend, "active", ""),
        }

    async def _model(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[LlmChunk]:
        """One model round. A task reads the model and holds a slot while it works, and passes
        the chunks on through a queue: a client that reads slowly (or not at all) cannot keep a
        slot busy, and a model that hangs times out."""
        queue: asyncio.Queue = asyncio.Queue()

        async def produce() -> None:
            try:
                async with self._llm_slots:
                    stream = self.backend.stream(messages, tools)
                    async for chunk in with_idle_timeout(stream, self.first_token_timeout, self.idle_timeout):
                        queue.put_nowait(chunk)
                queue.put_nowait(_END)
            except Exception as error:
                queue.put_nowait(error)

        producer = asyncio.ensure_future(produce())
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            producer.cancel()  # the consumer left (or failed): stop reading, free the slot
            with contextlib.suppress(BaseException):
                await producer

    def _history_overflows(self, conversation: str, state: ConversationState) -> bool:
        """Are there more turns waiting than the prompt takes back (`history_turns`)?"""
        return bool(self.compact_percent) and (
            self.memory.turns_after(conversation, state.upto_id) > self.history_turns
        )

    def _context_info(self, tokens: int) -> dict:
        window = self.window
        return {"tokens": tokens, "window": window, "percent": round(100 * tokens / window, 1)}

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------
    def context(self, conversation: str) -> dict:
        """Size of a conversation's context, and its summary if it has one."""
        state = self.memory.state(conversation)
        return {
            "conversation": conversation,
            "summary": state.summary,
            "messages": len(self.memory.messages_after(conversation, state.upto_id)),
            **self._context_info(state.context_tokens),
        }

    async def compact(self, conversation: str, focus: str = "") -> tuple[float, float]:
        """Replace the older messages of a conversation by a summary.

        The last `keep_recent_turns` turns stay as they are. Returns the context usage in
        percent before and after. NothingToCompact if there is nothing to summarise.
        """
        async with self._conversation_lock(conversation):
            return await self._compact_locked(conversation, focus)

    async def _summarise(self, transcript: str, previous: str, focus: str) -> str:
        parts: list[str] = []
        async with contextlib.aclosing(self._model(summary_request(transcript, previous, focus), None)) as model:
            async for chunk in model:
                parts.append(chunk.text)
        summary = "".join(parts).strip()
        if not summary:
            raise RuntimeError("the model returned an empty summary")
        return summary

    @staticmethod
    def _split_recent(rows: list[StoredMessage], keep_turns: int) -> tuple[list[StoredMessage], list[StoredMessage]]:
        """(rows to summarise, rows kept as they are): the kept ones start at the user message
        `keep_turns` from the end. With fewer turns than that, everything is summarised."""
        user_rows = [index for index, row in enumerate(rows) if row.role == "user"]
        if keep_turns <= 0 or len(user_rows) <= keep_turns:
            return rows, []
        boundary = user_rows[-keep_turns]
        return rows[:boundary], rows[boundary:]

    async def _compact_locked(
        self, conversation: str, focus: str = "", keep_recent_turns: int | None = None
    ) -> tuple[float, float]:
        keep = self.keep_recent_turns if keep_recent_turns is None else keep_recent_turns
        state = self.memory.state(conversation)
        rows = self.memory.messages_after(conversation, state.upto_id)
        if not rows:
            raise NothingToCompact("the conversation is empty: nothing to compact")
        old, kept = self._split_recent(rows, keep)

        # Summarise chunk by chunk. Each step is saved, so a failure halfway leaves a coherent
        # conversation (the summary so far, then the messages not yet summarised).
        summary = state.summary
        for chunk in chunk_messages(old, self._transcript_chars or transcript_budget(self.window)):
            transcript = build_transcript(chunk)
            if transcript:
                summary = await self._summarise(transcript, summary, focus)
            self.memory.set_summary(conversation, summary, chunk[-1].id, state.context_tokens)

        # What remains in the context: the fixed part (system prompt, tools), the summary, and the
        # messages kept. The fixed part is what the last measure had beyond summary and messages.
        measured = estimate_tokens(state.summary) + sum(estimate_message_tokens(row) for row in rows)
        fixed = max(0, state.context_tokens - measured)
        after_tokens = fixed + estimate_tokens(summary) + sum(estimate_message_tokens(row) for row in kept)
        self.memory.set_summary(conversation, summary, old[-1].id, after_tokens)
        window = self.window
        return 100 * state.context_tokens / window, 100 * after_tokens / window


@dataclass
class AgentStats:
    """Counters since the server started (shown by the console's /status)."""

    turns: int = 0
    active: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
