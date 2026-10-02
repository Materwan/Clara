"""A chat token only reaches the surfaces it is allowed, and accounts are not linked for free."""

from dataclasses import replace

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.linking import LinkCodes
from clara.memory import MergeRefused
from clara.server import create_app
from clara.settings import Settings, SettingsError, parse_client_surfaces

CLI = {"Authorization": "Bearer secret-cli"}
DISCORD = {"Authorization": "Bearer secret-discord"}
ERWAN = {"surface": "cli", "user_id": "erwan"}
DISCORD_42 = {"surface": "discord", "user_id": "42"}


@pytest.fixture
def restricted(settings):
    limited = replace(
        settings,
        client_surfaces={"terminal": frozenset({"cli", "console"}), "discord": frozenset({"discord"})},
    )
    backend = FakeBackend(say("ok"), say("ok"), say("ok"))
    with TestClient(create_app(limited, fake_providers(limited, backend))) as client:
        yield client


def test_a_client_cannot_speak_for_another_surface(restricted):
    body = {**ERWAN, "message": "hi"}
    assert restricted.post("/v1/chat", json=body, headers=DISCORD).status_code == 403
    assert restricted.post("/v1/chat/stream", json=body, headers=DISCORD).status_code == 403
    assert restricted.post("/v1/chat", json=body, headers=CLI).status_code == 200


def test_a_client_cannot_touch_facts_of_another_surface(restricted):
    restricted.post("/v1/memory/facts", json={**ERWAN, "text": "Likes tea"}, headers=CLI)
    assert restricted.get("/v1/memory/facts", params=ERWAN, headers=DISCORD).status_code == 403
    assert restricted.post("/v1/memory/facts", json={**ERWAN, "text": "x"}, headers=DISCORD).status_code == 403
    assert restricted.delete("/v1/memory/facts/1", params=ERWAN, headers=DISCORD).status_code == 403
    assert len(restricted.get("/v1/memory/facts", params=ERWAN, headers=CLI).json()["facts"]) == 1


def test_a_client_cannot_reach_conversations_of_another_surface(restricted):
    restricted.post("/v1/chat", json={**ERWAN, "message": "hi"}, headers=CLI)
    assert restricted.get("/v1/conversations/cli:erwan", headers=DISCORD).status_code == 403
    assert restricted.post("/v1/conversations/cli:erwan/compact", json={}, headers=DISCORD).status_code == 403
    assert restricted.delete("/v1/conversations/cli:erwan", headers=DISCORD).status_code == 403
    assert restricted.get("/v1/conversations/cli:erwan", headers=CLI).status_code == 200
    # nor can it name a foreign conversation in a chat of its own surface
    body = {**DISCORD_42, "message": "hi", "conversation": "cli:erwan"}
    assert restricted.post("/v1/chat", json=body, headers=DISCORD).status_code == 403
    body["conversation"] = "discord:channel:42"
    assert restricted.post("/v1/chat", json=body, headers=DISCORD).status_code == 200


def link_body(code, **extra):
    return {**DISCORD_42, "code": code, "to_surface": "cli", "to_user_id": "erwan", **extra}


def link_code(client, headers=DISCORD, account=DISCORD_42):
    return client.post("/v1/accounts/link-code", json=account, headers=headers)


def test_linking_needs_the_code_of_the_account_to_attach(restricted):
    restricted.post("/v1/memory/facts", json={**ERWAN, "text": "Likes tea"}, headers=CLI)
    restricted.post("/v1/memory/facts", json={**DISCORD_42, "text": "Victim fact"}, headers=DISCORD)
    # the attacker has no code for discord:42: nothing moves
    assert restricted.post("/v1/accounts/link", json=link_body("guess"), headers=CLI).status_code == 403
    code = link_code(restricted).json()["code"]
    # a code is for one account only, and only a client of that surface can ask for it
    assert restricted.post("/v1/accounts/link", json=link_body(code, user_id="43"), headers=CLI).status_code == 403
    assert link_code(restricted, headers=CLI).status_code == 403
    # both accounts own memories, so even with the code the merge is left to an operator
    assert restricted.post("/v1/accounts/link", json=link_body(code), headers=CLI).status_code == 409
    facts = restricted.get("/v1/memory/facts", params=DISCORD_42, headers=DISCORD)
    assert [f["text"] for f in facts.json()["facts"]] == ["Victim fact"]  # not destroyed


def test_linking_with_a_valid_code_works_once(restricted):
    restricted.post("/v1/memory/facts", json={**ERWAN, "text": "Likes tea"}, headers=CLI)
    code = link_code(restricted).json()["code"]
    ok = restricted.post("/v1/accounts/link", json=link_body(code), headers=CLI)
    assert ok.status_code == 200 and ok.json()["accounts"] == ["cli:erwan", "discord:42"]
    assert restricted.post("/v1/accounts/link", json=link_body(code), headers=CLI).status_code == 403


def test_a_client_cannot_link_into_a_surface_it_does_not_own(restricted):
    restricted.post("/v1/memory/facts", json={**ERWAN, "text": "Likes tea"}, headers=CLI)
    code = link_code(restricted).json()["code"]
    assert restricted.post("/v1/accounts/link", json=link_body(code), headers=DISCORD).status_code == 403


def test_unrestricted_clients_still_work(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend(say("ok"))))) as client:
        assert client.post("/v1/chat", json={**ERWAN, "message": "hi"}, headers=DISCORD).status_code == 200


def test_unrestricted_clients_are_warned_about(settings, caplog):
    create_app(settings, fake_providers(settings, FakeBackend()))
    assert "'terminal' may speak for any surface" in caplog.text


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_link_codes_expire_and_are_single_use():
    clock = Clock()
    codes = LinkCodes(lifetime=600, clock=clock)
    first = codes.issue("discord", "1")
    assert not codes.redeem("discord", "1", "wrong")
    assert codes.redeem("discord", "1", first)
    assert not codes.redeem("discord", "1", first)

    second = codes.issue("discord", "1")
    clock.now = 601
    assert not codes.redeem("discord", "1", second)


def test_memory_refuses_to_merge_two_people_with_data(memory):
    me = memory.resolve("cli", "erwan", "Erwan")
    other = memory.resolve("discord", "1", "Other")
    memory.add_fact(me.id, "a")
    memory.add_fact(other.id, "b")
    with pytest.raises(MergeRefused):
        memory.link_account("discord", "1", me)
    assert memory.find_person("discord", "1") == other
    memory.link_account("discord", "1", me, force=True)
    assert memory.find_person("discord", "1") == me


def test_memory_merges_when_only_one_side_has_data(memory):
    me = memory.resolve("cli", "erwan", "Erwan")
    other = memory.resolve("discord", "1", "Other")
    memory.add_fact(other.id, "b")
    memory.link_account("discord", "1", me)
    assert [f.text for f in memory.facts(me.id)] == ["b"]


def test_client_surfaces_setting():
    parsed = parse_client_surfaces("terminal=cli|console, discord=discord", {"terminal", "discord"})
    assert parsed == {"terminal": frozenset({"cli", "console"}), "discord": frozenset({"discord"})}
    for bad in ("ghost=cli", "terminal=Bad Surface", "terminal=", "terminal"):
        with pytest.raises(SettingsError):
            parse_client_surfaces(bad, {"terminal"})
    settings = Settings.from_env({"CLARA_TOKENS": "a:one,b:two", "CLARA_CLIENT_SURFACES": "a=cli"})
    assert settings.client_surfaces == {"a": frozenset({"cli"})}
    assert settings.unrestricted_clients == ["b"]
