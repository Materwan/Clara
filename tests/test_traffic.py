"""The traffic log: every request in and out, written as JSON lines, secrets left out."""

import json
from dataclasses import replace
from datetime import date

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.server import create_app
from clara.traffic import REDACTED, TrafficLog, redact

AUTH = {"Authorization": "Bearer secret-cli"}
CHAT = {"surface": "cli", "user_id": "erwan", "message": "hello"}


def entries(directory) -> list[dict]:
    lines = []
    for path in sorted(directory.glob("traffic-*.jsonl")):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return lines


def make(settings, *rounds, **changes):
    settings = replace(settings, **changes)
    return settings, TestClient(create_app(settings, fake_providers(settings, FakeBackend(*rounds))))


def written(client) -> list[dict]:
    client.app.state.traffic.flush()
    return entries(client.app.state.settings.logs_dir)


def test_a_request_and_its_response_are_logged_without_the_token(settings):
    _, http = make(settings, say("Hi ", "Erwan"))
    with http:
        assert http.post("/v1/chat", json=CHAT, headers=AUTH).status_code == 200
        log = written(http)
    request = next(e for e in log if e["kind"] == "request" and e["path"] == "/v1/chat")
    response = next(e for e in log if e["kind"] == "response" and e["id"] == request["id"])
    assert (request["dir"], request["peer"], request["method"]) == ("in", "client:terminal", "POST")
    assert request["body"]["message"] == "hello"
    assert (response["dir"], response["status"]) == ("out", 200)
    assert response["body"]["reply"] == "Hi Erwan"
    assert "secret-cli" not in json.dumps(log)


def test_the_model_call_is_logged_as_outgoing(settings):
    _, http = make(settings, say("Hi"))
    with http:
        http.post("/v1/chat", json=CHAT, headers=AUTH)
        log = written(http)
    sent = next(e for e in log if e["kind"] == "llm_request")
    answer = next(e for e in log if e["kind"] == "llm_response")
    assert (sent["dir"], sent["peer"], sent["model"]) == ("out", "ollama:local", "fake")
    assert sent["messages"][-1]["content"].endswith("hello")
    assert {tool["function"]["name"] for tool in sent["tools"]} >= {"remember", "notify"}
    assert (answer["id"], answer["text"], answer["error"]) == (sent["id"], "Hi", None)
    assert answer["prompt_tokens"] == 10 and answer["completion_tokens"] == 3


def test_a_stream_is_logged_event_by_event_but_the_tokens_are_only_counted(settings):
    _, http = make(settings, say("a", "b", "c"))
    with http:
        with http.stream("POST", "/v1/chat/stream", json=CHAT, headers=AUTH) as response:
            response.read()
        log = written(http)
    events = [e["event"] for e in log if e["kind"] == "sse"]
    assert "token" not in events and events[0] == "turn" and events[-1] == "done"
    done = next(e for e in log if e["kind"] == "sse" and e["event"] == "done")
    assert done["data"]["reply"] == "abc"
    end = next(e for e in log if e["kind"] == "response" and e["path"] == "/v1/chat/stream")
    assert end["tokens"] == 3 and end["events"] == len(events)


def test_secrets_in_bodies_are_redacted(settings):
    _, http = make(settings)
    with http:
        http.post("/v1/accounts/link-code", json={"surface": "cli", "user_id": "erwan"}, headers=AUTH)
        log = written(http)
    response = next(e for e in log if e["kind"] == "response" and e["path"] == "/v1/accounts/link-code")
    assert response["body"]["code"] == REDACTED
    assert redact({"a": [{"api_key": "k", "x": 1}]}) == {"a": [{"api_key": REDACTED, "x": 1}]}


def test_who_calls_is_named(settings):
    _, http = make(settings)
    with http:
        http.get("/health")
        http.get("/v1/reminders", params={"surface": "cli", "user_id": "x"}, headers={"Authorization": "Bearer bad"})
        http.get("/v1/admin/commands", headers={"Authorization": "Bearer secret-admin"})
        log = written(http)
    peers = {e["path"]: e["peer"] for e in log if e["kind"] == "request"}
    assert peers == {"/health": "anonymous", "/v1/reminders": "unknown-token", "/v1/admin/commands": "admin:ops"}


def test_big_bodies_are_cut(settings):
    _, http = make(settings, say("ok"), traffic_log_max_body=50)
    with http:
        http.post("/v1/chat", json={**CHAT, "message": "x" * 500}, headers=AUTH)
        log = written(http)
    request = next(e for e in log if e["kind"] == "request" and e["path"] == "/v1/chat")
    assert isinstance(request["body"], str) and "[cut:" in request["body"] and len(request["body"]) < 120


def test_the_log_can_be_turned_off(settings):
    settings, http = make(settings, say("Hi"), traffic_log=False)
    with http:
        http.post("/v1/chat", json=CHAT, headers=AUTH)
        assert http.app.state.traffic is None
    assert entries(settings.logs_dir) == []


@pytest.fixture
def day():
    return {"today": date(2026, 10, 2)}


def test_old_files_are_deleted(tmp_path, day):
    for name in ("traffic-2026-08-01.jsonl", "traffic-2026-09-25.jsonl", "notes.txt"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    log = TrafficLog(tmp_path, retention_days=30, today=lambda: day["today"])
    log.record({"kind": "test"})
    log.flush()
    log.close()
    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["notes.txt", "traffic-2026-09-25.jsonl", "traffic-2026-10-02.jsonl"]


def test_a_new_day_starts_a_new_file(tmp_path, day):
    log = TrafficLog(tmp_path, today=lambda: day["today"])
    log.record({"kind": "first"})
    log.flush()
    day["today"] = date(2026, 10, 3)
    log.record({"kind": "second"})
    log.flush()
    log.close()
    assert [e["kind"] for e in entries(tmp_path)] == ["first", "second"]
    assert (tmp_path / "traffic-2026-10-03.jsonl").exists()
