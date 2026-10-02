"""Erasing a person, and not keeping messages a summary already stands for."""

import time
from pathlib import Path

import pytest
from conftest import FakeBackend, fake_providers, say

from clara.agent import Agent, ChatRequest
from clara.commands import CommandContext, registry
from clara.prompt import SystemPrompt
from clara.settings import Settings, SettingsError
from clara.tools import default_toolbox


def fill(memory):
    """Erwan (two accounts, facts, a private thread, a shared channel) and Zoe (same channel)."""
    erwan = memory.resolve("cli", "erwan", "Erwan")
    memory.link_account("discord", "1", erwan)
    zoe = memory.resolve("discord", "2", "Zoe")
    memory.add_fact(erwan.id, "Likes jazz")
    memory.add_fact(erwan.id, "Lives in Lyon")
    memory.add_fact(zoe.id, "Likes opera")
    memory.add_exchange("cli:erwan", erwan.id, "my secret", "noted")
    memory.add_exchange("discord:channel", erwan.id, "hello channel", "hi Erwan")
    memory.add_exchange("discord:channel", zoe.id, "hello from Zoe", "hi Zoe")
    memory.set_summary("cli:erwan", "Erwan told a secret", 2, 50)
    memory.set_summary("discord:channel", "Erwan and Zoe said hello", 3, 50)
    return erwan, zoe


def test_footprint_counts_what_would_go(memory):
    erwan, zoe = fill(memory)
    found = memory.footprint(erwan.id)
    # 2 accounts, 2 facts; the private thread (2 messages) plus his own message in the channel
    assert (found.accounts, found.facts, found.messages, found.conversations) == (2, 2, 3, 2)
    assert memory.footprint(zoe.id).messages == 1


def test_deleting_a_person_erases_them_and_only_them(memory):
    erwan, zoe = fill(memory)
    memory.delete_person(erwan.id)

    assert memory.person_by_id(erwan.id) is None
    assert memory.find_person("cli", "erwan") is None and memory.find_person("discord", "1") is None
    assert memory.facts(erwan.id) == []
    # his private thread is gone with its summary; the shared channel keeps what is not his
    assert memory.messages_after("cli:erwan") == [] and memory.state("cli:erwan").summary == ""
    channel = [m.content for m in memory.messages_after("discord:channel")]
    assert "hello channel" not in channel and "hello from Zoe" in channel and "hi Zoe" in channel
    assert memory.state("discord:channel").summary != ""  # documented caveat
    # Zoe is untouched
    assert memory.find_person("discord", "2") == zoe and [f.text for f in memory.facts(zoe.id)] == ["Likes opera"]


def test_a_new_person_can_start_again_after_an_erasure(memory):
    erwan, _ = fill(memory)
    memory.delete_person(erwan.id)
    again = memory.resolve("cli", "erwan", "Erwan")
    assert again.id != erwan.id and memory.facts(again.id) == []


@pytest.fixture
def ctx(settings, memory, tmp_path) -> CommandContext:
    providers = fake_providers(settings)
    agent = Agent(memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    return CommandContext(settings, memory, agent, providers, time.monotonic(), "127.0.0.1:8765")


async def run(ctx, line: str) -> str:
    return (await registry.execute(line, ctx)).output


async def test_forget_person_shows_what_would_go_until_confirmed(ctx, memory):
    erwan, _ = fill(memory)
    preview = await run(ctx, "/forget-person Erwan")
    assert "2 facts" in preview and "/forget-person" in preview and "confirm" in preview
    assert memory.person_by_id(erwan.id) is not None

    done = await run(ctx, "/forget-person Erwan confirm")
    assert done.startswith("Erased Erwan") and memory.person_by_id(erwan.id) is None


async def test_forget_person_accepts_ids_accounts_and_reports_mistakes(ctx, memory):
    erwan, zoe = fill(memory)
    assert (await run(ctx, "/forget-person discord:2 confirm")).startswith("Erased Zoe")
    assert (await run(ctx, "/forget-person nobody confirm")).startswith("!")
    assert (await run(ctx, "/forget-person confirm")).startswith("!")
    assert (await run(ctx, "/forget-person")).startswith("!")
    assert memory.person_by_id(erwan.id) is not None


# --- purging summarised messages ------------------------------------------------------


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def talk(agent, text):
    async for _ in agent.turn(ChatRequest("cli", "erwan", "Erwan", text)):
        pass


@pytest.mark.parametrize("purge", [False, True])
async def test_purging_summarised_messages_is_optional(memory, tmp_path, purge):
    backend = FakeBackend(say("a1"), say("a2"), say("a3"), say("summary of 1-2"))
    agent = make_agent(memory, tmp_path, backend, compact_percent=0, keep_recent_turns=1, purge_summarised=purge)
    for text in ("q1", "q2", "q3"):
        await talk(agent, text)

    await agent.compact("cli:erwan")

    kept = [m.content for m in memory.messages_after("cli:erwan", 0)]
    assert memory.state("cli:erwan").summary == "summary of 1-2"
    if purge:
        assert kept == ["q3", "a3"]  # the summary is the only record of the rest
    else:
        assert kept == ["q1", "a1", "q2", "a2", "q3", "a3"]
    assert [m.content for m in memory.messages_after("cli:erwan", memory.state("cli:erwan").upto_id)] == ["q3", "a3"]


def test_the_purge_setting():
    base = {"CLARA_TOKENS": "a:secret-one"}
    assert Settings.from_env(base).purge_summarised is False
    assert Settings.from_env({**base, "CLARA_PURGE_SUMMARISED": "true"}).purge_summarised is True
    assert Settings.from_env({**base, "CLARA_PURGE_SUMMARISED": "off"}).purge_summarised is False
    with pytest.raises(SettingsError):
        Settings.from_env({**base, "CLARA_PURGE_SUMMARISED": "maybe"})
