"""Shrinking a long conversation: the context estimate and the summary request.

When a conversation fills the model's context window, its older messages are
replaced by a summary written by the model itself (`Agent.compact`). The
messages stay in the database; the summary just stands for them from then on.
"""

from __future__ import annotations

import math

from .memory import StoredMessage

CHARS_PER_TOKEN = 3.5  # rough average for mixed prose and code
MAX_TRANSCRIPT_CHARS = 60_000
MESSAGE_CHARS = 1_500  # kept of each message when building the transcript
TOOL_RESULT_CHARS = 300

COMPACT_PROMPT = (
    "You are summarising a conversation between a user and an AI assistant so that the "
    "assistant can continue the work without the original messages.\n"
    "Write a factual summary (at most 500 words) that keeps: the user's goals and "
    "instructions and preferences; decisions taken; files that were read, created or "
    "modified (with their paths); commands that were run and what they showed; errors and "
    "how they were solved; what remains to be done.\n"
    "Write it in the language the user speaks. Do not add commentary or greetings."
)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def build_transcript(messages: list[StoredMessage], budget: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The messages as plain text for the summariser: long messages and tool results are
    cut, and the oldest ones are dropped when it is still too long."""
    lines: list[str] = []
    for message in messages:
        if message.role == "tool":
            lines.append(f"[{message.tool_name or 'tool'} result] {_clip(message.content, TOOL_RESULT_CHARS)}")
        elif message.role == "assistant":
            calls = [
                (call.get("function") or {}).get("name", "?") for call in message.tool_calls or []
            ]
            said = _clip(message.content, MESSAGE_CHARS)
            if calls:
                said = (said + " " if said else "") + f"[called: {', '.join(calls)}]"
            if said:
                lines.append(f"Assistant: {said}")
        elif message.role == "user":
            who = message.author or "User"
            lines.append(f"{who}: {_clip(message.content, MESSAGE_CHARS)}")  # not the prefix

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > budget:
            kept.append("[earlier messages omitted]")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(reversed(kept))


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
