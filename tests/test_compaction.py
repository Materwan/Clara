"""Compaction keeps what matters: tool arguments, every message, and the latest turns."""

from pathlib import Path

import pytest
from conftest import FakeBackend, say, untimed

from clara.agent import Agent, ChatRequest
from clara.compaction import build_transcript, chunk_messages, estimate_message_tokens
from clara.memory import StoredMessage
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox

CONVERSATION = "cli:erwan"


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    options.setdefault("compact_percent", 0)
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def talk(agent: Agent, message: str) -> None:
    request = ChatRequest("cli", "erwan", "Erwan", message)
    async for _ in agent.turn(request):
        pass


def stored(role, content, **fields) -> StoredMessage:
    return StoredMessage(role, content, 1, "Erwan" if role == "user" else None, fields.pop("id", 0), **fields)


# --- the transcript ---------------------------------------------------------------


def test_the_transcript_shows_the_arguments_of_tool_calls():
    messages = [
        stored("user", "open it"),
        stored("assistant", "", tool_calls=[{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]),
        stored("tool", "contents", tool_name="read_file"),
    ]
    assert build_transcript(messages) == (
        "Erwan: open it\n"
        'Assistant: [called: read_file({"path": "a.txt"})]\n'
        "[read_file result] contents"
    )


def test_long_arguments_and_results_are_clipped():
    call = {"function": {"name": "write_file", "arguments": {"text": "y" * 1000}}}
    transcript = build_transcript([stored("assistant", "", tool_calls=[call]), stored("tool", "z" * 1000, tool_name="w")])
    assert "y" * 150 in transcript and "y" * 250 not in transcript
    assert "z" * 300 in transcript and "z" * 400 not in transcript


def test_chunks_cover_every_message_in_order():
    messages = [stored("user", f"message {i} " + "x" * 90, id=i) for i in range(1, 11)]
    chunks = chunk_messages(messages, budget=300)
    assert len(chunks) > 1
    assert [m.id for chunk in chunks for m in chunk] == list(range(1, 11))
    assert all(len(build_transcript(chunk)) <= 300 for chunk in chunks)


def test_a_message_larger_than_the_budget_gets_its_own_chunk():
    chunks = chunk_messages([stored("user", "a" * 500, id=1), stored("user", "b", id=2)], budget=100)
    assert [[m.id for m in chunk] for chunk in chunks] == [[1], [2]]


def test_message_estimates_count_prefix_and_tool_calls():
    plain = estimate_message_tokens(stored("user", "x" * 350))
    with_extras = estimate_message_tokens(
        stored("user", "x" * 350, prefix="p" * 350, tool_calls=[{"function": {"name": "n", "arguments": {}}}])
    )
    assert with_extras > plain * 2


# --- compaction -------------------------------------------------------------------


async def test_a_long_transcript_is_summarised_in_chunks_without_losing_anything(memory, tmp_path):
    # four turns of ~600 characters, and a budget that fits one turn per request
    backend = FakeBackend(
        *[say(f"answer {i}") for i in range(4)], say("summary A"), say("summary B"), say("summary C"), say("summary D")
    )
    agent = make_agent(memory, tmp_path, backend, keep_recent_turns=0, transcript_chars=700)
    for i in range(4):
        await talk(agent, f"question {i} " + "w" * 560)

    await agent.compact(CONVERSATION)

    requests = [call[0][1]["content"] for call in backend.calls[4:]]
    assert len(requests) == 4
    for i, text in enumerate(requests):
        assert f"question {i}" in text  # every message was shown to the summariser, once
    assert "Summary of the conversation before these messages:\nsummary A" in requests[1]
    assert "Summary of the conversation before these messages:\nsummary C" in requests[3]
    state = memory.state(CONVERSATION)
    assert state.summary == "summary D"
    assert state.upto_id == memory.last_message_id(CONVERSATION)


async def test_a_failure_halfway_keeps_what_was_summarised(memory, tmp_path):
    backend = FakeBackend(*[say(f"a{i}") for i in range(3)], say("summary A"), say(""))  # 2nd request: empty
    agent = make_agent(memory, tmp_path, backend, keep_recent_turns=0, transcript_chars=700)
    for i in range(3):
        await talk(agent, f"q{i} " + "w" * 500)

    with pytest.raises(RuntimeError, match="empty summary"):
        await agent.compact(CONVERSATION)

    state = memory.state(CONVERSATION)
    assert state.summary == "summary A"
    assert 0 < state.upto_id < memory.last_message_id(CONVERSATION)  # the rest is still replayed, not lost


async def test_the_last_turns_stay_verbatim(memory, tmp_path):
    backend = FakeBackend(say("a1"), say("a2"), say("a3"), say("a4"), say("old summary"), say("a5"))
    agent = make_agent(memory, tmp_path, backend, keep_recent_turns=2)
    for text in ("q1", "q2", "q3", "q4"):
        await talk(agent, text)

    await agent.compact(CONVERSATION)

    transcript = backend.calls[4][0][1]["content"]
    assert "q1" in transcript and "q2" in transcript
    assert "q3" not in transcript and "q4" not in transcript

    await talk(agent, "q5")
    prompt = backend.calls[5][0]
    assert "## Earlier in this conversation (summary)\nold summary" in prompt[0]["content"]
    assert [untimed(m["content"]) for m in prompt[1:]] == ["q3", "a3", "q4", "a4", "q5"]


async def test_with_too_few_turns_everything_is_summarised(memory, tmp_path):
    backend = FakeBackend(say("a1"), say("a2"), say("summary"), say("a3"))
    agent = make_agent(memory, tmp_path, backend, keep_recent_turns=2)
    await talk(agent, "q1")
    await talk(agent, "q2")

    await agent.compact(CONVERSATION)
    assert memory.state(CONVERSATION).upto_id == memory.last_message_id(CONVERSATION)

    await talk(agent, "q3")
    assert [untimed(m["content"]) for m in backend.calls[3][0][1:]] == ["q3"]


async def test_the_size_after_compaction_counts_the_kept_turns(memory, tmp_path):
    long = "x" * 3_500  # about 1000 tokens
    backend = FakeBackend(
        say("a1", prompt_tokens=600), say("a2", prompt_tokens=1_200), say("a3", prompt_tokens=1_800),
        say("short summary"),
    )
    agent = make_agent(memory, tmp_path, backend, context_window=4_000, keep_recent_turns=1)
    for i in range(1, 4):
        await talk(agent, f"q{i} " + long)

    before, after = await agent.compact(CONVERSATION)

    assert before == pytest.approx(100 * 1_803 / 4_000, abs=0.5)
    kept_tokens = 1_000  # the last turn stays in the context
    assert after > 100 * kept_tokens / 4_000
    assert after < before
    assert memory.state(CONVERSATION).context_tokens == pytest.approx(after * 40, abs=1)
