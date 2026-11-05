# Implementation status and departures from docs/PLAN.md

> The state at submission: the agents branch, the chat with the Telegram bot, trip history and the map are merged.

What was verified, what was not, what is still open. Nothing embellished.

## Verified working

| What | How it was verified |
|---|---|
| The core (operations, permissions, versions, graph, re-planning, scoring, budget, 17 connection rules, checking the numbers in the answer) | unit tests, `backend/tests/test_domain.py`, `test_checks.py` |
| The orchestrator: a complete request with no questions; an incomplete one → one batch of questions → a trip; re-planning (a budget change does not trigger a search, moving dates does); a vague destination → 3 candidates | `test_orchestrator.py` with a recorded Tutu response and a scripted LLM |
| API + queue + worker + SSE with catch-up + idempotency + chat isolation + cart → re-check → payment | `test_api_db.py` on a real PostgreSQL 16 |
| Normalising Tutu responses | on real recorded `search_hotels` / `get_offer_details` responses (`tests/fixtures`) |
| **Live Tutu (28.10)**: hotels, trains, flights, including Moscow→Tokyo→Osaka→Moscow; the plan is assembled entirely from live data, two runs in a row, together with a scripted LLM | a `scripts` run of the orchestrator against `mcp.tutu.ru` |
| Tutu transport normalisation (the price for the group: `price_party` for rail, flights and buses are already for everyone; time zones; stations and airports) | tests on real recorded responses `tests/fixtures/tutu_search_{rail,avia}_*.json` |
| Source modes `auto` / `tutu` / `mock`; retries on irregular `ConnectTimeout` | unit tests and a live run |
| A flight landing the next day: check-in moves to the arrival day, and the other dates follow | unit tests and a live run of the Japan trip |
| The UI in a browser: question → plan → card → moving dates → payment, a map with pins and road transfers | by hand with Playwright + Chrome (scripted LLM) |
| The Telegram `initData` signature, the MCP tool allow-list (creating Tutu carts and orders is forbidden) | unit tests |
| `docker compose up --build` without `.env`: `app` and `db` healthy, `/health`, `/ready`, the UI on `:8080`, a non-root process, `linux/amd64`, no dev packages in the image; without a GigaChat key the user gets an understandable error | by hand on Apple Silicon (amd64 emulation) |
| **The merge**: 210 tests (149 from the chat side plus the agents ones) green on SQLite and on PostgreSQL; migrations 0001–0003 apply, roll back and show no drift (`alembic check`) on PostgreSQL, 0003 on SQLite too; the worker follows the chat contract, guests and Telegram users, questions as `clarifications`, `emit_event` events, accepted history is applied; `view_json` passes the map schema | pytest, Alembic on PG |
| The merged stack in Docker: a clean database, migrations at start, `/health`, `/ready`, the worker, guest sign-in, a message through the API → an understandable error without a key | `docker compose up --build` |
| The UI after the merge in a browser (live Tutu, scripted LLM): question → answer → plan → card → moving dates → payment | Playwright + Chrome |
| A fresh clone of the commit + `requirements-dev.txt` in a clean venv: 211 tests green on SQLite and on PostgreSQL 16 | by hand |
| The "fit for the guest" block: Tutu's per-topic ratings, a disappointment risk (it lowers the score slightly), quotes from real reviews of the chosen hotel, bed type and view as conditions | `tests/test_hotel_fit.py` on recorded Tutu responses (Cosmos Smart: 9.05 and every topic above 8.8; Cosmos VDNKh: Comfort 5.64, Cleanliness 6.87, 1846 reviews) |
| Prompt-injection defence: input, cleaning external data, checking the answer, the prompt preamble | `tests/test_guard.py`: 16 attacks are recognised, 16 ordinary requests are not blocked, swapping the answer for a link or a copy of the prompt leads to the template |
| **An external audit (b2dd43f, 17 findings): F01-F12, F14, F15, F17 fixed**: the cart is bound to the plan's content (a fingerprint) and expires when it changes; payment is atomic and idempotent (one order per cart, migration 0004); a stale node blocks `ready` and cannot be paid for; transport ids carry no price (a re-check finds the same service at its new price anywhere in the results); a repeated city gets its own stay; new dates beat the old nights; "warm sea" is understood in any word form and the travel-time limit is not lifted; the `tutu` mode never substitutes mocks; filter values and a raised price ceiling trigger a new search; a draft from history gets the full plan view; times are shown on the place's clock; soft wishes affect the score; an answer with an invented name or a guarantee is replaced by the template; the metrics count only complete plans without errors | `tests/test_audit_fixes.py` (21 tests), 286 tests green on SQLite and PostgreSQL 16, migration 0004 up/down on PG and SQLite, `alembic check` |
| **GigaChat on a live key**: the key and OAuth, the model list, `GigaChat-2-Max`; «Найди отели в Москве…» reached a recommendation with live Tutu hotels; «Хочу в Питер на выходные» → one batch of questions; «Тёплое море в ноябре с ребёнком» stopped at the question about the party and did not reach the destination choice | a manual `docker compose up --build` run |
| **A visual review by an agent (29.10, 24 defects) and the fixes**: the party is counted by code ("there are four of us" = in total, one adult is not assumed by default), a small edit does not narrow the trip, a flight starts in its own city and the map's bounds include the arcs, the fallback map draws arcs, bus and rail stations are geocoded with the city and a fallback query, "train tickets only" restricts the search, travel time with changes, "cheaper" never raises the price, a 150 s limit per request, an offline banner, layout at 150% zoom | `tests/test_visual_findings.py`, live runs on GigaChat and Tutu, 407 tests green on SQLite (the database part on PostgreSQL 16 too) |

## NOT verified

- **GigaChat quality and speed.** On the free tier parallel requests return 429 (hence `GIGACHAT_MAX_CONCURRENCY=1` and a retry with a pause); `response_format: json_schema` breaks nested schemas (off by default, JSON through the prompt instead); the model sometimes wraps its answer in `{"properties": …}` (unwrapped). One answer takes 20–60 seconds (the three LLM calls per message run one after another): for a demo, run the request in advance. The destination-choice scenario on a live key did not reach the list of destinations. The `eval_scenarios.py` metrics were taken on a live key (see the README).
- **A cart with live Tutu offers**: the price re-check (`reprice` for hotels and transport) is covered by tests with a fake MCP but was not run against the live server.
- **Tutu's stability**: from the development network it sometimes answered and sometimes did not (`ConnectTimeout`); retries were added, but its behaviour under the jury's load is unknown.
- Building and running on someone else's machine and on native amd64 (verified only on one Mac, through emulation).

## Open

00. **Not closed from the audit:** F13 (transfers: a time estimate and a line on the map, but no options, no price and no cart item; the end of the geometry is taken from the city rather than the hotel), F16 (hotel hints are not offered automatically after a destination is chosen). The remaining "dead code" items from the audit (unused fields, `base_revision` never passed) were left on purpose: deleting by scanner output is not safe.

0. **Review quotes and the injection defence were not checked on the live model.** Quotes are picked by code using word stems (no LLM), so sometimes not the most telling sentence is chosen; the attack filter is narrow and does not replace the structural limits (closed schemas, operation permissions, number checks).

1. **Stations and airports without coordinates.** Tutu returns only names, and the geocoder (Nominatim) looks up coordinates: sometimes it finds nothing (Narita airport), and then no transfer is computed for that node and the map shows a straight line between the cities.
2. **Japanese railways are not in Tutu** (Russian Railways and buses cover Russia only): Tokyo→Osaka goes by plane. Japanese hotels are in Tutu.
3. Soft text wishes (`free_text`: "quiet", "near the centre") are not scored: the plan and the answer honestly say that they are not checked automatically (the plan had a separate LLM call for this).

## Departures from docs/PLAN.md

| In the plan | What was done | Why |
|---|---|---|
| its own chat, its own psycopg layer, its own SQL | replaced by the chat module (SQLAlchemy + Alembic) and the `agent_runs`/`clarifications`/`emit_event` contract; the agents' tables added by migration 0003 | one database and one contract; `shared.models` gained `users.is_guest`, a nullable `telegram_id`, `chat_messages.turn`, queue fields, and a partial index for "one running per chat" |
| the LLM returns typed operations | the LLM returns **deltas** (flat fields), and code builds the operations from them | more robust with GigaChat: no discriminated union in the response schema; the guarantees are the same (a closed set, permissions, versions) |
| `plan_id` separate from the chat | `plan_id == chat_id` (one plan per chat) | simpler, enough for v1 |
| a `clarifications` table | questions live in the plan (`state.questions`) | one source of truth |
| scoring soft wishes with a separate LLM call | not implemented (marked honestly) | see "Open", item 3 |
| check-in on the departure day | check-in on the arrival day (`align_arrival_dates`): for long flights, the Japan example from the task | the checker raised `DATES_STAY_ARRIVAL` on every flight landing the next day |
| source modes `tutu` / `mock` | `auto` added and made the default | the jury may be on a network where Tutu does not answer |
| a warm-sea threshold of 24 °C | 22 °C, configurable with `WARM_SEA_MIN_C` | Open-Meteo gives 23.3 °C for Antalya in November: at 24 °C the typical answer to "a warm sea in November" was filtered out |
| price: min-max per node | the ratio "minimum / value" | min-max turned two options into 0 and 1 and let a tiny price difference beat any priority (caught by a test) |
| the main agent's answer entirely from the LLM | code builds the facts and the "why / trade-off / risk" structure, the LLM words the text, the numbers are checked | a defence against invented numbers |
