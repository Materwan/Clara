"""`clara-admin`: the server's console, from another computer.

It is the same prompt as the one embedded in `clara-server` (same commands,
same completion), but each line travels to the server over HTTP. It needs an
*admin* token (CLARA_ADMIN_TOKENS on the server), not a chat token.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .commands import CommandResult
from .console import run_console


def explain(error: Exception, url: str) -> str:
    if isinstance(error, httpx.ConnectError):
        return f"! Cannot reach the Clara server at {url}."
    if isinstance(error, httpx.HTTPStatusError):
        try:
            detail = error.response.json().get("detail", error.response.text)
        except ValueError:
            detail = error.response.text
        return f"! Server said {error.response.status_code}: {detail}"
    return f"! {type(error).__name__}: {error}"


async def amain(url: str, token: str, one_shot: str = "") -> None:
    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(10.0, read=60.0),  # /provider and /model call out to Ollama
    ) as http:
        try:
            response = await http.get("/v1/admin/commands")
            response.raise_for_status()
            commands = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SystemExit(explain(error, url)) from None

        async def execute(line: str) -> CommandResult:
            try:
                response = await http.post("/v1/admin/command", json={"line": line})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as error:
                return CommandResult(explain(error, url))
            return CommandResult(body["output"], body["quit"])

        if one_shot:
            result = await execute(one_shot)
            print(result.output)
            return

        await run_console(
            execute,
            commands,
            banner=f"Clara console on {url}. /help for commands, /quit or Ctrl+D to leave.",
            history_path=Path.home() / ".clara_admin_history",
            prompt="clara@remote> ",
        )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="clara-admin", description="Remote Clara console.")
    parser.add_argument("--url", default=os.getenv("CLARA_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.getenv("CLARA_ADMIN_TOKEN"), help="or CLARA_ADMIN_TOKEN")
    parser.add_argument("command", nargs="*", help="run this command and exit, e.g. /status")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("No admin token: pass --token or set CLARA_ADMIN_TOKEN.")
    for stream in (sys.stdout, sys.stderr):  # Windows consoles are not always UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(amain(args.url, args.token, " ".join(args.command)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
