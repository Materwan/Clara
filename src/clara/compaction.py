"""Shrinking a long conversation: the context estimate and the summary request.

When a conversation fills the model's context window, its older messages are
replaced by a summary written by the model itself (`Agent.compact`). The
messages stay in the database; the summary just stands for them from then on.
Nothing is ever dropped silently: a transcript too long for one request is
summarised in several chunks, each one continuing the summary of the previous.
"""

from __future__ import annotations

import json
import math

from .memory import StoredMessage

CHARS_PER_TOKEN = 3.5  # rough average for mixed prose and code
MAX_TRANSCRIPT_CHARS = 60_000  # most that one summary request carries
MIN_TRANSCRIPT_CHARS = 4_000
MESSAGE_CHARS = 1_500  # kept of each message when building the transcript
TOOL_RESULT_CHARS = 300
TOOL_ARGUMENT_CHARS = 200  # kept of each tool call's arguments

COMPACT_PROMPT = (
    "You are summarising a conversation between a user and an AI assistant so that the "
    "assistant can continue the work without the original messages.\n"
    "Write a factual summary (at most 800 words) that keeps: the user's goals and "
    "instructions and preferences; decisions taken; files that were read, created or "
    "modified; commands that were run and what they showed; errors and how they were solved; "
    "what remains to be done.\n"
    "Copy identifiers exactly as written, never paraphrase them: file paths, commands, "
    "names, ids, numbers, URLs, error messages.\n"
    "Write it in the language the user speaks. Do not add commentary or greetings."
)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def estimate_message_tokens(message: StoredMessage) -> int:
    """What a stored message adds to the context: its text, its prefix and its tool calls."""
    calls = json.dumps(message.tool_calls, ensure_ascii=False, default=str) if message.tool_calls else ""
    return estimate_tokens(message.content) + estimate_tokens(message.prefix) + estimate_tokens(calls)


def transcript_budget(window: int) -> int:
    """How many characters of transcript one summary request may carry for a model window."""
    return max(MIN_TRANSCRIPT_CHARS, min(MAX_TRANSCRIPT_CHARS, int(window * CHARS_PER_TOKEN * 0.4)))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _call_text(call: dict) -> str:
    function = call.get("function") or {}
    arguments = json.dumps(function.get("arguments") or {}, ensure_ascii=False, default=str)
    return f"{function.get('name', '?')}({_clip(arguments, TOOL_ARGUMENT_CHARS)})"


def message_line(message: StoredMessage) -> str:
    """One message as a line of transcript ("" when it says nothing)."""
    if message.role == "tool":
        return f"[{message.tool_name or 'tool'} result] {_clip(message.content, TOOL_RESULT_CHARS)}"
    if message.role == "assistant":
        said = _clip(message.content, MESSAGE_CHARS)
        calls = [_call_text(call) for call in message.tool_calls or []]
        if calls:
            said = (said + " " if said else "") + f"[called: {', '.join(calls)}]"
        return f"Assistant: {said}" if said else ""
    who = message.author or "User"
    return f"{who}: {_clip(message.content, MESSAGE_CHARS)}"  # not the prefix


def build_transcript(messages: list[StoredMessage]) -> str:
    """The messages as plain text for the summariser: long messages and tool results are cut."""
    return "\n".join(line for line in map(message_line, messages) if line)


def chunk_messages(messages: list[StoredMessage], budget: int = MAX_TRANSCRIPT_CHARS) -> list[list[StoredMessage]]:
    """The messages in consecutive groups whose transcripts each fit in `budget` characters."""
    chunks: list[list[StoredMessage]] = []
    current: list[StoredMessage] = []
    used = 0
    for message in messages:
        size = len(message_line(message))
        size = size + 1 if size else 0
        if current and used + size > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(message)
        used += size
    if current:
        chunks.append(current)
    return chunks


def summary_request(transcript: str, previous_summary: str = "", focus: str = "") -> list[dict]:
    """The messages that ask the model for the new summary."""
    request = ""
    if previous_summary:
        request += f"Summary of the conversation before these messages:\n{previous_summary}\n\n"
    request += f"Conversation:\n{transcript}"
    if focus:
        request += f"\n\nPay particular attention to: {focus}"
    return [
        {"role": "system", "content": COMPACT_PROMPT},
        {"role": "user", "content": request},
    ]
