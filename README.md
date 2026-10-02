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
cp .env.example .env                    # then put real tokens in CLARA_TOKENS (the server
                                        # refuses the "change-me" placeholders)
.venv/bin/clara-server                  # listens on 127.0.0.1:8765
```

In another terminal:

```bash
export CLARA_TOKEN=<one of the tokens>
.venv/bin/clara-chat --user erwan --name Erwan
```

Tests: `pytest`. Lint: `ruff check .` (both run in CI on Linux and Windows, Python 3.11 and 3.12).

## The console

Started in a terminal, `clara-server` shows a prompt beside the server logs
(history, Tab completion). Without a terminal (systemd) or with
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
| `/forget-person <person> [confirm]` | erase a person: accounts, facts and what they said (without `confirm`, only shows what would go) |
| `/link <surface:user> <person>` | make an account belong to a person (merges them) |
| `/stop [now]` | stop the server: tell every client, refuse new questions, wait for running replies and agents, exit (`now`: do not wait); works from `clara-admin` too |
| `/help [command]`, `/quit` | `/quit` stops the server like `/stop` from the embedded console, and only closes a remote one |

### Providers

| id | Name | What it is |
| --- | --- | --- |
| `local` | Local host | Ollama on this machine, or any host in `OLLAMA_HOST`. Model: `CLARA_LOCAL_MODEL` |
| `cloud` | Ollama API key | `ollama.com` with `OLLAMA_API_KEY`. Model: `CLARA_CLOUD_MODEL` |

Models whose name ends in `-cloud` are not local: a local Ollama forwards them to ollama.com (they need
`ollama signin`), and the window Clara requests from a local server (`num_ctx`) cannot be relied on for them,
so set `CLARA_LOCAL_CONTEXT_WINDOW` to the window such a model really has.

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

The model saves and removes facts itself through three tools, `remember`, `forget` and
`recall_facts`, which can only touch the person who is talking. The prompt shows the newest facts that fit
`CLARA_FACTS_TOKEN_BUDGET` tokens (2000) and says how many older ones are left out; `recall_facts` searches
those by words (any case, any accent: "Élan" and "élan" are one fact). Duplicates are detected on the folded
text (Unicode NFKC + case folding + spaces), and databases from earlier versions are migrated at startup.

## API

All routes except `/health` need `Authorization: Bearer <token>`.

| Route | |
| --- | --- |
| `POST /v1/chat` | `{surface, user_id, user_name?, message, conversation?}` → `{reply, conversation, person, tools, usage}`; 413 if it cannot fit the model's window, 504 if the model hangs, 503 if the server is stopping |
| `POST /v1/chat/stream` | same body; Server-Sent Events `turn` / `token` / `tool` / `tool_requests` / `usage` / `compacted` / `warning` / `done` / `error` |
| `POST /v1/turns/{id}/tool-results` | `{results: [{id, content}]}`: a client's answer to a `tool_requests` event (see below) |
| `GET /v1/conversations/{id}` | `{tokens, window, percent, summary, messages}`: how full the context is |
| `POST /v1/conversations/{id}/compact` | `{focus?}` → `{before_percent, after_percent, summary}`: summarise the older messages |
| `GET /v1/memory/facts?surface=&user_id=` | list a person's facts |
| `POST /v1/memory/facts` | `{surface, user_id, text}` |
| `DELETE /v1/memory/facts/{id}?surface=&user_id=` | |
| `POST /v1/reminders` | `{surface, user_id, user_name?, text, at, repeat?, timezone?, conversation?}` → `{id, text, due_at, repeat}`; 422 if `at` is past or not ISO 8601 (see *Reminders*); `conversation` (default: the account's own) is where Clara writes the announcement |
| `GET /v1/reminders?surface=&user_id=` | the person's reminders that have not fired yet |
| `DELETE /v1/reminders/{id}?surface=&user_id=` | cancel one of the person's own reminders |
| `GET /v1/reminders/stream` | Server-Sent Events: `reminder` when one comes due (**every client** gets every reminder, with the message Clara wrote) and `server` (running, stopping, stopped) |
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

### Privacy

`/forget-person <person> confirm` erases a person: their accounts, facts and messages. A conversation only they
took part in goes entirely, answers and summary included. In a conversation shared with other people only their own
messages go, and the answers and summary that remain may still mention them. Messages are kept after a compaction
(the summary stands for them) unless `CLARA_PURGE_SUMMARISED=true`, which deletes them: the summary, which can
contain personal data too, is then the only record. There is no retention limit otherwise.

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

## Reminders

A reminder is a text and a moment. When the moment comes, the server announces it to **every client**,
whoever set it and whichever surface they speak for (`CLARA_CLIENT_SURFACES` does not apply: a reminder
is a broadcast, so the person setting one should know that everybody will read it).

- **Setting one.** `POST /v1/reminders` with `at` as ISO 8601: `2026-10-05T09:00+02:00`, or without an
  offset (`2026-10-05T09:00`), read in `timezone` (an IANA name) or else the server's own timezone.
  `repeat` is `daily`, `weekly` or `monthly`: the same wall-clock time again, and the same day of the
  month, clamped to the month's length (the 31st is the 28th in February, then the 31st again). With a
  `timezone` a repeat follows daylight saving; with only an offset it keeps that offset. The model can do
  it too (`remind`, `list_reminders`, `cancel_reminder`; it reads `when` in the request's `timezone`),
  and `clara-chat` and the console have `/remind`. At most 100 per person, 500 characters each.
- **Clara writes the announcement.** When a reminder comes due, the server has Clara write the message that
  is shown, instead of just the reminder's text: an ordinary turn in the conversation where the reminder was
  set, as the person who set it. She knows what she knows about them, the exchange (`[Reminder due] …` and her
  answer) is kept in their history, and she is told that everybody connected will read it. She has no tools in
  that turn, so a reminder cannot create reminders. One message is written, the same for every client. If she
  cannot write it within `CLARA_REMINDER_AI_TIMEOUT` seconds (60; the model is down or slow), or the reminder
  was set before the server kept where, the reminder's own text is announced. `0` turns this off.
  **Mind that** what she writes may draw on the author's private facts and goes to every client.
- **Receiving them.** A client opens `GET /v1/reminders/stream` and keeps it open (a `: keepalive` comment
  every 15 s; reconnect when it drops). An event is either
  `{"type": "reminder", "id", "text", "message", "due_at", "fired_at", "from"}` (UTC times; `from` is who set
  it; **show `message`**, which is what Clara wrote, or `text` when `message` is null), or
  `{"type": "server", "state", "message"}`, the state of the server (see *Stopping the server*).
- **Missing the moment.** Announced reminders are stored for 7 days, and the server remembers how far each
  *client* (each token) has read. A client that connects after a reminder fired is sent what it missed,
  oldest first, and tells the user: compare `fired_at` with the clock. A client the server has never seen
  starts from now, with no backlog. A reminder is delivered at least once: if the connection drops while
  it is being sent, it comes again on reconnection. Two connections made with the same token both get
  what fires while they are open, but share what they missed.
- **The server was down** at the moment: the reminder fires when it is back. A repeating one that missed
  several occurrences fires once, then waits for its next.
- **Privacy.** `/forget-person` also erases the person's reminders and what they announced. Only the person
  who set a reminder can list or cancel it.

## Stopping the server

`/stop` (in the server's console, or `clara-admin /stop` from another computer with an admin token),
Ctrl+C or SIGTERM stop the server **without cutting anybody off**:

1. every client connected to `GET /v1/reminders/stream` is told: `{"type": "server", "state": "stopping"}`
   (a client that connects meanwhile is told at once);
2. new questions are refused with **HTTP 503** (`Clara is stopping and takes no new question`), and so are
   new compactions; reminders that come due wait for the next start;
3. the server **waits, with no time limit**, for what is running: replies being written, agents waiting for
   their client's tools (`tool-results` are still accepted), summaries, announcements being written;
4. clients get `{"type": "server", "state": "stopped"}`, their streams end, and the process exits.

To stop without waiting: `/stop now`, or a second Ctrl+C. Running turns are then cut, and say so on their
side. `/status` shows `STOPPING` and how many turns are awaited, and the log says so every 30 s. In the
embedded console `/quit` and Ctrl+D do the same as `/stop`. Clients keep trying to reconnect: when the
server is back they get `{"type": "server", "state": "running"}`.

A client that has no event stream open (a script, a bot) is not told; it only sees the 503, or the
connection closing. The three clients of this repository (`clara-chat`, the console, the desktop app) all
listen and say "Clara is stopping / is not running / is running again".

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
  linking.py    single-use codes that prove control of an account before it is linked
  llm.py        LlmBackend interface + Ollama implementation
  providers.py  local / cloud providers, switchable live, saved in runtime.json
  tools.py      tools the model can call
  reminders.py  reminders: parsing, repeats, the scheduler, each client's stream of announcements
  prompt.py     personality file + per-request context
  agent.py      one conversation turn (locks, server and client tools, streaming, storage, compaction)
  compaction.py transcript and summary request for long conversations
  commands.py   the console commands (/provider, /status...), shared by both consoles
  console.py    the interactive prompt (history, completion)
  server.py     FastAPI routes, auth, embedded console
  client.py     clara-chat (also shows reminders, and has /remind)
  admin.py      clara-admin (remote console)
config/system_prompt.md   Clara's personality, re-read when edited
```

## Next steps

- Semantic recall of facts (embeddings).
- A backend that is not Ollama: implement `LlmBackend.stream()` (the two providers, local and cloud, are
  both Ollama).
- Turn the existing Discord bot into a client of this server.
- Scheduled / proactive tasks.
