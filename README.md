# backgammon-demo

A two-player backgammon game: a pure-Python rules engine plus a WebSocket sync
server, and a static browser client that talks to it.

- `backgammon/` — rules engine (`rules.py`, `engine.py`, `dice.py`, `types.py`)
  and the wire encoding (`wire.py`). No I/O, no framework.
- `backgammon/transport/` — the sync server: `server.py` (process entry point),
  `session.py` (the game/session state machine), `protocol.py` (message types
  and error codes).
- `backgammon/client/` — the static client (`index.html`, `game.js`,
  `style.css`). Served separately; it connects back over `wss://`.

Exactly one table, two seats. A third visitor is told the table is full.

## Requirements

Python 3.11+ (the engine uses `StrEnum`) and `websockets==16.0`.

```
pip install -r requirements.txt
```

## Running the sync server

```
python3 -m backgammon.transport.server
```

It binds `0.0.0.0` on `$PORT`, defaulting to **8765**, and prints a line to
stdout for every inbound message. That per-message logging is deliberate — on
hosts that measure idleness by terminal output rather than by network traffic,
it is what keeps the process's host awake while a game is in progress.

**Run it in a foreground terminal.** Backgrounding it with stdout redirected to
a file can mean the output never reaches a terminal, which silently defeats the
keepalive while the log file fills up and looks correct.

### Environment

| Variable | Default | Effect |
|---|---|---|
| `PORT` | `8765` | Listen port. |
| `ANALYTICS_TOKEN` | unset | When set, enables `GET /events.jsonl` behind `Authorization: Bearer <token>`. Unset, that path returns `404` — an unset token is not distinguishable from no such endpoint. |
| `BACKGAMMON_TEST_ORIGINS` | unset | Test-only. Relaxes the `Origin` allowlist to loopback. Never set this in production. |

The server enforces an `Origin` allowlist. In its default configuration it
accepts only the deployed client's origin, so a client connecting from anywhere
else is rejected at the handshake.

### HTTP endpoints on the same port

- `GET /healthz` → `200 ok`
- `GET /metrics` → `200`, JSON (uptime, seats occupied, phase, game counts)
- `GET /events.jsonl` → `404` unless `ANALYTICS_TOKEN` is set

Everything else on the port falls through to the WebSocket upgrade.

### Local state

The server appends analytics events to `./data/events.jsonl`, rotating at
50,000 lines. That directory is gitignored; nothing is pushed anywhere.
