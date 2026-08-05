# backgammon-demo

A two-player backgammon game: a pure-Python rules engine plus a WebSocket sync
server, and a static browser client that talks to it.

- `backgammon/` — rules engine (`rules.py`, `engine.py`, `dice.py`, `types.py`)
  and the wire encoding (`wire.py`). No I/O, no framework.
- `backgammon/transport/` — the sync server: `server.py` (process entry point),
  `session.py` (the game/session state machine), `protocol.py` (message types
  and error codes).
- `index.html`, `game.js`, `style.css` (repo root) — the static client. Served
  separately from the sync server; it connects back over `wss://`.

Exactly one table, two seats. A third visitor is told the table is full.

## Client hosting

The client is served by GitHub Pages from this repository's **`main` branch,
root folder**, at <https://backgammon.baxter.solutions> (see `CNAME`). The three
client files live at the repo root specifically so Pages serves `index.html` at
the site root rather than under a subpath; `.nojekyll` disables Jekyll so the
files are published verbatim.

The sync server's `Origin` allowlist contains only that one origin, so the
client is expected to work **only** when loaded from the custom domain — not
from `*.github.io`, and not from a `file://` copy.

### Pointing the client at a sync server

`game.js` resolves its WebSocket endpoint in this order:

1. a `?ws=wss://…` query-string parameter,
2. `window.BACKGAMMON_WS`, if a script sets it before `game.js` loads,
3. the `SYNC_HOST_WS_URL` constant at the top of `game.js`.

Only `wss://` is accepted — a browser hard-blocks plaintext `ws://` from an
HTTPS page as mixed content.

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
