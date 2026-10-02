"""Gaps found by the review: the Ollama backend's stream, merging with conversation state, the version."""

from types import SimpleNamespace as NS

import clara
from clara.llm import OllamaBackend


class FakeOllamaClient:
    """Plays what the ollama package returns for `chat(..., stream=True)`."""

    def __init__(self, parts):
        self.parts = parts
        self.requests = []

    async def chat(self, **request):
        self.requests.append(request)

        async def parts():
            for part in self.parts:
                yield part

        return parts()


def part(content="", calls=(), prompt=None, completion=None):
    tool_calls = [NS(function=NS(name=name, arguments=arguments)) for name, arguments in calls]
    return NS(message=NS(content=content, tool_calls=tool_calls or None), prompt_eval_count=prompt, eval_count=completion)


async def test_the_ollama_backend_turns_parts_into_chunks():
    client = FakeOllamaClient(
        [part("Hel"), part("lo"), part("", calls=[("read_file", {"path": "a"})]), part("", prompt=120, completion=7)]
    )
    backend = OllamaBackend("m", client=client, num_ctx=8192)
    schemas = [{"type": "function", "function": {"name": "read_file"}}]

    chunks = [chunk async for chunk in backend.stream([{"role": "user", "content": "hi"}], schemas)]

    assert [c.text for c in chunks] == ["Hel", "lo", "", ""]
    assert chunks[2].tool_calls[0].name == "read_file" and chunks[2].tool_calls[0].arguments == {"path": "a"}
    assert (chunks[3].prompt_tokens, chunks[3].completion_tokens) == (120, 7)
    request = client.requests[0]
    assert request["model"] == "m" and request["stream"] is True and request["tools"] == schemas
    assert request["options"] == {"num_ctx": 8192}


async def test_the_ollama_backend_sends_no_tools_or_options_when_there_are_none():
    client = FakeOllamaClient([part("ok")])
    backend = OllamaBackend("m", client=client)
    assert [c.text async for c in backend.stream([], None)] == ["ok"]
    assert client.requests[0]["tools"] is None and client.requests[0]["options"] is None


def test_merging_people_keeps_the_summary_and_the_history_of_their_conversations(memory):
    me = memory.resolve("cli", "erwan", "Erwan")
    other = memory.resolve("discord", "1", "E")
    memory.add_exchange("discord:chan", other.id, "hello", "hi")
    memory.add_exchange("discord:chan", other.id, "and again", "yes")
    memory.set_summary("discord:chan", "They said hello", 2, 90)

    memory.link_account("discord", "1", me, force=True)

    state = memory.state("discord:chan")
    assert (state.summary, state.upto_id, state.context_tokens) == ("They said hello", 2, 90)
    history = memory.messages_after("discord:chan", state.upto_id)
    assert [m.content for m in history] == ["and again", "yes"]
    assert history[0].person_id == me.id  # the messages now belong to the person that remains


def test_the_version_comes_from_the_package_metadata():
    from importlib import metadata

    assert clara.__version__ == metadata.version("clara-server")
