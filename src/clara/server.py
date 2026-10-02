"""The HTTP API. Every client (terminal, Discord, web app...) talks to this and nothing else.

    GET    /health                      no auth
    POST   /v1/chat                     JSON answer
    POST   /v1/chat/stream              Server-Sent Events: token / tool / done / error
    GET    /v1/memory/facts             facts of an account's person
    POST   /v1/memory/facts
    DELETE /v1/memory/facts/{id}
    POST   /v1/accounts/link            "this account is the same person as that one"
    DELETE /v1/conversations/{id}       forget the thread (facts are kept)
    GET    /v1/admin/commands           console commands, for completion   (admin token)
    POST   /v1/admin/command            run a console command              (admin token)

Chat clients are trusted: the bearer token proves *which client* is calling, and
the client states which user is talking. Give each client its own token.
Operator commands need a different kind of token (CLARA_ADMIN_TOKENS), so a
chat client can never switch the provider or edit memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import Agent, ChatRequest
from .commands import CommandContext, CommandResult, registry
from .memory import Memory, Person
from .prompt import SystemPrompt
from .providers import ProviderManager
from .settings import Settings, SettingsError
from .tools import default_toolbox

log = logging.getLogger("clara")

Surface = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Field(min_length=1, max_length=128)]


class _Body(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value: Any) -> Any:
        # Discord ids are big integers; accept them without making clients stringify
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class ChatBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    message: str = Field(min_length=1, max_length=8000)
    conversation: str | None = Field(default=None, min_length=1, max_length=200)

    def to_request(self) -> ChatRequest:
        return ChatRequest(self.surface, self.user_id, self.user_name, self.message, self.conversation)


class FactBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1)


class LinkBody(_Body):
    surface: Surface  # the account to attach...
    user_id: ExternalId
    to_surface: Surface  # ...to the person who owns this one
    to_user_id: ExternalId


class CommandBody(BaseModel):
    line: str = Field(min_length=1, max_length=2000)


def _caller(request: Request, tokens: dict[str, str]) -> str | None:
    """Name attached to the bearer token of the request, if it is one of `tokens`."""
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not given:
        return None
    caller = None
    for token, name in tokens.items():  # no early exit: constant-ish time
        if secrets.compare_digest(token.encode(), given.strip().encode()):
            caller = name
    return caller


def authenticate(request: Request) -> str:
    """Name of the calling chat client, or 401."""
    caller = _caller(request, request.app.state.settings.tokens)
    if caller is None:
        raise HTTPException(401, "Missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    return caller


def authenticate_admin(request: Request) -> str:
    """Name of the calling operator, or 403 (remote admin off) / 401."""
    tokens = request.app.state.settings.admin_tokens
    if not tokens:
        raise HTTPException(403, "Remote admin is disabled: set CLARA_ADMIN_TOKENS")
    caller = _caller(request, tokens)
    if caller is None:
        raise HTTPException(
            401, "Missing or invalid admin token", headers={"WWW-Authenticate": "Bearer"}
        )
    return caller


Client = Annotated[str, Depends(authenticate)]
Admin = Annotated[str, Depends(authenticate_admin)]


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(settings: Settings, providers: ProviderManager | None = None) -> FastAPI:
    memory = Memory(settings.db_path)
    providers = providers or ProviderManager.from_settings(settings)
    agent = Agent(
        memory,
        providers,
        default_toolbox(),
        SystemPrompt(settings.system_prompt_file),
        history_messages=settings.history_messages,
        max_concurrent_llm=settings.max_concurrent_llm,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        memory.close()

    app = FastAPI(title="Clara", lifespan=lifespan)
    app.state.settings = settings
    app.state.memory = memory
    app.state.agent = agent
    app.state.providers = providers
    app.state.commands = CommandContext(
        settings, memory, agent, providers, time.monotonic(), f"{settings.host}:{settings.port}"
    )

    def known_person(surface: Surface, user_id: ExternalId) -> Person:
        person = memory.find_person(surface, user_id)
        if person is None:
            raise HTTPException(404, f"Nobody known as {surface}:{user_id}")
        return person

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "provider": providers.active, "model": providers.model}

    @app.post("/v1/chat")
    async def chat(body: ChatBody, client: Client) -> dict:
        final: dict | None = None
        try:
            async for event in agent.turn(body.to_request()):
                final = event
        except Exception:
            log.exception("chat failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return final or {}

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatBody, client: Client) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for event in agent.turn(body.to_request()):
                    yield sse(event)
            except Exception:
                log.exception("chat stream failed (client=%s)", client)
                yield sse({"type": "error", "message": "The language model failed"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/memory/facts")
    async def list_facts(client: Client, surface: Surface, user_id: ExternalId) -> dict:
        person = known_person(surface, user_id)
        return {
            "person": {"id": person.id, "name": person.name},
            "facts": [{"id": fact.id, "text": fact.text} for fact in memory.facts(person.id, 1000)],
        }

    @app.post("/v1/memory/facts", status_code=201)
    async def add_fact(body: FactBody, client: Client) -> dict:
        person = memory.resolve(body.surface, body.user_id, body.user_name)
        try:
            fact = memory.add_fact(person.id, body.text)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"created": fact is not None, "id": fact.id if fact else None}

    @app.delete("/v1/memory/facts/{fact_id}")
    async def delete_fact(
        fact_id: int, client: Client, surface: Surface, user_id: ExternalId
    ) -> dict:
        person = known_person(surface, user_id)
        if not memory.delete_fact(person.id, fact_id):
            raise HTTPException(404, "No such fact for this person")
        return {"deleted": fact_id}

    @app.post("/v1/accounts/link")
    async def link_accounts(body: LinkBody, client: Client) -> dict:
        target = known_person(body.to_surface, body.to_user_id)
        person = memory.link_account(body.surface, body.user_id, target)
        accounts = [f"{surface}:{external}" for surface, external in memory.accounts_of(person.id)]
        return {"person": {"id": person.id, "name": person.name}, "accounts": accounts}

    @app.delete("/v1/conversations/{conversation:path}")
    async def clear_conversation(conversation: str, client: Client) -> dict:
        return {"deleted_messages": memory.clear_conversation(conversation)}

    @app.get("/v1/admin/commands")
    async def admin_commands(admin: Admin) -> list[dict]:
        return registry.describe(app.state.commands)

    @app.post("/v1/admin/command")
    async def admin_command(body: CommandBody, admin: Admin) -> dict:
        log.info("admin %s ran /%s", admin, body.line.lstrip("/").split(None, 1)[0])
        result = await registry.execute(body.line, app.state.commands)
        return {"output": result.output, "quit": result.quit}

    return app


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )


async def serve(app: FastAPI, settings: Settings, with_console: bool) -> None:
    """Run the HTTP server, plus the interactive console on the same event loop."""
    if not with_console:
        await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
        return

    from prompt_toolkit.patch_stdout import patch_stdout

    from .console import run_console

    # Everything is created inside patch_stdout so server logs print above the prompt
    with patch_stdout():
        configure_logging()  # again: the handler must capture the patched stderr
        server = uvicorn.Server(
            uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False)
        )
        server_task = asyncio.create_task(server.serve())
        while not server.started and not server_task.done():
            await asyncio.sleep(0.05)
        if server_task.done():  # could not start (port in use...): surface the reason
            await server_task
            return

        context: CommandContext = app.state.commands

        async def execute(line: str) -> CommandResult:
            return await registry.execute(line, context)

        console_task = asyncio.create_task(
            run_console(
                execute,
                registry.describe(context),
                banner="Clara console. /help for commands, /quit or Ctrl+D stops the server.",
                history_path=settings.data_dir / "console_history.txt",
            )
        )
        await asyncio.wait({server_task, console_task}, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        console_task.cancel()
        await asyncio.gather(server_task, console_task, return_exceptions=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clara-server", description="Run the Clara server.")
    parser.add_argument(
        "--no-console",
        action="store_true",
        help="no interactive prompt (this is the default when not in a terminal)",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except SettingsError as error:
        raise SystemExit(str(error)) from None
    configure_logging()
    with_console = not args.no_console and sys.stdin.isatty() and sys.stdout.isatty()
    try:
        asyncio.run(serve(create_app(settings), settings, with_console))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
