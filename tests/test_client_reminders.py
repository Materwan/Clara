"""clara-chat: setting reminders, and showing the ones the server announces."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from clara.client import ClaraApi, Notices, describe_reminder, format_reminder, listen, parse_remind

PARIS = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=PARIS)


def test_relative_times():
    assert parse_remind("+30m Tea", NOW) == (NOW + timedelta(minutes=30), "", "Tea")
    assert parse_remind("+2h Call mum", NOW)[0] == NOW + timedelta(hours=2)
    assert parse_remind("+3d Rent", NOW)[0] == NOW + timedelta(days=3)


def test_a_time_of_day_is_today_if_it_is_ahead_and_tomorrow_if_it_has_passed():
    assert parse_remind("18:30 Dinner", NOW)[0] == datetime(2026, 10, 2, 18, 30, tzinfo=PARIS)
    assert parse_remind("09:00 Stand-up", NOW)[0] == datetime(2026, 10, 3, 9, 0, tzinfo=PARIS)


def test_tomorrow_and_a_full_date():
    assert parse_remind("tomorrow 9:05 Dentist", NOW)[0] == datetime(2026, 10, 3, 9, 5, tzinfo=PARIS)
    due, _, text = parse_remind("2026-12-24 20:00 Gifts", NOW)
    assert due.astimezone(timezone.utc).replace(tzinfo=None) == datetime(2026, 12, 24, 20, 0) - due.utcoffset()
    assert text == "Gifts"


def test_a_repeat_comes_first():
    due, repeat, text = parse_remind("weekly 09:00 Bins out", NOW)
    assert (due, repeat, text) == (datetime(2026, 10, 3, 9, 0, tzinfo=PARIS), "weekly", "Bins out")


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ("", "Usage"),
        ("daily", "Usage"),
        ("soon Tea", "Cannot read the time"),
        ("25:00 Tea", "Not a time of day"),
        ("2026-02-30 10:00 Tea", "Not a date"),
        ("+30m", "needs a text"),
        ("tomorrow", "Cannot read the time"),
    ],
)
def test_bad_input_is_explained(argument, message):
    with pytest.raises(ValueError, match=message):
        parse_remind(argument, NOW)


def event(**fields) -> dict:
    return {
        "type": "reminder",
        "id": 1,
        "text": "Dentist",
        "due_at": "2026-10-02T10:00:00+00:00",
        "fired_at": "2026-10-02T10:00:00+00:00",
        "from": "Erwan",
        **fields,
    }


def test_a_reminder_that_just_fired_is_shown_plainly():
    shown = format_reminder(event(), datetime(2026, 10, 2, 10, 0, 5, tzinfo=timezone.utc))
    assert "Dentist" in shown and "set by Erwan" in shown
    assert "missed" not in shown


def test_a_reminder_that_fired_while_away_says_so():
    shown = format_reminder(event(), datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc))
    assert "missed, it was due" in shown


def test_describe_lists_id_time_repeat_and_text():
    line = describe_reminder({"id": 7, "text": "Bins", "due_at": "2026-10-05T07:00:00+00:00", "repeat": "weekly"})
    assert line.startswith("[7] ") and line.endswith("(weekly)  Bins")


def test_reminders_wait_while_a_reply_is_being_printed(capsys):
    notices = Notices()
    with notices.busy():
        notices.show(event(text="Later"))
        assert capsys.readouterr().out == ""
    assert "Later" in capsys.readouterr().out
    notices.show(event(text="Now"))
    assert "Now" in capsys.readouterr().out


def test_the_client_receives_a_reminder_from_a_real_server(live):
    app, url = live
    person = app.state.memory.resolve("cli", "erwan", "Erwan")
    app.state.memory.set_reminder_cursor("terminal", 0)
    api = ClaraApi(url, "secret-cli", "erwan", "Erwan", "cli:erwan")

    due = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0)
    created = api.add_reminder(due, "From the client", "daily")
    assert [r["text"] for r in api.reminders()] == ["From the client"]
    api.cancel_reminder(created["id"])
    assert api.reminders() == []

    app.state.memory.add_reminder(person.id, "Announced", datetime.now(timezone.utc))
    asyncio.run(app.state.reminders.fire_due())
    events = iter(api.reminder_events())
    first, second = next(events), next(events)  # the backlog, and what the server is doing
    assert first["text"] == "Announced"
    assert second == {"type": "server", "state": "running", "message": "Clara is running"}


def test_the_message_clara_wrote_is_shown_instead_of_the_reminder_name():
    shown = format_reminder(event(message="Erwan, your dentist is waiting!"), datetime(2026, 10, 2, 10, 0, 5, tzinfo=timezone.utc))
    assert "your dentist is waiting" in shown and "Dentist" not in shown


class Scripted:
    """An api whose connections play the given scenarios in turn (a list of events, or an exception)."""

    def __init__(self, *scenarios, stop):
        self.scenarios, self.stop = list(scenarios), stop

    def reminder_events(self):
        if not self.scenarios:
            self.stop.set()
            return
        scenario = self.scenarios.pop(0)
        if isinstance(scenario, Exception):
            raise scenario
        yield from scenario


class Said:
    def __init__(self):
        self.lines = []

    def say(self, text):
        self.lines.append(text)

    def show(self, event):
        self.lines.append("reminder: " + (event.get("message") or event["text"]))


def listened(*scenarios) -> list[str]:
    stop = threading.Event()
    said = Said()
    listen(Scripted(*scenarios, stop=stop), said, stop, pause=0)
    return said.lines


def server(state):
    return {"type": "server", "state": state}


def test_the_terminal_says_when_the_server_goes_away_and_when_it_is_back():
    lines = listened(
        [server("running"), event(message="Time for the dentist!")],  # the connection then drops
        [server("running")],
    )
    assert lines == [
        "reminder: Time for the dentist!",
        "Clara is not running.",
        "Clara is running again.",
        "Clara is not running.",  # the last scripted connection ends too
    ]


def test_the_terminal_says_when_the_server_is_stopping_then_stopped_once():
    lines = listened([server("running"), server("stopping"), server("stopped")], [server("running")])
    assert lines == [
        "Clara is stopping: she finishes what is running and takes nothing new.",
        "Clara is not running.",
        "Clara is running again.",
        "Clara is not running.",
    ]


def test_a_server_that_is_down_at_the_start_is_said_once():
    lines = listened(httpx.ConnectError("refused"), httpx.ConnectError("refused"), [server("running")])
    assert lines == ["Clara is not running.", "Clara is running again.", "Clara is not running."]
