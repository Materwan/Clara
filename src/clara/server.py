"""The HTTP API. Every client (terminal, Discord, web app...) talks to this and nothing else.

    GET    /health                      no auth
    POST   /v1/chat                     JSON answer
    POST   /v1/chat/stream              Server-Sent Events: token / tool / done / error
    GET    /v1/memory/facts             facts of an account's person
    POST   /v1/memory/facts
    DELETE /v1/memory/facts/{id}
    POST   /v1/accounts/link-code       a code proving control of an account
    POST   /v1/accounts/link            "this account is the same person as that one" (needs the code)
    POST   /v1/turns/{id}/tool-results  a client's answer to a `tool_requests` event
    GET    /v1/conversations/{id}       size of the context, summary
    POST   /v1/conversations/{id}/compact   summarise the older messages
    DELETE /v1/conversations/{id}       forget the thread (facts are kept)
    GET    /v1/admin/commands           console commands, for completion   (admin token)
    POST   /v1/admin/command            run a console command              (admin token)

Chat clients are trusted: the bearer token proves *which client* is calling, and
the client states which user is talking. Give each client its own token, and with
CLARA_CLIENT_SURFACES limit the surfaces (and so the people and conversations) it can reach.
Operator commands need a different kind of token (CLARA_ADMIN_TOKENS), so a
chat client can never switch the provider or edit memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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

from .agent import Agent, ChatRequest, ClientToolTimeout, ModelTimeout, NothingToCompact
from .commands import CommandContext, CommandResult, registry
from .linking import LinkCodes
from .memory import Memory, MergeRefused, Person
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
    message: str = Field(min_length=1, max_length=200_000)
    conversation: str | None = Field(default=None, min_length=1, max_length=200)
    # What a client can add to a turn (see agent.py):
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=200)  # tools it runs itself
    instructions: str = Field(default="", max_length=100_000)  # added to the system prompt
    prefix: str = Field(default="", max_length=50_000)  # put before the message, never summarised
    ephemeral: bool = False  # one-shot job: no persona, no memory, nothing stored
    timezone: str | None = Field(default=None, max_length=64)  # IANA name, e.g. "Europe/Paris"

    def to_request(self) -> ChatRequest:
        return ChatRequest(
            self.surface, self.user_id, self.user_name, self.message, self.conversation,
            tuple(self.tools), self.instructions, self.prefix, self.ephemeral, self.timezone,
        )


class ToolResult(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=2_000_000)


class ToolResultsBody(BaseModel):
    results: list[ToolResult] = Field(max_length=100)


class CompactBody(BaseModel):
    focus: str = Field(default="", max_length=2000)


class FactBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1)


class LinkCodeBody(_Body):
    surface: Surface
    user_id: ExternalId


class LinkBody(_Body):
    surface: Surface  # the account to attach...
    user_id: ExternalId
    code: str = Field(min_length=1, max_length=100)  # ...proved with the code it was given...
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


def require_surface(request: Request, client: str, surface: str) -> None:
    """403 unless the client may speak for `surface` (CLARA_CLIENT_SURFACES)."""
    allowed = request.app.state.settings.client_surfaces.get(client)
    if allowed is not None and surface not in allowed:
        raise HTTPException(403, f"This client may not use the surface {surface!r}")


def require_conversation(request: Request, client: str, conversation: str) -> None:
    """403 unless the conversation belongs to a surface the client may use."""
    allowed = request.app.state.settings.client_surfaces.get(client)
    if allowed is not None and not any(conversation.startswith(f"{surface}:") for surface in allowed):
        raise HTTPException(403, "This client may not use that conversation")


Client = Annotated[str, Depends(authenticate)]
Admin = Annotated[str, Depends(authenticate_admin)]


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


KEEPALIVE_SECONDS = 15.0


async def with_keepalive(events: AsyncIterator[dict], interval: float = KEEPALIVE_SECONDS) -> AsyncIterator[dict | None]:
    """The events, plus a None every `interval` seconds of silence (a client may spend minutes
    running a tool, and idle connections get dropped by proxies)."""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield event
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending
        with contextlib.suppress(Exception):
            await iterator.aclose()  # type: ignore[attr-defined]


def create_app(settings: Settings, providers: ProviderManager | None = None) -> FastAPI:
    memory = Memory(settings.db_path)
    link_codes = LinkCodes()
    for name in settings.unrestricted_clients:
        log.warning(
            "client %r may speak for any surface: set CLARA_CLIENT_SURFACES to limit it", name
        )
    providers = providers or ProviderManager.from_settings(settings)
    agent = Agent(
        memory,
        providers,
        default_toolbox(),
        SystemPrompt(settings.system_prompt_file),
        history_turns=settings.history_turns,
        max_concurrent_llm=settings.max_concurrent_llm,
        max_tool_rounds=settings.max_tool_rounds,
        context_window=lambda: providers.context_window,
        compact_percent=settings.compact_percent,
        keep_recent_turns=settings.keep_recent_turns,
        tool_timeout=settings.tool_timeout,
        first_token_timeout=settings.llm_first_token_timeout,
        idle_timeout=settings.llm_idle_timeout,
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

    def checked(http: Request, client: str, body: ChatBody) -> ChatRequest:
        require_surface(http, client, body.surface)
        if body.conversation:
            require_conversation(http, client, body.conversation)
        request = body.to_request()
        try:
            agent.validate(request)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return request

    @app.post("/v1/chat")
    async def chat(body: ChatBody, client: Client, http: Request) -> dict:
        if body.tools:
            raise HTTPException(422, "Client tools need a stream: use /v1/chat/stream")
        request = checked(http, client, body)
        final: dict | None = None
        try:
            async for event in agent.turn(request, client):
                final = event
        except ModelTimeout as error:
            raise HTTPException(504, str(error)) from None
        except Exception:
            log.exception("chat failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return final or {}

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatBody, client: Client, http: Request) -> StreamingResponse:
        request = checked(http, client, body)

        async def events() -> AsyncIterator[str]:
            try:
                async with contextlib.aclosing(with_keepalive(agent.turn(request, client))) as stream:
                    async for event in stream:
                        yield ": keepalive\n\n" if event is None else sse(event)
            except (ClientToolTimeout, ModelTimeout) as error:
                yield sse({"type": "error", "message": str(error)})
            except Exception:
                log.exception("chat stream failed (client=%s)", client)
                yield sse({"type": "error", "message": "The language model failed"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/memory/facts")
    async def list_facts(
        client: Client, http: Request, surface: Surface, user_id: ExternalId
    ) -> dict:
        require_surface(http, client, surface)
        person = known_person(surface, user_id)
        return {
            "person": {"id": person.id, "name": person.name},
            "facts": [{"id": fact.id, "text": fact.text} for fact in memory.facts(person.id, 1000)],
        }

    @app.post("/v1/memory/facts", status_code=201)
    async def add_fact(body: FactBody, client: Client, http: Request) -> dict:
        require_surface(http, client, body.surface)
        person = memory.resolve(body.surface, body.user_id, body.user_name)
        try:
            fact = memory.add_fact(person.id, body.text)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"created": fact is not None, "id": fact.id if fact else None}

    @app.delete("/v1/memory/facts/{fact_id}")
    async def delete_fact(
        fact_id: int, client: Client, http: Request, surface: Surface, user_id: ExternalId
    ) -> dict:
        require_surface(http, client, surface)
        person = known_person(surface, user_id)
        if not memory.delete_fact(person.id, fact_id):
            raise HTTPException(404, "No such fact for this person")
        return {"deleted": fact_id}

    @app.post("/v1/accounts/link-code")
    async def issue_link_code(body: LinkCodeBody, client: Client, http: Request) -> dict:
        """Step 1, from the client of the account to attach: a code valid for ten minutes."""
        require_surface(http, client, body.surface)
        code = link_codes.issue(body.surface, body.user_id)
        return {"code": code, "expires_in": int(link_codes.lifetime)}

    @app.post("/v1/accounts/link")
    async def link_accounts(body: LinkBody, client: Client, http: Request) -> dict:
        """Step 2, from the client of the person to attach to: the code proves that whoever
        asks controls the account `surface:user_id`. Only the target's surface is checked here,
        since the two accounts usually belong to different clients."""
        require_surface(http, client, body.to_surface)
        target = known_person(body.to_surface, body.to_user_id)
        if not link_codes.redeem(body.surface, body.user_id, body.code):
            raise HTTPException(403, "Wrong or expired link code")
        try:
            person = memory.link_account(body.surface, body.user_id, target)
        except MergeRefused as error:
            raise HTTPException(409, str(error)) from None
        accounts = [f"{surface}:{external}" for surface, external in memory.accounts_of(person.id)]
        return {"person": {"id": person.id, "name": person.name}, "accounts": accounts}

    @app.post("/v1/turns/{turn_id}/tool-results")
    async def tool_results(turn_id: str, body: ToolResultsBody, client: Client) -> dict:
        results = {item.id: item.content for item in body.results}
        try:
            agent.submit_results(turn_id, client, results)
        except KeyError:
            raise HTTPException(404, "No turn of yours is waiting for tool results") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"accepted": len(results)}

    @app.get("/v1/conversations/{conversation:path}")
    async def conversation_info(conversation: str, client: Client, http: Request) -> dict:
        require_conversation(http, client, conversation)
        return agent.context(conversation)

    @app.post("/v1/conversations/{conversation:path}/compact")
    async def compact_conversation(
        conversation: str, body: CompactBody, client: Client, http: Request
    ) -> dict:
        require_conversation(http, client, conversation)
        try:
            before, after = await agent.compact(conversation, body.focus)
        except NothingToCompact as error:
            raise HTTPException(409, str(error)) from None
        except ModelTimeout as error:
            raise HTTPException(504, str(error)) from None
        except Exception:
            log.exception("compaction failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return {
            "before_percent": round(before, 1),
            "after_percent": round(after, 1),
            "summary": memory.state(conversation).summary,
        }

    @app.delete("/v1/conversations/{conversation:path}")
    async def clear_conversation(conversation: str, client: Client, http: Request) -> dict:
        require_conversation(http, client, conversation)
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
