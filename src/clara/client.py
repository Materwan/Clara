"""`clara-chat`: a minimal terminal client. Also the model for writing other clients.

A client does three things: sends `surface` + `user_id` + `message`, reads the
SSE stream, shows the tokens. It owns no memory at all.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Iterator

import httpx
from dotenv import load_dotenv

SURFACE = "cli"

HELP = """\
/facts            what Clara remembers about you
/remember <text>  store a fact
/forget <id>      delete a fact
/link <surface> <id>
                  tell Clara that account (e.g. discord 1234) is you too
/new              start a fresh conversation thread (facts are kept)
/quit             leave"""


class ClaraApi:
    def __init__(self, url: str, token: str, user: str, name: str | None, conversation: str):
        self.user, self.name, self.conversation = user, name, conversation
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=300.0),  # a model can think for a long while
        )

    def identity(self) -> dict:
        return {"surface": SURFACE, "user_id": self.user}

    def stream_chat(self, message: str) -> Iterator[dict]:
        body = {
            **self.identity(),
            "user_name": self.name,
            "message": message,
            "conversation": self.conversation,
        }
        with self.http.stream("POST", "/v1/chat/stream", json=body) as response:
            if response.is_error:
                response.read()
                response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def facts(self) -> list[dict]:
        response = self.http.get("/v1/memory/facts", params=self.identity())
        response.raise_for_status()
        return response.json()["facts"]

    def remember(self, text: str) -> bool:
        response = self.http.post(
            "/v1/memory/facts", json={**self.identity(), "user_name": self.name, "text": text}
        )
        response.raise_for_status()
        return response.json()["created"]

    def forget(self, fact_id: int) -> None:
        self.http.delete(f"/v1/memory/facts/{fact_id}", params=self.identity()).raise_for_status()

    def link(self, surface: str, external_id: str) -> list[str]:
        response = self.http.post(
            "/v1/accounts/link",
            json={
                "surface": surface,
                "user_id": external_id,
                "to_surface": SURFACE,
                "to_user_id": self.user,
            },
        )
        response.raise_for_status()
        return response.json()["accounts"]

    def new_thread(self) -> None:
        self.http.delete(f"/v1/conversations/{self.conversation}").raise_for_status()


def chat(api: ClaraApi, message: str) -> None:
    for event in api.stream_chat(message):
        if event["type"] == "token":
            print(event["text"], end="", flush=True)
        elif event["type"] == "tool":
            print(f"\n  [{event['name']}]", end="", flush=True)
        elif event["type"] == "error":
            print(f"\n! {event['message']}")
    print()


def command(api: ClaraApi, line: str) -> bool:
    """Run a /command. False means: leave."""
    name, _, argument = line.partition(" ")
    argument = argument.strip()
    if name in ("/quit", "/exit"):
        return False
    if name == "/facts":
        facts = api.facts()
        print("\n".join(f"[{fact['id']}] {fact['text']}" for fact in facts) or "(nothing yet)")
    elif name == "/remember" and argument:
        print("Stored." if api.remember(argument) else "Already known.")
    elif name == "/forget" and argument.isdigit():
        api.forget(int(argument))
        print("Forgotten.")
    elif name == "/link" and len(argument.split()) == 2:
        surface, external_id = argument.split()
        print("Accounts: " + ", ".join(api.link(surface, external_id)))
    elif name == "/new":
        api.new_thread()
        print("New thread.")
    else:
        print(HELP)
    return True


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="clara-chat", description="Talk to a Clara server.")
    parser.add_argument("--url", default=os.getenv("CLARA_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.getenv("CLARA_TOKEN"), help="or CLARA_TOKEN")
    parser.add_argument("--user", default=getpass.getuser(), help="your id on the 'cli' surface")
    parser.add_argument("--name", default=None, help="how Clara should call you")
    parser.add_argument("--conversation", default=None, help="thread id (default: cli:<user>)")
    parser.add_argument("message", nargs="*", help="one-shot: send this and exit")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("No token: pass --token or set CLARA_TOKEN.")

    for stream in (sys.stdout, sys.stderr):  # Windows consoles are not always UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    api = ClaraApi(args.url, args.token, args.user, args.name, args.conversation or f"{SURFACE}:{args.user}")

    def handle(line: str) -> bool:
        """Process one input line; False means: leave. Server errors are shown, not fatal."""
        try:
            if line.startswith("/"):
                return command(api, line)
            chat(api, line)
        except httpx.ConnectError:
            print(f"! Cannot reach the Clara server at {args.url}.")
        except httpx.HTTPStatusError as error:
            print(f"! Server said {error.response.status_code}: {error.response.text}")
        return True

    if args.message:
        handle(" ".join(args.message))
        return
    print("Clara — /help for commands, Ctrl+D to leave.")
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line and not handle(line):
            return


if __name__ == "__main__":
    main()
