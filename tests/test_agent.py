import asyncio
from datetime import datetime
from pathlib import Path

from conftest import FakeBackend, call, say

from clara.agent import Agent, ChatRequest
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "cli", "user_id": "erwan", "user_name": "Erwan", **fields})
    return [event async for event in agent.turn(request)]


async def test_streams_tokens_then_done_and_stores_the_exchange(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say("Hel", "lo!")))
    events = await run(agent, message="hi")

    assert [e["type"] for e in events] == ["token", "token", "done"]
    done = events[-1]
    assert done["reply"] == "Hello!"
    assert done["conversation"] == "cli:erwan"
    assert done["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert [m.content for m in memory.history("cli:erwan", 10)] == ["hi", "Hello!"]


async def test_tools_run_and_memory_reaches_the_next_prompt(memory, tmp_path):
    backend = FakeBackend(
        call("remember", fact="Has a cat named Miso"),
        say("Noted."),
        say("Miso, your cat!"),
    )
    agent = make_agent(memory, tmp_path, backend)

    first = await run(agent, message="I have a cat named Miso")
    assert [e["type"] for e in first] == ["tool", "token", "done"]
    assert first[-1]["tools"] == ["remember"]

    # Another surface, same person once linked: the fact is in the system prompt
    person = memory.find_person("cli", "erwan")
    memory.link_account("discord", "1234", person)
    await run(agent, surface="discord", user_id="1234", message="what is my pet called?")

    system_prompt = backend.calls[-1][0][0]["content"]
    assert "Has a cat named Miso" in system_prompt
    assert "Erwan" in system_prompt


async def test_tool_result_is_sent_back_to_the_model(memory, tmp_path):
    backend = FakeBackend(call("remember", fact="Likes tea"), say("ok"))
    agent = make_agent(memory, tmp_path, backend)
    await run(agent, message="I like tea")

    second_round = backend.calls[1][0]
    assert second_round[-2]["role"] == "assistant" and second_round[-2]["tool_calls"]
    assert second_round[-1]["role"] == "tool"
    assert second_round[-1]["content"].startswith("Remembered")


async def test_a_bad_tool_call_does_not_crash_the_turn(memory, tmp_path):
    backend = FakeBackend(call("forget", fact_id="abc"), call("nope"), say("fine"))
    agent = make_agent(memory, tmp_path, backend)
    events = await run(agent, message="x")
    assert events[-1]["reply"] == "fine"


async def test_last_round_offers_no_tools(memory, tmp_path):
    backend = FakeBackend(call("remember", fact="a"), call("remember", fact="b"), say("done"))
    agent = make_agent(memory, tmp_path, backend, max_tool_rounds=2)
    await run(agent, message="x")
    assert [tools is not None for _, tools in backend.calls] == [True, True, False]


async def test_empty_answers_are_not_stored(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say()))
    events = await run(agent, message="hi")
    assert events[-1]["reply"] == ""
    assert memory.history("cli:erwan", 10) == []


async def test_messages_from_other_people_are_labelled(memory, tmp_path):
    backend = FakeBackend(say("a"), say("b"))
    agent = make_agent(memory, tmp_path, backend)
    await run(agent, user_id="alice", user_name="Alice", conversation="room", message="hello")
    await run(agent, user_id="bob", user_name="Bob", conversation="room", message="and me?")

    history = backend.calls[1][0][1:-1]
    assert history[0] == {"role": "user", "content": "Alice: hello"}
    assert history[1] == {"role": "assistant", "content": "a"}
    assert backend.calls[1][0][-1] == {"role": "user", "content": "and me?"}


async def test_turns_of_one_conversation_never_overlap(memory, tmp_path):
    active = peak = 0

    class SlowBackend(FakeBackend):
        async def stream(self, messages, tools):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            for chunk in say("ok"):
                yield chunk

    agent = make_agent(memory, tmp_path, SlowBackend())
    await asyncio.gather(*(run(agent, conversation="same", message=f"m{i}") for i in range(3)))
    assert peak == 1

    peak = 0
    await asyncio.gather(*(run(agent, conversation=f"c{i}", message="m") for i in range(2)))
    assert peak == 2


def test_system_prompt_rereads_the_file_when_edited(tmp_path):
    path = tmp_path / "prompt.md"
    prompt = SystemPrompt(path)
    assert "Clara" in prompt.personality()  # default while the file is missing
    path.write_text("You are Test.", encoding="utf-8")
    assert prompt.personality() == "You are Test."
    path.write_text("You are Other.", encoding="utf-8")
    import os

    os.utime(path, (1, 1))  # force a different mtime
    assert prompt.personality() == "You are Other."
    rendered = prompt.render(
        __import__("clara.memory", fromlist=["Person"]).Person(1, "Zoe"), "cli", [], datetime.now()
    )
    assert "Zoe" in rendered and "(nothing yet)" in rendered
