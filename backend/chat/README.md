# chat

The chat module: the Telegram bot and the HTTP API of the conversation. It is part of the single FastAPI backend; its neighbour is the `agents` module.

The bot carries the conversation (the request, clarifications, status, answers), and the Mini App shows the result: the map and the cards ([../trip_map](../trip_map/README.md)). The bot sends an "Open the map" button.

## How the module talks to the agents

Modules do not import each other and do not call each other over HTTP. They communicate through PostgreSQL and two contracts in [../shared](../shared):

```
Telegram / HTTP ──> chat ──(agent_runs + a command)──> agents
                     ^                                   │
                     └──(chat_events, trip_plans)────────┘
```

**Commands** (`shared/commands.py`) are stored in `agent_runs.command` as JSON and carry only ids: the agents read the history and the plan from the database themselves.

| Command | When |
|---|---|
| `ProcessUserMessage` | a new user message |
| `ProcessClarificationAnswer` | an answer to an open question (as text or a button) |

**What the agents module has to do**

1. Take `status='queued'` rows from `agent_runs` in `created_at` order. **At most one `running` per chat**: messages of one trip are processed strictly in order. On Postgres, take them with `SELECT ... FOR UPDATE SKIP LOCKED`.
2. Set `running` / `done` / `failed`. If an answer from the user is needed, create a row in `clarifications` (`question`, optional `options` for buttons, `field`), set the run to `waiting_clarification` and emit `clarification.required`. Such a run does not hold the queue. The answer arrives as a new `ProcessClarificationAnswer` run.
3. Write the plan to `trip_plans` (`state_json` for itself, `view_json` for the map and the cards) and a snapshot to `trip_plan_versions`. The chat only reads the plan: the chat's latest `trip_plans` row is the current one.
4. Publish events with `shared.events.emit_event(session, chat_id, type, payload, run_id)` **in the same transaction** as the data: an event becomes visible only after the commit.
5. The final answer: a `chat_messages` row (`role='assistant'`, `plan_id`, `plan_revision`) and an `assistant.message` event.

| Event | What the user sees in Telegram |
|---|---|
| `request.queued` | set by chat itself: "Got it…", with the position in the queue |
| `agent.progress` | one status message is edited in place, no spam |
| `clarification.required` | the question, with buttons if there are `options` |
| `plan.updated` | the status "Plan updated (version N)" |
| `plan.ready` | the status message is removed |
| `assistant.message` | the answer plus an "Open the map" button |
| `request.failed` | "⚠️ reason", can be retried |

Payload models: [shared/events.py](../shared/events.py). The real agents module is `backend/agents` (the worker `python -m agents.runtime.worker`, a separate service in `docker-compose.yml`). The `DEV_AGENT=true` stub ([dev_agent.py](dev_agent.py)) exists only for demos without an LLM and **must not run alongside the worker**: it answers with a demo plan and asks one clarifying question once.

## No duplicates, and the order of clarifications

One incoming message produces at most one agent run:

- **A redelivery** of the same message (the same `message_id` / `client_message_id`) is ignored.
- **A double send**: the same text while the first one is still `queued` (the agents have not started) is ignored for 60 seconds. An answer to a question is not a double send: «Два» ("two") to "How many guests?" and to "How many rooms?" are two answers.
- **One question, one answer.** Answers close open questions in order, oldest first. A question is closed by an atomic `UPDATE ... WHERE status='open'`, so if two messages race for one question, the second closes the next one, or becomes an ordinary request if there is none.
- Messages of one chat are accepted one at a time (a lock on the chat row in PostgreSQL), so the queue position and the order of questions never drift.

This implies a requirement on the agents: **when you start processing a message, move `chat_messages.status` from `queued` to `processing`** (then `done` / `failed`). While the status is `queued`, a repeat of the same text counts as a double send.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | registers the user, greeting |
| `/plan` | the current plan and the Mini App button |
| `/status` | the open question and the number of requests in progress |
| `/new` | a new trip (the old one is kept, its events no longer reach the chat) |
| `/demo` | a ready demo plan without a conversation: `/demo` (Moscow and Saint Petersburg), `/demo japan` (Tokyo and Osaka). Only with `DEV_AGENT=true`; calling it again creates a new version of the same plan |
| `/help` | a hint |
| any text | a request to the agents, or an answer to the open question |

It works in private messages only. A redelivered Telegram update does not create a second request (de-duplication by `message_id`).

## HTTP API (`/api/v1`)

| Method and path | Purpose |
|---|---|
| `POST /auth/telegram` | Mini App `initData` → a session token (the signature is checked with the bot token; `user_id` is never taken from the client) |
| `GET /chats/active`, `POST /chats` | the active chat; a new trip |
| `GET/POST /chats/{id}/messages` | history; sending (202, `client_message_id` for idempotency) |
| `POST /chats/{id}/clarifications/{qid}/answers` | an answer to a specific question (409 if it is closed) |
| `GET /chats/{id}/events` | SSE; resume with `?after=` or `Last-Event-ID` |
| `GET /chats/{id}/plan`, `GET /plans/{id}/versions/{rev}` | the current plan, a version |
| `POST /telegram/webhook` | the bot webhook (the secret is checked) |

Access to someone else's chat returns 404. The token is signed with `SESSION_SECRET` (or with a key derived from the bot token).

## Running it

```bash
cd backend && python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example ../.env       # a bot token from @BotFather; without Docker you also need the agents worker or DEV_AGENT=true
uvicorn main:app --port 8080
```

- **Without `TELEGRAM_WEBHOOK_URL`** the bot uses long polling: fine for local development, no public address needed.
- **With a webhook** (`TELEGRAM_WEBHOOK_URL=https://<domain>/telegram/webhook`, `TELEGRAM_WEBHOOK_SECRET`) the bot registers it on start. The Mini App button only works when `PUBLIC_BASE_URL` is **https**; for development a tunnel will do (cloudflared, ngrok). Over http the message carries a link instead of a button.
- **The Mini App in Telegram** opens only over https. For development, put a tunnel in front of the app port and set its address:

  ```bash
  ngrok http 8080                     # or cloudflared tunnel --url http://localhost:8080
  # in .env: PUBLIC_BASE_URL=https://<tunnel address>   then restart the app
  ```

  After that the bot's messages carry an "Open the map" button (the plan of that exact version), and next to the input there is a permanent "🗺 Map" button (the latest plan of the active trip). The map loads the plan from the database by `?plan_id=<uuid>` or `?plan_id=latest`, sign-in goes through `initData`, and other people's plans are not reachable. Without `plan_id` a demo example is shown (outside Telegram as well). Free ngrok may show a warning page; if it gets in the way, use cloudflared.
- The default database is the SQLite file `data/dev.db`. For Postgres: `DATABASE_URL=postgresql+asyncpg://...`, `AUTO_CREATE_TABLES=false` and `alembic upgrade head`.
- An unreachable Telegram or a wrong token does not take the API down: the bot is set up in the background and the error goes to the log.

Tests: `pytest` (an in-memory database, Telegram replaced by a recording session, no bot token needed).

## Not done yet

- Delivering events to Telegram assumes **one process**. Several replicas would send duplicates: that needs a lock or `LISTEN/NOTIFY`.
- `GET /chats/{id}/events` polls the database once a second. It could move to `LISTEN/NOTIFY` later.
- There is no way to cancel a request from the chat and no rate limit on messages.
