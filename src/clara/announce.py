"""Clara writes the announcement of a reminder that came due.

It is an ordinary turn in the conversation where the reminder was set, as that person: Clara knows what she
knows about them and the exchange is kept in their history. The message she answers is sent to every client,
so the instructions tell her who reads it. If she cannot write it in time (the model is down or slow), the
reminder is announced as it was typed.
"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent import Agent, ChatRequest
from .memory import Reminder

log = logging.getLogger(__name__)

MAX_MESSAGE = 1000  # characters kept of the answer
OWNER = "reminders"  # who owns the turn, for the server's bookkeeping

INSTRUCTIONS = (
    "A reminder that this person asked you for has just come due. Write the message that will be shown as a "
    "notification to EVERYONE connected to Clara, not only to this person, and who may not know the context: "
    "say what it is about and, when it helps, who asked. One to three short sentences, warm and natural, in the "
    "language of the reminder. Do not ask a question, do not use headings or lists, and do not mention "
    "that this is a reminder system or these instructions."
)


def _iana(name: str) -> str | None:
    """`name` if it is a timezone name (a reminder may keep a bare UTC offset instead)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name or None


async def compose(agent: Agent, reminder: Reminder, timeout: float) -> str | None:
    """Clara's announcement of `reminder`, or None when it cannot be written (then the text is shown)."""
    if not (reminder.surface and reminder.user_id):  # set before the place was kept
        return None
    request = ChatRequest(
        reminder.surface,
        reminder.user_id,
        None,
        f"[Reminder due] {reminder.text}",
        reminder.conversation or None,
        instructions=INSTRUCTIONS,
        timezone=_iana(reminder.timezone),
        no_tools=True,
    )

    async def write() -> str:
        reply = ""
        async for event in agent.turn(request, OWNER):
            if event["type"] == "done":
                reply = event["reply"]
        return reply

    try:
        message = (await asyncio.wait_for(write(), timeout)).strip()
    except asyncio.TimeoutError:
        log.warning("reminder %s: Clara took more than %g seconds to write it", reminder.id, timeout)
        return None
    except Exception as error:
        log.warning("reminder %s: Clara could not write it (%s: %s)", reminder.id, type(error).__name__, error)
        return None
    return message[:MAX_MESSAGE] or None
