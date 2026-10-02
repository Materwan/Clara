"""A hung model times out, and a slow reader never keeps a model slot busy."""

import asyncio
from pathlib import Path

import pytest
from conftest import FakeBackend, say

from clara.agent import Agent, ChatRequest, ModelTimeout, with_idle_timeout
from clara.llm import LlmChunk
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


def ask(message="hi", conversation=None) -> ChatRequest:
    return ChatRequest("cli", "erwan", "Erwan", message, conversation)


class Hangs(FakeBackend):
    """Says one word, then never answers again."""

    def __init__(self, hang_before_anything: bool = False):
        super().__init__(model="hangs")
        self.hang_before_anything = hang_before_anything

    async def stream(self, messages, tools):
        if self.hang_before_anything:
            await asyncio.sleep(3600)
        yield LlmChunk(text="Hel")
        await asyncio.sleep(3600)


async def chunks_of(*pieces, delay=0.0):
    for piece in pieces:
        await asyncio.sleep(delay)
        yield LlmChunk(text=piece)


async def test_idle_timeout_wrapper_passes_chunks_and_times_out():
    assert [c.text async for c in with_idle_timeout(chunks_of("a", "b"), 1, 1)] == ["a", "b"]
    with pytest.raises(ModelTimeout, match="to start"):
        [c async for c in with_idle_timeout(chunks_of("a", delay=0.3), 0.05, 1)]
    with pytest.raises(ModelTimeout, match="between two pieces"):
        [c async for c in with_idle_timeout(chunks_of("a", "b", delay=0.15), 1, 0.05)]


@pytest.mark.parametrize("hang_before_anything", [False, True])
async def test_a_model_that_hangs_times_out_and_frees_the_slot_and_the_lock(memory, tmp_path, hang_before_anything):
    agent = make_agent(
        memory, tmp_path, Hangs(hang_before_anything), max_concurrent_llm=1,
        first_token_timeout=0.1, idle_timeout=0.1,
    )
    with pytest.raises(ModelTimeout):
        async for _ in agent.turn(ask()):
            pass
    assert nothing_is_held(agent)

    agent.backend = FakeBackend(say("back again"))
    events = await asyncio.wait_for(_all(agent.turn(ask("hello again"))), timeout=2)
    assert events[-1]["reply"] == "back again"


async def _all(stream):
    return [event async for event in stream]


def nothing_is_held(agent: Agent) -> bool:
    return agent._llm_slots._value == agent.max_concurrent_llm and agent.stats.active == 0


async def test_a_reader_that_stops_reading_does_not_hold_a_slot(memory, tmp_path):
    backend = FakeBackend(say("one ", "two ", "three"), say("other conversation"))
    agent = make_agent(memory, tmp_path, backend, max_concurrent_llm=1)

    stalled = agent.turn(ask("slow reader", conversation="a"))
    assert (await stalled.__anext__())["type"] == "turn"
    assert (await stalled.__anext__())["type"] == "token"  # ...and the reader walks away here

    events = await asyncio.wait_for(_all(agent.turn(ask("quick", conversation="b"))), timeout=2)
    assert events[-1]["reply"] == "other conversation"
    await stalled.aclose()


async def test_closing_a_stream_stops_the_model_and_frees_the_slot(memory, tmp_path):
    agent = make_agent(memory, tmp_path, Hangs(), max_concurrent_llm=1, first_token_timeout=60, idle_timeout=60)
    stream = agent.turn(ask())
    async for event in stream:
        if event["type"] == "token":
            break
    await stream.aclose()
    await asyncio.sleep(0)
    assert nothing_is_held(agent)


async def test_compaction_times_out_too(memory, tmp_path):
    backend = FakeBackend(say("answer"))
    agent = make_agent(memory, tmp_path, backend, first_token_timeout=0.1, idle_timeout=0.1)
    await _all(agent.turn(ask()))
    agent.backend = Hangs(hang_before_anything=True)
    with pytest.raises(ModelTimeout):
        await agent.compact("cli:erwan")
    assert nothing_is_held(agent)
