import pytest


def test_same_account_is_the_same_person(memory):
    first = memory.resolve("cli", "erwan", "Erwan")
    again = memory.resolve("cli", "erwan", "Another name")
    assert first == again
    assert first.name == "Erwan"


def test_name_falls_back_to_the_external_id(memory):
    assert memory.resolve("discord", "1234").name == "1234"


def test_facts_are_per_person_and_deduplicated(memory):
    alice = memory.resolve("cli", "alice")
    bob = memory.resolve("cli", "bob")
    assert memory.add_fact(alice.id, "Likes   tea") is not None
    assert memory.add_fact(alice.id, "likes tea") is None  # same fact, other case/spacing
    assert memory.add_fact(bob.id, "Likes tea") is not None
    assert [fact.text for fact in memory.facts(alice.id)] == ["Likes tea"]


@pytest.mark.parametrize("text", ["", "   ", "x" * 301])
def test_invalid_facts_are_rejected(memory, text):
    person = memory.resolve("cli", "alice")
    with pytest.raises(ValueError):
        memory.add_fact(person.id, text)


def test_a_person_cannot_delete_someone_elses_fact(memory):
    alice = memory.resolve("cli", "alice")
    bob = memory.resolve("cli", "bob")
    fact = memory.add_fact(alice.id, "Secret")
    assert not memory.delete_fact(bob.id, fact.id)
    assert memory.delete_fact(alice.id, fact.id)
    assert memory.facts(alice.id) == []


def test_linking_a_new_account_shares_the_person(memory):
    me = memory.resolve("cli", "erwan", "Erwan")
    memory.add_fact(me.id, "Lives in Paris")
    memory.link_account("discord", "1234", me)
    assert memory.find_person("discord", "1234") == me
    assert memory.accounts_of(me.id) == [("cli", "erwan"), ("discord", "1234")]


def test_linking_an_existing_account_merges_both_people(memory):
    me = memory.resolve("cli", "erwan", "Erwan")
    other = memory.resolve("discord", "1234", "erwan#0001")
    memory.add_fact(me.id, "Lives in Paris")
    memory.add_fact(other.id, "lives in paris")  # duplicate of the first
    memory.add_fact(other.id, "Plays guitar")
    memory.add_exchange("discord:chan", other.id, "hi", "hello")

    memory.link_account("discord", "1234", me)

    assert memory.find_person("discord", "1234") == me
    assert [fact.text for fact in memory.facts(me.id)] == ["Lives in Paris", "Plays guitar"]
    assert memory.history("discord:chan", 10)[0].person_id == me.id


def test_history_is_per_conversation_and_ordered(memory):
    person = memory.resolve("cli", "alice", "Alice")
    memory.add_exchange("a", person.id, "q1", "r1")
    memory.add_exchange("a", person.id, "q2", "r2")
    memory.add_exchange("b", person.id, "other", "thread")

    assert [m.content for m in memory.history("a", 10)] == ["q1", "r1", "q2", "r2"]
    assert [m.content for m in memory.history("a", 2)] == ["q2", "r2"]
    assert memory.history("a", 1)[0].role == "assistant"
    assert memory.history("a", 10)[0].author == "Alice"
    assert memory.clear_conversation("a") == 4
    assert memory.history("a", 10) == []
    assert len(memory.history("b", 10)) == 2
