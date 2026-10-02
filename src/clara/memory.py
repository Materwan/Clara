"""The one memory, in one SQLite file. Only the server process opens it.

    people(id, name)                        one row per real person
    accounts(surface, external_id, person)  "discord:1234" and "cli:erwan" can be the same person
    facts(id, person, text)                 what Clara knows about a person (shared by every surface)
    messages(id, conversation, person, role, content)
                                            conversation history, one thread per conversation id

Facts follow the *person*, history follows the *conversation*: Clara knows you
are the same human on every surface, but a Discord channel and a terminal
session stay separate threads.

The connection is shared by all requests, guarded by a lock; every operation
is a few milliseconds, so it is called directly from async code.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_FACT_LENGTH = 300
MAX_NAME_LENGTH = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    surface     TEXT NOT NULL,
    external_id TEXT NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people (id),
    PRIMARY KEY (surface, external_id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_person ON accounts (person_id);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_unique ON facts (person_id, lower(text));
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    person_id    INTEGER REFERENCES people (id),
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    prefix       TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT,
    tool_name    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation, id);
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation   TEXT PRIMARY KEY,
    summary        TEXT NOT NULL DEFAULT '',
    upto_id        INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release: databases created before have to get them.
_ADDED_COLUMNS = {
    "prefix": "TEXT NOT NULL DEFAULT ''",
    "tool_calls": "TEXT",
    "tool_name": "TEXT",
}


class MergeRefused(ValueError):
    """Both people already have facts or history: merging them is irreversible."""


@dataclass(frozen=True)
class Person:
    id: int
    name: str


@dataclass(frozen=True)
class PersonSummary:
    person: Person
    accounts: list[str]  # "surface:external_id"
    facts: int


@dataclass(frozen=True)
class Fact:
    id: int
    text: str


@dataclass(frozen=True)
class StoredMessage:
    role: str  # "user", "assistant" or "tool"
    content: str
    person_id: int | None
    author: str | None  # display name of the person who wrote it (user messages)
    id: int = 0
    prefix: str = ""  # context the client put before a user message (kept for the model, not shown)
    tool_calls: list[dict] | None = None  # assistant messages: [{"function": {"name", "arguments"}}]
    tool_name: str | None = None  # tool messages: which tool answered


@dataclass(frozen=True)
class TurnRow:
    """A message produced during a turn, after the user's: the answer, a tool call, a result."""

    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_name: str | None = None


@dataclass(frozen=True)
class ConversationState:
    summary: str = ""  # replaces every message up to `upto_id`
    upto_id: int = 0
    context_tokens: int = 0  # size of the context at the end of the last turn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class Memory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        present = {row["name"] for row in self._db.execute("PRAGMA table_info(messages)")}
        for column, definition in _ADDED_COLUMNS.items():
            if column not in present:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()

    # ------------------------------------------------------------------
    # People and accounts
    # ------------------------------------------------------------------
    def find_person(self, surface: str, external_id: str) -> Person | None:
        with self._lock:
            row = self._db.execute(
                "SELECT p.id, p.name FROM accounts a JOIN people p ON p.id = a.person_id"
                " WHERE a.surface = ? AND a.external_id = ?",
                (surface, external_id),
            ).fetchone()
        return Person(row["id"], row["name"]) if row else None

    def resolve(self, surface: str, external_id: str, name: str | None = None) -> Person:
        """The person behind an account; a new person is created on first contact."""
        with self._lock, self._db:
            person = self.find_person(surface, external_id)
            if person:
                return person
            display = _one_line(name or "")[:MAX_NAME_LENGTH] or external_id[:MAX_NAME_LENGTH]
            created = self._db.execute(
                "INSERT INTO people (name, created_at) VALUES (?, ?)", (display, _now())
            )
            self._db.execute(
                "INSERT INTO accounts (surface, external_id, person_id) VALUES (?, ?, ?)",
                (surface, external_id, created.lastrowid),
            )
            return Person(created.lastrowid, display)

    def person_by_id(self, person_id: int) -> Person | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, name FROM people WHERE id = ?", (person_id,)
            ).fetchone()
        return Person(row["id"], row["name"]) if row else None

    def people_named(self, name: str) -> list[Person]:
        """People whose name matches, ignoring case."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name FROM people WHERE lower(name) = lower(?) ORDER BY id", (name,)
            ).fetchall()
        return [Person(row["id"], row["name"]) for row in rows]

    def summaries(self) -> list[PersonSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id, p.name, (SELECT COUNT(*) FROM facts f WHERE f.person_id = p.id)"
                " AS fact_count FROM people p ORDER BY p.id"
            ).fetchall()
            return [
                PersonSummary(
                    Person(row["id"], row["name"]),
                    [f"{surface}:{external}" for surface, external in self.accounts_of(row["id"])],
                    row["fact_count"],
                )
                for row in rows
            ]

    def counts(self) -> tuple[int, int]:
        """(people, facts)"""
        with self._lock:
            people = self._db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            facts = self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return people, facts

    def accounts_of(self, person_id: int) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT surface, external_id FROM accounts WHERE person_id = ?"
                " ORDER BY surface, external_id",
                (person_id,),
            ).fetchall()
        return [(row["surface"], row["external_id"]) for row in rows]

    def has_data(self, person_id: int) -> bool:
        """Does the person own any fact or message?"""
        with self._lock:
            return self._db.execute(
                "SELECT EXISTS (SELECT 1 FROM facts WHERE person_id = ?)"
                " OR EXISTS (SELECT 1 FROM messages WHERE person_id = ?)",
                (person_id, person_id),
            ).fetchone()[0] == 1

    def link_account(
        self, surface: str, external_id: str, target: Person, force: bool = False
    ) -> Person:
        """Make an account belong to `target`.

        If the account already had its own person, that person is merged into
        `target`: facts and history move over, duplicate facts are dropped. A merge
        cannot be undone, so it is refused (MergeRefused) when both people already
        have data, unless `force` (the operator's console).
        """
        with self._lock, self._db:
            current = self.find_person(surface, external_id)
            if current is None:
                self._db.execute(
                    "INSERT INTO accounts (surface, external_id, person_id) VALUES (?, ?, ?)",
                    (surface, external_id, target.id),
                )
            elif current.id != target.id:
                if not force and self.has_data(current.id) and self.has_data(target.id):
                    raise MergeRefused(
                        "Both accounts already have memories; an operator must merge them "
                        "from the server console (/link)."
                    )
                self._merge(current.id, target.id)
        return target

    def _merge(self, source: int, target: int) -> None:
        db = self._db
        db.execute("UPDATE OR IGNORE facts SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("DELETE FROM facts WHERE person_id = ?", (source,))  # duplicates left behind
        db.execute("UPDATE accounts SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE messages SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("DELETE FROM people WHERE id = ?", (source,))

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------
    def facts(self, person_id: int, limit: int = 100) -> list[Fact]:
        """The most recent facts of a person, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text FROM facts WHERE person_id = ? ORDER BY id DESC LIMIT ?",
                (person_id, limit),
            ).fetchall()
        return [Fact(row["id"], row["text"]) for row in reversed(rows)]

    def add_fact(self, person_id: int, text: str) -> Fact | None:
        """Store a fact; None when the person already has it. Raises ValueError if invalid."""
        text = _one_line(text)
        if not text:
            raise ValueError("A fact cannot be empty.")
        if len(text) > MAX_FACT_LENGTH:
            raise ValueError(f"A fact is at most {MAX_FACT_LENGTH} characters long.")
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO facts (person_id, text, created_at) VALUES (?, ?, ?)",
                (person_id, text, _now()),
            )
            return Fact(cursor.lastrowid, text) if cursor.rowcount else None

    def delete_fact(self, person_id: int, fact_id: int) -> bool:
        """Delete one of the person's facts (never someone else's)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM facts WHERE id = ? AND person_id = ?", (fact_id, person_id)
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------
    _MESSAGE_COLUMNS = (
        "SELECT m.id, m.role, m.content, m.person_id, p.name, m.prefix, m.tool_calls, m.tool_name"
        " FROM messages m LEFT JOIN people p ON p.id = m.person_id"
    )

    @staticmethod
    def _stored(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            row["role"],
            row["content"],
            row["person_id"],
            row["name"],
            row["id"],
            row["prefix"],
            json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            row["tool_name"],
        )

    def history(self, conversation: str, turns: int, after_id: int = 0) -> list[StoredMessage]:
        """The last `turns` turns of a conversation (each from a user message to the
        next one, tool calls included), oldest first, ignoring messages up to `after_id`."""
        with self._lock:
            start = self._db.execute(
                "SELECT id FROM messages WHERE conversation = ? AND role = 'user' AND id > ?"
                " ORDER BY id DESC LIMIT 1 OFFSET ?",
                (conversation, after_id, max(turns, 1) - 1),
            ).fetchone()
            rows = self._db.execute(
                self._MESSAGE_COLUMNS + " WHERE m.conversation = ? AND m.id >= ? ORDER BY m.id",
                (conversation, start["id"] if start else after_id + 1),
            ).fetchall()
        return [self._stored(row) for row in rows]

    def messages_after(self, conversation: str, after_id: int = 0) -> list[StoredMessage]:
        """Every message of a conversation after `after_id`, oldest first."""
        with self._lock:
            rows = self._db.execute(
                self._MESSAGE_COLUMNS + " WHERE m.conversation = ? AND m.id > ? ORDER BY m.id",
                (conversation, after_id),
            ).fetchall()
        return [self._stored(row) for row in rows]

    def last_message_id(self, conversation: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(id) FROM messages WHERE conversation = ?", (conversation,)
            ).fetchone()
        return row[0] or 0

    def add_turn(
        self,
        conversation: str,
        person_id: int,
        question: str,
        rows: list[TurnRow],
        prefix: str = "",
    ) -> None:
        """Store a question and everything that followed it (calls, results, answer), or nothing."""
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO messages (conversation, person_id, role, content, created_at, prefix)"
                " VALUES (?, ?, 'user', ?, ?, ?)",
                (conversation, person_id, question, now, prefix),
            )
            self._db.executemany(
                "INSERT INTO messages (conversation, person_id, role, content, created_at,"
                " tool_calls, tool_name) VALUES (?, NULL, ?, ?, ?, ?, ?)",
                [
                    (
                        conversation,
                        row.role,
                        row.content,
                        now,
                        json.dumps(row.tool_calls, ensure_ascii=False) if row.tool_calls else None,
                        row.tool_name,
                    )
                    for row in rows
                ],
            )

    def add_exchange(self, conversation: str, person_id: int, question: str, answer: str) -> None:
        """Store a question and its plain answer."""
        self.add_turn(conversation, person_id, question, [TurnRow("assistant", answer)])

    def clear_conversation(self, conversation: str) -> int:
        with self._lock, self._db:
            self._db.execute("DELETE FROM conversation_state WHERE conversation = ?", (conversation,))
            return self._db.execute(
                "DELETE FROM messages WHERE conversation = ?", (conversation,)
            ).rowcount

    # ------------------------------------------------------------------
    # Summary and size of a conversation
    # ------------------------------------------------------------------
    def state(self, conversation: str) -> ConversationState:
        with self._lock:
            row = self._db.execute(
                "SELECT summary, upto_id, context_tokens FROM conversation_state"
                " WHERE conversation = ?",
                (conversation,),
            ).fetchone()
        return ConversationState(*row) if row else ConversationState()

    def set_context_tokens(self, conversation: str, tokens: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversation_state (conversation, context_tokens) VALUES (?, ?)"
                " ON CONFLICT (conversation) DO UPDATE SET context_tokens = excluded.context_tokens",
                (conversation, tokens),
            )

    def set_summary(self, conversation: str, summary: str, upto_id: int, context_tokens: int) -> None:
        """From now on `summary` stands for every message up to `upto_id`."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversation_state (conversation, summary, upto_id, context_tokens)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (conversation) DO UPDATE SET"
                " summary = excluded.summary, upto_id = excluded.upto_id,"
                " context_tokens = excluded.context_tokens",
                (conversation, summary, upto_id, context_tokens),
            )
