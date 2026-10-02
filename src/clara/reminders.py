"""Reminders: a text and a moment, announced to every connected client when the moment comes.

    create()   a client (or the model, through a tool) sets one
    run()      the scheduler: when one is due it becomes an *event*, and a repeating one is moved on
    events()   one client's stream of events, oldest first

When one comes due, Clara writes the announcement herself (see announce.py) and that message is what the
clients show; if she cannot, they show the reminder's own text.

The same stream also tells every client what the server is doing (`server` events: running, stopping,
stopped), so that they can say "Clara is not running" instead of just failing.

Events are stored, and each client has a cursor (how far it has read). A client that connects
after a reminder fired, because it was off or offline at that moment, is sent what it missed;
a client seeing the server for the first time starts from now. Two connections of one client
(same token) both get what fires while they are open, and share the cursor.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, AsyncIterator, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .memory import Memory, Person, Reminder, ReminderEvent

log = logging.getLogger(__name__)

REPEATS = ("daily", "weekly", "monthly")
MAX_TEXT = 500
MAX_PER_PERSON = 100
EVENT_RETENTION = timedelta(days=7)  # a client offline longer than this misses the reminder
MAX_SLEEP = 30.0  # seconds: the scheduler looks again at least this often (clock changes, safety net)
BATCH = 100  # events read from the database at a time

SERVER_MESSAGES = {
    "running": "Clara is running",
    "stopping": "Clara is stopping: she finishes what is running and takes nothing new",
    "stopped": "Clara is not running",
}

# Writes the announcement of a reminder that came due: its text, or None to announce the reminder as it is
Composer = Callable[[Reminder], Awaitable["str | None"]]

_OFFSET = re.compile(r"^([+-])(\d\d):(\d\d)$")


class ReminderError(ValueError):
    """The reminder cannot be created as asked (the message says why)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tzinfo(spec: str) -> tzinfo:
    """A clock from its name: an IANA zone ("Europe/Paris"), an offset ("+02:00"), or UTC for ""."""
    if not spec:
        return timezone.utc
    match = _OFFSET.match(spec)
    if match:
        delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
        return timezone(delta if match[1] == "+" else -delta)
    try:
        return ZoneInfo(spec)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ReminderError(f"Unknown timezone: {spec}") from None


def _offset_name(moment: datetime) -> str:
    seconds = int(moment.utcoffset().total_seconds())  # type: ignore[union-attr]
    sign = "-" if seconds < 0 else "+"
    minutes = abs(seconds) // 60
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def parse_moment(at: str, zone: str | None) -> tuple[datetime, str]:
    """`(UTC moment, the clock a repeat keeps)` from an ISO 8601 text such as `2026-10-05T09:00`
    or `2026-10-05T09:00+02:00`. Without an offset the time is read in `zone` (an IANA name), or
    in the server's own timezone. The clock is `zone` if given, else the offset of the time."""
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError:
        raise ReminderError(
            f"Cannot read the time {at!r}: use ISO 8601, like 2026-10-05T09:00 or 2026-10-05T09:00+02:00."
        ) from None
    if zone:
        _tzinfo(zone)  # validates the name
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tzinfo(zone)) if zone else moment.astimezone()
    return moment.astimezone(timezone.utc), zone or _offset_name(moment)


def _shift(local: datetime, repeat: str, count: int) -> datetime:
    """`local` moved `count` periods on, keeping the wall-clock time (and the day of the month,
    clamped to the length of the month)."""
    if repeat == "daily":
        return local + timedelta(days=count)
    if repeat == "weekly":
        return local + timedelta(weeks=count)
    years, month = divmod(local.month - 1 + count, 12)
    year, month = local.year + years, month + 1
    return local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))


def next_occurrence(reminder: Reminder, after: datetime) -> datetime:
    """The first moment of a repeating reminder strictly after `after`. Occurrences are counted from
    the first one, so "monthly on the 31st" is the 28th in February and the 31st again in March."""
    zone = _tzinfo(reminder.timezone)
    anchor = reminder.anchor_at.astimezone(zone)
    count = 0
    while True:
        count += 1
        candidate = _shift(anchor, reminder.repeat, count).astimezone(timezone.utc)
        if candidate > after:
            return candidate


def describe(reminder: Reminder) -> dict[str, Any]:
    return {
        "id": reminder.id,
        "text": reminder.text,
        "due_at": reminder.due_at.isoformat(timespec="seconds"),
        "repeat": reminder.repeat,
    }


def event_payload(event: ReminderEvent) -> dict[str, Any]:
    return {
        "type": "reminder",
        "id": event.id,
        "text": event.text,
        "due_at": event.due_at,
        "fired_at": event.fired_at,
        "from": event.author,
        "message": event.message,  # what Clara wrote; None: show `text`
    }


class ReminderService:
    def __init__(self, memory: Memory, clock: Callable[[], datetime] = _utc_now):
        self.memory = memory
        self._clock = clock
        self._wake = asyncio.Event()  # something changed: the scheduler looks again
        self._listeners: set[asyncio.Event] = set()  # one per open client stream
        self.composer: Composer | None = None  # who writes the announcements (None: the text is announced)
        self.stopping = False  # the server is stopping: what comes due waits for the next start
        self.firing = False  # announcements are being written
        self.server_state = "running"

    # -- setting and cancelling ------------------------------------------------------------- #

    def create(
        self,
        person: Person,
        text: str,
        at: str,
        repeat: str = "",
        zone: str | None = None,
        origin: tuple[str, str, str] = ("", "", ""),
    ) -> Reminder:
        """Set a reminder. `origin` is (surface, user_id, conversation) of where it was asked: Clara writes
        the announcement there. Raises :class:`ReminderError` when it is invalid or in the past."""
        text = " ".join((text or "").split())
        if not text:
            raise ReminderError("A reminder needs a text.")
        if len(text) > MAX_TEXT:
            raise ReminderError(f"A reminder is at most {MAX_TEXT} characters long.")
        repeat = (repeat or "").strip().lower()
        if repeat and repeat not in REPEATS:
            raise ReminderError(f"repeat must be one of: {', '.join(REPEATS)} (or nothing).")
        due, clock = parse_moment(at, zone)
        if due <= self._clock():
            raise ReminderError("That moment is already past.")
        if self.memory.reminder_count(person.id) >= MAX_PER_PERSON:
            raise ReminderError(f"At most {MAX_PER_PERSON} reminders at a time: cancel some first.")
        reminder = self.memory.add_reminder(person.id, text, due, repeat, clock, origin)
        self._wake.set()
        return reminder

    def upcoming(self, person: Person) -> list[Reminder]:
        return self.memory.reminders_of(person.id)

    def cancel(self, person: Person, reminder_id: int) -> bool:
        cancelled = self.memory.delete_reminder(person.id, reminder_id)
        if cancelled:
            self._wake.set()
        return cancelled

    # -- the scheduler ------------------------------------------------------------------------ #

    async def fire_due(self) -> int:
        """Announce every reminder that is due: Clara writes each announcement (all at once), then it is
        stored and sent. A repeating reminder fires once however many occurrences it missed (the server
        was down), then waits for its next one. Returns how many fired."""
        due = self.memory.due_reminders(self._clock())
        if not due:
            return 0
        self.firing = True
        try:
            messages = await asyncio.gather(*(self._compose(reminder) for reminder in due))
            now = self._clock()  # after the writing: that is when the clients get it
            fired = 0
            for reminder, message in zip(due, messages):
                if not self.memory.reminder_exists(reminder.id):  # cancelled while Clara was writing
                    continue
                following = next_occurrence(reminder, now) if reminder.repeat else None
                self.memory.fire_reminder(reminder, following, now, message)
                fired += 1
            if fired:
                self.memory.prune_reminder_events(now - EVENT_RETENTION)
                for listener in self._listeners:
                    listener.set()
            return fired
        finally:
            self.firing = False

    async def _compose(self, reminder: Reminder) -> str | None:
        if self.composer is None or self.stopping:
            return None
        try:
            return await self.composer(reminder)
        except Exception:
            log.exception("reminders: could not write the announcement of reminder %s", reminder.id)
            return None

    def announce_server(self, state: str) -> None:
        """Tell every connected client what the server is doing ("stopping", "stopped")."""
        self.server_state = state
        for listener in self._listeners:
            listener.set()

    async def run(self) -> None:
        """The scheduler loop; runs for the life of the server."""
        while True:
            self._wake.clear()  # before looking: a reminder set meanwhile wakes the next wait at once
            try:
                if not self.stopping:
                    await self.fire_due()
            except Exception:
                log.exception("reminders: could not fire the due ones")
            upcoming = self.memory.next_reminder_due()
            delay = MAX_SLEEP if upcoming is None or self.stopping else (upcoming - self._clock()).total_seconds()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.0, min(delay, MAX_SLEEP)))

    # -- one client's stream ------------------------------------------------------------------- #

    async def events(self, client: str) -> AsyncIterator[dict[str, Any]]:
        """What fires from now on, preceded by what fired since this client last connected, and the
        state of the server whenever it changes (first of all when the client connects)."""
        wake = asyncio.Event()
        self._listeners.add(wake)
        told = ""
        try:
            cursor = self.memory.reminder_cursor(client)
            if cursor is None:  # first time: no backlog
                cursor = self.memory.last_reminder_event()
                self.memory.set_reminder_cursor(client, cursor)
            while True:
                wake.clear()  # before reading: an event stored meanwhile is not lost
                batch = self.memory.reminder_events_after(cursor, BATCH)
                for event in batch:
                    yield event_payload(event)
                    cursor = event.id
                    self.memory.set_reminder_cursor(client, cursor)
                if len(batch) == BATCH:
                    continue
                state = self.server_state
                if state != told:
                    told = state
                    yield {"type": "server", "state": state, "message": SERVER_MESSAGES[state]}
                if state == "stopped":
                    return  # everything was delivered: the connection can close
                await wake.wait()
        finally:
            self._listeners.discard(wake)
