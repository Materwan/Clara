"""Facts: comparison that works beyond ASCII, a bounded prompt block, and a way to find older ones."""

import sqlite3
from pathlib import Path

from conftest import FakeBackend, call, say

from clara.agent import Agent, ChatRequest
from clara.memory import Memory, fact_key
from clara.prompt import SystemPrompt
from clara.tools import ToolContext, default_toolbox


def test_facts_are_compared_beyond_ascii(memory):
    person = memory.resolve("cli", "erwan", "Erwan")
    assert memory.add_fact(person.id, "Aime Élan")
    for variant in ("aime élan", "AIME ÉLAN", "  Aime   Élan  ", "Aime Élan"):
        assert memory.add_fact(person.id, variant) is None
    assert [fact.text for fact in memory.facts(person.id)] == ["Aime Élan"]


def test_the_key_folds_case_width_and_spaces():
    assert fact_key("  ÉLAN \t vital ") == fact_key("élan vital")
    assert fact_key("Straße") == fact_key("STRASSE")  # casefold, not lower
    assert fact_key("ｆａｃｔ") == "fact"  # NFKC: full-width letters


def test_the_same_fact_may_belong_to_two_people(memory):
    one, two = memory.resolve("cli", "a", "A"), memory.resolve("cli", "b", "B")
    assert memory.add_fact(one.id, "Aime le thé") and memory.add_fact(two.id, "aime le thé")


def test_a_database_from_before_text_keys_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE accounts (surface TEXT NOT NULL, external_id TEXT NOT NULL,
            person_id INTEGER NOT NULL REFERENCES people (id), PRIMARY KEY (surface, external_id));
        CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL REFERENCES people (id),
            text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX idx_facts_unique ON facts (person_id, lower(text));
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
            person_id INTEGER REFERENCES people (id), role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO people (name, created_at) VALUES ('Erwan', 'x');
        INSERT INTO accounts VALUES ('cli', 'erwan', 1);
        INSERT INTO facts (person_id, text, created_at) VALUES
            (1, 'Aime Élan', 'x'), (1, 'aime élan', 'x'), (1, 'Joue de la guitare', 'x');
        """
    )
    old.commit()
    old.close()

    memory = Memory(path)
    try:
        person = memory.find_person("cli", "erwan")
        assert [(f.id, f.text) for f in memory.facts(person.id)] == [(1, "Aime Élan"), (3, "Joue de la guitare")]
        assert memory.add_fact(person.id, "AIME ÉLAN") is None  # the new index knows
        assert memory.add_fact(person.id, "Habite Lyon")
    finally:
        memory.close()
    Memory(path).close()  # and opening it again is harmless


def test_merging_people_drops_duplicates_in_any_case(memory):
    me, other = memory.resolve("cli", "erwan", "Erwan"), memory.resolve("discord", "1", "E")
    memory.add_fact(me.id, "Aime Élan")
    memory.add_fact(other.id, "AIME ÉLAN")
    memory.add_fact(other.id, "Joue du piano")
    memory.link_account("discord", "1", me, force=True)
    assert [f.text for f in memory.facts(me.id)] == ["Aime Élan", "Joue du piano"]


# --- finding facts ------------------------------------------------------------------


def test_search_matches_every_word_in_any_case(memory):
    person = memory.resolve("cli", "erwan", "Erwan")
    for text in ("Likes Jazz and tea", "Lives in Paris", "Plays the guitar", "Éléonore is his sister"):
        memory.add_fact(person.id, text)
    assert [f.text for f in memory.search_facts(person.id, "JAZZ tea")] == ["Likes Jazz and tea"]
    assert [f.text for f in memory.search_facts(person.id, "guit")] == ["Plays the guitar"]  # part of a word
    assert [f.text for f in memory.search_facts(person.id, "éléonore")] == ["Éléonore is his sister"]
    assert memory.search_facts(person.id, "jazz paris") == []
    assert memory.search_facts(person.id, "   ") == []
    assert memory.search_facts(person.id, "100% _") == []  # no wildcard meaning


def test_the_recall_tool_only_returns_the_speakers_facts(memory):
    erwan, zoe = memory.resolve("cli", "erwan", "Erwan"), memory.resolve("cli", "zoe", "Zoe")
    memory.add_fact(erwan.id, "Likes jazz")
    memory.add_fact(zoe.id, "Hates jazz")
    toolbox = default_toolbox()
    answer = toolbox.run("recall_facts", ToolContext(erwan, memory), {"query": "jazz"})
    assert "Likes jazz" in answer and "Hates jazz" not in answer
    assert toolbox.run("recall_facts", ToolContext(erwan, memory), {"query": "opera"}) == "No matching fact."
    assert "recall_facts" in toolbox.names


def test_the_recall_tool_returns_at_most_ten(memory):
    person = memory.resolve("cli", "erwan", "Erwan")
    for index in range(15):
        memory.add_fact(person.id, f"Reads book {index}")
    answer = default_toolbox().run("recall_facts", ToolContext(person, memory), {"query": "book"})
    assert len(answer.splitlines()) == 10 and "book 14" in answer and "book 4" not in answer


# --- the facts in the prompt --------------------------------------------------------


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def test_facts_beyond_the_budget_are_announced_and_the_newest_are_shown(memory, tmp_path):
    person = memory.resolve("cli", "erwan", "Erwan")
    for index in range(40):
        memory.add_fact(person.id, f"Fact number {index:02d} about something")
    backend = FakeBackend(say("ok"))
    agent = make_agent(memory, tmp_path, backend, facts_token_budget=100)  # room for about 8 facts

    async for _ in agent.turn(ChatRequest("cli", "erwan", "Erwan", "hi")):
        pass

    system = backend.calls[0][0][0]["content"]
    shown = [line for line in system.splitlines() if line.startswith("- [")]
    assert 3 <= len(shown) < 15 and "Fact number 39" in shown[-1] and "Fact number 00" not in system
    assert f"[{40 - len(shown)} older facts not shown, use recall_facts]" in system


async def test_no_notice_when_every_fact_fits(memory, tmp_path):
    person = memory.resolve("cli", "erwan", "Erwan")
    memory.add_fact(person.id, "Likes tea")
    backend = FakeBackend(say("ok"))
    async for _ in make_agent(memory, tmp_path, backend).turn(ChatRequest("cli", "erwan", "Erwan", "hi")):
        pass
    assert "older facts not shown" not in backend.calls[0][0][0]["content"]


async def test_the_model_can_recall_an_older_fact(memory, tmp_path):
    person = memory.resolve("cli", "erwan", "Erwan")
    memory.add_fact(person.id, "Allergic to peanuts")
    backend = FakeBackend(call("recall_facts", query="peanuts"), say("You are allergic to peanuts."))
    async for _ in make_agent(memory, tmp_path, backend).turn(ChatRequest("cli", "erwan", "Erwan", "allergies?")):
        pass
    tool_message = backend.calls[1][0][-1]
    assert tool_message["role"] == "tool" and "Allergic to peanuts" in tool_message["content"]
