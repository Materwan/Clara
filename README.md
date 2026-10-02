# Clara server

One AI, one memory, many clients. Clara runs as a small HTTP server and is the
**only** process that touches the memory. A terminal, a Discord bot or any app
are thin clients: they send a message, they show the answer.

```
 terminal (clara-chat) ─┐
 Discord adapter ───────┼─►  Clara server  ─►  Ollama
 your other app ────────┘     │
                          SQLite memory
```

Runs the same on Linux and Windows (pure Python, SQLite, no native extension).

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
cp .env.example .env                    # then put real tokens in CLARA_TOKENS
.venv/bin/clara-server                  # listens on 127.0.0.1:8765
```

In another terminal:

```bash
export CLARA_TOKEN=<one of the tokens>
.venv/bin/clara-chat --user erwan --name Erwan
```

Tests: `pytest`.

## The console

Started in a terminal, `clara-server` shows a prompt beside the server logs
(history, Tab completion). Without a terminal (systemd, Docker) or with
`--no-console`, there is no prompt.

To use the same console **from another computer**, set `CLARA_ADMIN_TOKENS` on
the server and run:

```bash
clara-admin --url http://my-server:8765 --token <admin token>   # or CLARA_URL / CLARA_ADMIN_TOKEN
clara-admin /status                                              # one command, then exit
```

Both consoles run the same commands (the `/` is optional):

| Command | |
| --- | --- |
| `/provider [local\|cloud]` | show the providers, or switch where the model runs |
| `/model [name]` | list the active provider's models, or change its model |
| `/status` | provider, model, uptime, running turns, tokens, memory size |
| `/people` | everybody Clara knows, with accounts and fact counts |
| `/facts <person> [add <text>\|del <id>]` | read or edit what Clara knows (person = id, `surface:user` or name) |
| `/link <surface:user> <person>` | make an account belong to a person (merges them) |
| `/help [command]`, `/quit` | `/quit` stops the server from the embedded console, and only closes a remote one |

### Providers

| id | Name | What it is |
| --- | --- | --- |
| `local` | Local host | Ollama on this machine, or any host in `OLLAMA_HOST`. Model: `CLARA_LOCAL_MODEL` |
| `cloud` | Ollama API key | `ollama.com` with `OLLAMA_API_KEY`. Model: `CLARA_CLOUD_MODEL` |

`/provider cloud` switches every client at once, without a restart, then checks
that the provider answers (and, for `cloud`, that the key is accepted).
The choice and the models picked with `/model` are saved in `data/runtime.json`
and survive restarts. The API key only ever lives in the environment: it is not
saved, printed, or reachable through the API.

Remote admin tokens are separate from chat tokens: a Discord adapter cannot
switch the provider or edit memory, and an admin token cannot chat.

## How the memory works

| What | Follows | Example |
| --- | --- | --- |
| **Facts** ("likes jazz") | the *person*, on every surface | told in the terminal, known on Discord |
| **History** (last messages) | the *conversation* | a Discord channel and a terminal session are separate threads |

A client identifies the speaker as `surface` + `user_id` (`cli`/`erwan`,
`discord`/`1234`). Each pair is an *account*; accounts are tied to a *person*.
Two accounts become one person with `POST /v1/accounts/link` (or `/link
discord 1234` in `clara-chat`): their facts and history are merged.

The model saves and removes facts itself through two tools, `remember` and
`forget`, which can only touch the person who is talking.

## API

All routes except `/health` need `Authorization: Bearer <token>`.

| Route | |
| --- | --- |
| `POST /v1/chat` | `{surface, user_id, user_name?, message, conversation?}` → `{reply, conversation, person, tools, usage}` |
| `POST /v1/chat/stream` | same body; Server-Sent Events `token` / `tool` / `done` / `error` |
| `GET /v1/memory/facts?surface=&user_id=` | list a person's facts |
| `POST /v1/memory/facts` | `{surface, user_id, text}` |
| `DELETE /v1/memory/facts/{id}?surface=&user_id=` | |
| `POST /v1/accounts/link` | `{surface, user_id, to_surface, to_user_id}` |
| `DELETE /v1/conversations/{id}` | forget a thread, keep the facts |
| `GET /health` | no auth; shows the active provider and model |
| `GET /v1/admin/commands`, `POST /v1/admin/command` | `{line}` → `{output, quit}`; **admin token** only (used by `clara-admin`) |

`conversation` defaults to `<surface>:<user_id>` (a private thread). A group
client such as a Discord channel should pass its own id (`discord:channel:42`);
messages from other people in that thread reach the model prefixed with their name.

Interactive docs: `http://127.0.0.1:8765/docs`.

### Writing a client

```python
import httpx, json
with httpx.stream("POST", "http://127.0.0.1:8765/v1/chat/stream",
                  headers={"Authorization": "Bearer <token>"},
                  json={"surface": "web", "user_id": "42", "message": "hello"}) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])   # {"type": "token", "text": "..."} ...
```

`src/clara/client.py` is a complete example.

## Simultaneous use

- Turns in the **same conversation** are answered one after the other.
- Turns in **different conversations** run in parallel, up to
  `CLARA_MAX_CONCURRENT_LLM` model calls at once.
- Memory is written only by the server process, so clients never conflict.

## Security

- Tokens are required; the server refuses to start without any. Use one token
  per client so one can be revoked alone.
- Clients are **trusted**: a token proves which client calls, and the client
  says who is speaking. Do not hand a token to anything you don't control.
- It listens on `127.0.0.1` by default. To reach it from another machine, put
  it behind a VPN such as Tailscale or an HTTPS reverse proxy rather than
  exposing the port.
- Facts are given to the model as *data*; the prompt says they are not instructions.

## Running on a Linux server

See `deploy/clara.service` (systemd). Ollama must be reachable from the server
(`OLLAMA_HOST`), or use a `-cloud` model.

## Layout

```
src/clara/
  settings.py   environment configuration
  memory.py     SQLite: people, accounts, facts, history
  llm.py        LlmBackend interface + Ollama implementation
  providers.py  local / cloud providers, switchable live, saved in runtime.json
  tools.py      tools the model can call
  prompt.py     personality file + per-request context
  agent.py      one conversation turn (locks, tools, streaming, storage)
  commands.py   the console commands (/provider, /status...), shared by both consoles
  console.py    the interactive prompt (history, completion)
  server.py     FastAPI routes, auth, embedded console
  client.py     clara-chat
  admin.py      clara-admin (remote console)
config/system_prompt.md   Clara's personality, re-read when edited
```

## Next steps

- Summaries of old history, and semantic recall of facts (embeddings).
- A second LLM provider: implement `LlmBackend.stream()`.
- Turn the existing Discord bot into a client of this server.
- Scheduled / proactive tasks.
