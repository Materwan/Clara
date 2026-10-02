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
Two accounts become one person in two steps, so nobody can claim someone else's account: the
account to attach asks its own client for a code (`POST /v1/accounts/link-code`, or `/linkcode` in
`clara-chat`; valid 10 minutes, usable once), then the client of the person it joins sends it with
`POST /v1/accounts/link` (or `/link discord 1234 <code>`). Facts and history are merged, but only
if at most one of the two already has memories: merging two filled accounts cannot be undone, so
an operator does it from the server console (`/link`).

The model saves and removes facts itself through two tools, `remember` and
`forget`, which can only touch the person who is talking.

## API

All routes except `/health` need `Authorization: Bearer <token>`.

| Route | |
| --- | --- |
| `POST /v1/chat` | `{surface, user_id, user_name?, message, conversation?}` → `{reply, conversation, person, tools, usage}`; 413 if it cannot fit the model's window, 504 if the model hangs |
| `POST /v1/chat/stream` | same body; Server-Sent Events `turn` / `token` / `tool` / `tool_requests` / `usage` / `compacted` / `warning` / `done` / `error` |
| `POST /v1/turns/{id}/tool-results` | `{results: [{id, content}]}`: a client's answer to a `tool_requests` event (see below) |
| `GET /v1/conversations/{id}` | `{tokens, window, percent, summary, messages}`: how full the context is |
| `POST /v1/conversations/{id}/compact` | `{focus?}` → `{before_percent, after_percent, summary}`: summarise the older messages |
| `GET /v1/memory/facts?surface=&user_id=` | list a person's facts |
| `POST /v1/memory/facts` | `{surface, user_id, text}` |
| `DELETE /v1/memory/facts/{id}?surface=&user_id=` | |
| `POST /v1/accounts/link-code` | `{surface, user_id}` → `{code, expires_in}`: proof of control of that account |
| `POST /v1/accounts/link` | `{surface, user_id, code, to_surface, to_user_id}`; 403 bad code, 409 both accounts have memories |
| `DELETE /v1/conversations/{id}` | forget a thread, keep the facts |
| `GET /health` | no auth; shows the active provider and model |
| `GET /v1/admin/commands`, `POST /v1/admin/command` | `{line}` → `{output, quit}`; **admin token** only (used by `clara-admin`) |

`conversation` defaults to `<surface>:<user_id>` (a private thread). A group
client such as a Discord channel should pass its own id (`discord:channel:42`);
messages from other people in that thread reach the model prefixed with their name.

Interactive docs: `http://127.0.0.1:8765/docs`.

### Tools that run on the client

A client can give Clara tools of its own: files, a shell, a calendar... whatever lives on *its*
machine. The request of `/v1/chat/stream` takes more fields:

| Field | |
| --- | --- |
| `tools` | tools the client runs itself, as function schemas (`{"type": "function", "function": {"name", "description", "parameters"}}`). Names must not collide with the server's (`remember`, `forget`) |
| `instructions` | text added to the system prompt (what this client is for, how to use its tools) |
| `prefix` | text shown to the model before the message, kept in the history but left out of summaries (e.g. the date) |
| `ephemeral` | a one-shot job: no Clara persona, no memory, no stored history, no server tools; the system prompt is just `instructions`. Used for sub-agents |
| `timezone` | IANA name (`Europe/Paris`) for the date and time the model is told; default: the server's own |

When the model calls one of the client's tools, the stream sends

```
event: tool_requests
data: {"type": "tool_requests", "turn": "<id>", "calls": [{"id": "call_0_0", "name": "read_file", "arguments": {...}}]}
```

and waits. The client runs the tools, then posts `{"results": [{"id": "call_0_0", "content": "..."}]}` (one entry
per call, no more, no fewer) to `/v1/turns/<id>/tool-results`; the same stream goes on with the next model round. The
connection stays open meanwhile (a `: keepalive` comment is sent every 15 s). Only the client that opened
the turn can answer it. Closing the stream gives the turn up; a client silent for `CLARA_TOOL_TIMEOUT` seconds
ends it with an `error` event. A model slot is held only while the model works, never while a client does.
`/v1/chat` (no stream) cannot carry client tools.

The tool calls and their results are stored with the conversation, so the model remembers what it did; the outputs
of all but the 8 most recent tool calls are replaced by a short note when the history is replayed.

The system prompt only holds the date, and the time of day is added to the newest user message (not
stored), so the prompt and the replayed history stay identical from one turn to the next and Ollama can
reuse its cache of them.

### Long conversations

The `done` event reports `context: {tokens, window, percent}`. When a conversation fills `CLARA_COMPACT_PERCENT`
of the provider's window, the server asks the model for a summary of the older messages (a `compacted` event says
so) and from then on sends the summary instead of them; the messages stay in the database. `POST
/v1/conversations/{id}/compact` does it on demand, with an optional focus. The last
`CLARA_KEEP_RECENT_TURNS` turns (2) are not summarised: the model goes on from them word for word. A
conversation that is too long for one summary request is summarised in several steps, each continuing
the previous one, so no message is skipped. The same happens when a conversation has more turns than `CLARA_HISTORY_TURNS`: the oldest
are summarised (half of the history is kept) instead of silently falling out of the prompt, as long
as `CLARA_COMPACT_PERCENT` is not 0. The summary is not meant to shrink conversations that are
already short.

A prompt is never left to be truncated silently. Before each model round the server estimates its size
(messages, tool calls and schemas, whatever the model reports): above 95% of the window it summarises the
older turns, then leaves the oldest replayed turns out (with a `warning` event), and if it still does not fit
the turn ends with an error: HTTP 413 on `/v1/chat`, an `error` event on the stream. A single message
taking more than half of the window is refused at once. The context size reported to clients is the larger of
the model's figure and the estimate, since a prompt cache can make Ollama report only what it evaluated.

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
- A model slot is held only while the model works: a client that reads its stream slowly, or not at
  all, never keeps one busy. A model that stops answering is given up on after
  `CLARA_LLM_FIRST_TOKEN_TIMEOUT` / `CLARA_LLM_IDLE_TIMEOUT` seconds (an `error` event, or 504).
- Memory is written only by the server process, so clients never conflict.

## Security

- Tokens are required; the server refuses to start without any. Use one token
  per client so one can be revoked alone.
- Clients are **trusted** within their surfaces: a token proves which client calls, and the client
  says who is speaking. `CLARA_CLIENT_SURFACES` (`terminal=cli|console,discord=discord`) limits each
  token to its surfaces: facts, conversations and accounts of other surfaces answer 403. A client
  with no entry may use any surface (the server warns at startup). Do not hand a token to anything
  you don't control.
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
  agent.py      one conversation turn (locks, server and client tools, streaming, storage, compaction)
  compaction.py transcript and summary request for long conversations
  commands.py   the console commands (/provider, /status...), shared by both consoles
  console.py    the interactive prompt (history, completion)
  server.py     FastAPI routes, auth, embedded console
  client.py     clara-chat
  admin.py      clara-admin (remote console)
config/system_prompt.md   Clara's personality, re-read when edited
```

## Next steps

- Semantic recall of facts (embeddings).
- A second LLM provider: implement `LlmBackend.stream()`.
- Turn the existing Discord bot into a client of this server.
- Scheduled / proactive tasks.
