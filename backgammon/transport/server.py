"""backgammon/transport/server.py -- wave 5C, the serve loop (§4.2, §6, §8).

Owns (§9.2's 5C row):
  - `serve()` on `websockets.asyncio.server.serve`, one asyncio process, one
    port (§3.1).
  - `process_request`: the little bit of HTTP that shares the port --
    `/healthz`, `/metrics`, `/events.jsonl` -- falling through to the
    WebSocket upgrade for everything else (§8.1, §8.2).
  - The origin check (§4.2): built as the `origins=` argument passed
    straight into `serve()`. Rev 2 verified `ServerProtocol.process_origin`
    does the actual matching (exact string, or `re.Pattern.fullmatch`, or a
    bare `None` entry matching an absent `Origin` header) -- this module
    does not re-implement that matching, only supplies the right list.
  - The rate limit (rejection-order step 4, §4.5.3) and the 3-malformed-
    frames `1008` close (step 1's counter) -- both observed through
    `GameSession.reject()`/the frames it emits via `send()`, per 5B's
    handoff note, not by re-parsing client messages.
  - The analytics JSONL sink (§8) -- local file, append-only, pull-based;
    the laptop pulls `/events.jsonl`, the server never pushes.
  - Wiring one `GameSession` (5B) to two seats' worth of WebSocket
    connections, one message loop per connection.

Does NOT own: message parsing/validation (protocol.py), session state
transitions, CAS, the opening-roll timer (session.py). This module never
imports `backgammon.rules` (§3.3) -- game logic reaches this module only
through `backgammon.engine`, and indirectly through `GameSession`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve

from ..dice import production_dice
from .protocol import CloseCode, extract_cid
from .session import PLAYER_SEAT, SEAT_ORDER, GameSession

# === Bind + origins (§4.2, frozen) ==========================================

CLIENT_ORIGIN = "https://backgammon.baxter.solutions"

# §4.5.3 step 4: token bucket, 20 messages / 10 s, burst 10.
RATE_LIMIT_REFILL_PER_S = 20.0 / 10.0
RATE_LIMIT_BURST = 10.0

# §8.1: rotate at 50,000 lines (a full game is ~250 lines).
ANALYTICS_MAX_LINES = 50_000

DEFAULT_EVENTS_PATH = Path("./data/events.jsonl")


def default_host() -> str:
    return "0.0.0.0"                              # NOT 127.0.0.1 -- §4.2


def default_port() -> int:
    return int(os.environ.get("PORT", 8765))       # every free container host injects PORT


def build_origins() -> list[Any]:
    """§4.2's frozen origin list.

    Production: exactly `[CLIENT_ORIGIN]`. Tests, when
    `BACKGAMMON_TEST_ORIGINS=1`: a regex matching any `127.0.0.1:<port>`
    Origin header, PLUS a bare `None` entry -- `websockets`'
    `ServerProtocol.process_origin` matches `None` only when the `Origin`
    header is absent (verified against 16.0 source, §4.2/§4.0), which is
    what `websockets.asyncio.client.connect` sends by default. Both entries
    are load-bearing (§4.2's asymmetry note) -- do not "clean up" either.
    """
    if os.environ.get("BACKGAMMON_TEST_ORIGINS") == "1":
        return [re.compile(r"^http://127\.0\.0\.1:\d+$"), None]
    return [CLIENT_ORIGIN]


# === Analytics sink (§8.1, §8.3) ============================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_event_id() -> str:
    """A sortable-enough, unique-enough local id.

    Not a real ULID (no ULID library ships in `requirements.txt` and this
    module is stdlib + `websockets` only) -- a millisecond timestamp prefix
    (sortable) plus a random suffix (unique under concurrent appends),
    which is all §8.1's at-least-once / dedupe-on-`event_id` contract needs.
    """
    return f"{int(time.time() * 1000):013x}{secrets.token_hex(5)}"


class AnalyticsSink:
    """Append-only local JSONL (§8.1). The laptop PULLS; this class never
    pushes anywhere."""

    def __init__(self, path: Path, *, max_lines: int = ANALYTICS_MAX_LINES) -> None:
        self.path = path
        self.max_lines = max_lines
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._line_count = self._count_lines()

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def _rotate(self) -> None:
        if self.path.exists() and self.path.stat().st_size > 0:
            rotated = self.path.with_name(f"{self.path.stem}.{int(time.time())}{self.path.suffix}")
            self.path.rename(rotated)
        self._line_count = 0

    def append(self, event: str, detail: dict) -> str:
        """Write one line. Returns the new record's `event_id`."""
        if self._line_count >= self.max_lines:
            self._rotate()
        event_id = _new_event_id()
        record = {"event_id": event_id, "ts": _now_iso(), "event": event, **detail}
        line = json.dumps(record, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        self._line_count += 1
        return event_id

    def read_since(self, since: str | None) -> list[str]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if not since:
            return lines
        out: list[str] = []
        found = False
        for line in lines:
            if found:
                out.append(line)
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_id") == since:
                found = True
        # Cursor not found (e.g. rotated away) -- safe fallback is "everything".
        return out if found else lines


# === Host-liveness heartbeat (§6.3's post-Rev-6 ruling) =====================
#
# 🔴 READ THIS BEFORE TOUCHING ANYTHING BELOW. THE STDOUT LINE IS NOT DEBUG
# OUTPUT AND ITS REASON IS NOT GUESSABLE FROM THE CODE.
#
# The sync host is a GitHub Codespace. GitHub documents that traffic to a
# forwarded port does NOT reset the 30-minute idle timer -- *terminal output*
# does: "Terminal activity, either input or output, also resets the idle
# timeout period... However, if you share a port, and then don't interact with
# the codespace, and no terminal output is generated, the codespace will time
# out after the configured period."  And `websockets`' `ping_interval=20`
# keepalives are handled inside the library and print NOTHING -- so the DEFAULT
# configuration does not keep the host alive. A stopped codespace does not wake
# on an inbound request; the game is then permanently dead and no client-side
# retry recovers it (§4.4 item 5, §6.3, §10.3 risk 2).
#
# The line below is the mechanism that prevents that: one line per inbound
# message means PLAYING THE GAME is what keeps the table up.
#
# ⚠️ Ways to "clean this up" that silently break the demo half an hour later:
#   - Deleting it, gating it behind a `--verbose`/DEBUG flag, or sampling it.
#   - Routing it through `logging`, which defaults to stderr and is commonly
#     configured to a file -- stdout to a terminal is the whole point.
#   - Dropping `flush=True`. Python BLOCK-BUFFERS stdout when it is not a TTY,
#     so without the flush the heartbeat can sit in a buffer past the idle
#     window during a quiet stretch of a game and do nothing at all.
#   - Running the server backgrounded with stdout redirected to a file
#     (`nohup ... > log 2>&1 &`). Every line here still executes, `log` fills
#     up and looks perfect, and the codespace idles out anyway. The server
#     MUST run in a foreground codespace terminal --
#     see docs/runbooks/backgammon-demo-day-runbook.md.


def _sniff_type(raw: Any) -> str:
    """Best-effort message `t` for the heartbeat line. Never raises.

    Yes, this decodes a frame that `_dispatch` decodes again a few lines later.
    That is deliberate. Hoisting the decode would put it in front of the rate
    limit and change which exceptions each rejection branch swallows (§4.5.3's
    ordering, and the rate-limited branch's deliberately broad `except`). One
    extra `json.loads` on a <=16 KiB frame, at two-humans-playing-backgammon
    rates, is a much better trade than perturbing rejection order to save it.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        return "<malformed>"
    if isinstance(payload, dict):
        t = payload.get("t")
        return t if isinstance(t, str) else "<no-t>"
    return "<not-object>"


def _keepalive_line(seat: str | None, msg_type: str) -> None:
    """Emit ONE flushed stdout line per inbound message. Load-bearing -- see the
    block comment above. Short on purpose: an operational heartbeat, not an
    audit log (§8's JSONL sink is the audit trail and is unaffected).

    Cannot raise: a broken stdout must never kill a game in progress.
    """
    try:
        print(f"[bg] {_now_iso()} seat={seat or '-'} msg={msg_type}", flush=True)
    except Exception:
        pass


# === Rate limiter (§4.5.3 step 4) ===========================================


class TokenBucket:
    def __init__(self, refill_per_s: float, capacity: float) -> None:
        self.refill_per_s = refill_per_s
        self.capacity = capacity
        self.tokens = capacity
        self._updated = time.monotonic()

    def consume(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_s)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# === Metrics (§8.2) =========================================================


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.monotonic)
    games_started: int = 0
    games_finished: int = 0
    games_abandoned: int = 0
    blocked_visitors: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)
    _game_start_times: dict[str, float] = field(default_factory=dict)
    _durations: list[float] = field(default_factory=list)

    def note_game_started(self, game_id: str) -> None:
        self.games_started += 1
        self._game_start_times[game_id] = time.monotonic()

    def note_game_finished(self, game_id: str) -> None:
        self.games_finished += 1
        start = self._game_start_times.pop(game_id, None)
        if start is not None:
            self._durations.append(time.monotonic() - start)

    def note_game_abandoned(self) -> None:
        self.games_abandoned += 1

    def note_blocked_visitor(self) -> None:
        self.blocked_visitors += 1

    def note_error(self, code: str) -> None:
        self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    @property
    def avg_game_duration_s(self) -> float | None:
        if not self._durations:
            return None
        return sum(self._durations) / len(self._durations)


# === Server-side wiring: one GameSession, per-connection loops =============


@dataclass
class _ConnState:
    bucket: TokenBucket
    malformed_strikes: int = 0
    last_msg_type: str | None = None


class BackgammonServer:
    """Wires one `GameSession` to the WebSocket transport (§3.1: one session
    = one pair of seats = one process). Owns the rate limiter, the 3-strike
    `1008` counter, and analytics -- all observed through `send()`/`reject()`,
    never by re-implementing session logic."""

    def __init__(
        self,
        *,
        dice_factory: Any = production_dice,
        events_path: Path | None = None,
    ) -> None:
        self.sink = AnalyticsSink(events_path or DEFAULT_EVENTS_PATH)
        self.metrics = Metrics()
        self._conns: dict[ServerConnection, _ConnState] = {}
        self._last_state_seq: int | None = None

        self.session = GameSession(
            dice_factory=dice_factory,
            send=self._send,
            close=self._close,
            analytics=self._analytics,
        )
        self.sink.append("session_opened", {"session_id": self.session.state.session_id})

    # --- the callables GameSession drives everything through ---------------

    def _analytics(self, event: str, detail: dict) -> None:
        """5B's own emitter -- currently `opening_unresolved` only (§8.3's
        eleventh event, two call sites: O-11 and O-14)."""
        self.sink.append(event, detail)

    async def _close(self, conn: ServerConnection, code: int, reason: str) -> None:
        try:
            await conn.close(code, reason)
        except Exception:
            pass

    async def _send(self, conn: ServerConnection, frame: dict) -> None:
        """The one seam every server->client frame passes through. Analytics
        and metrics are derived by observing frames here rather than by
        re-implementing any of 5B's transitions."""
        self._observe_frame(conn, frame)
        try:
            await conn.send(json.dumps(frame))
        except Exception:
            # A connection that dropped between queuing and flushing (5B's
            # `_flush` awaits each send in turn) -- nothing to do here, the
            # eventual `connection_lost` handling in the message loop cleans
            # the seat up.
            pass

    # --- analytics/metrics derivation, from frames alone --------------------

    def _observe_frame(self, conn: ServerConnection, frame: dict) -> None:
        t = frame.get("t")

        if t == "blocked":
            self.metrics.note_blocked_visitor()
            self.sink.append("blocked_visitor", {"reason": frame.get("reason")})
            return

        if t == "error":
            code = frame.get("code")
            self.metrics.note_error(code or "")
            state = self._conns.get(conn)
            seat = self.session.seat_of(conn)
            active_seat = None
            game = self.session.state.game
            if game is not None:
                active_seat = PLAYER_SEAT[game.turn]
            self.sink.append("client_error", {
                "code": code,
                "cid": frame.get("cid"),
                "msg_type": state.last_msg_type if state else None,
                "sender_was_active_seat": (
                    (seat == active_seat) if (seat is not None and active_seat is not None) else None
                ),
            })
            if code == "malformed" and state is not None:
                state.malformed_strikes += 1
            return

        if t == "replay":
            self.sink.append("replay_requested", {"game_id": frame.get("game_id")})
            return

        if t != "state":
            return

        # De-dup: a broadcast queues the SAME frame to every seated conn, and
        # an error's unsolicited resend re-emits `state` at an UNBUMPED
        # `state_seq` (§4.5.4) -- both cases must not double-count.
        seq = frame.get("state_seq")
        if seq == self._last_state_seq:
            return
        self._last_state_seq = seq

        ev = frame.get("last_event")
        if not ev:
            return
        kind = ev.get("kind")
        if kind == "game_started":
            self.metrics.note_game_started(frame.get("game_id", ""))
            self.sink.append("game_started", {"game_id": frame.get("game_id")})
        elif kind == "game_finished":
            self.metrics.note_game_finished(frame.get("game_id", ""))
            self.sink.append("game_finished", {
                "game_id": frame.get("game_id"), "seat": ev.get("seat"), "detail": ev.get("detail"),
            })
        elif kind == "opponent_abandoned":
            self.metrics.note_game_abandoned()
            self.sink.append("game_abandoned", {"detail": ev.get("detail")})
        elif kind == "rematch_started":
            self.sink.append("rematch_started", {"seat": ev.get("seat"), "detail": ev.get("detail")})
        elif kind == "seat_left":
            self.sink.append("disconnect", {"seat": ev.get("seat")})
        elif kind in ("turn_ended", "turn_forfeited"):
            self.sink.append("turn_completed", {
                "seat": ev.get("seat"), "game_id": frame.get("game_id"), "detail": ev.get("detail"),
            })

    # --- the per-connection message loop ------------------------------------

    async def handler(self, conn: ServerConnection) -> None:
        state = _ConnState(bucket=TokenBucket(RATE_LIMIT_REFILL_PER_S, RATE_LIMIT_BURST))
        self._conns[conn] = state
        try:
            async for raw in conn:
                await self._dispatch(conn, state, raw)
                if state.malformed_strikes >= 3:
                    await self._close(conn, CloseCode.POLICY, "3 malformed frames")
                    break
        finally:
            self._conns.pop(conn, None)
            await self.session.connection_lost(conn)

    async def _dispatch(self, conn: ServerConnection, state: _ConnState, raw: Any) -> None:
        # ⚠️ HOST-LIVENESS HEARTBEAT -- see `_keepalive_line` and the block
        # comment above it. FIRST statement in the loop, UNCONDITIONALLY, and
        # before the rate limiter: a frame about to be rejected as
        # `rate_limited` or `malformed` resets the codespace idle timer exactly
        # as well as a legal move does, and those are precisely the frames a
        # "log only what we accepted" version would drop. It touches neither
        # the token bucket, the origin check, nor the analytics sink.
        _keepalive_line(self.session.seat_of(conn), _sniff_type(raw))

        # §4.5.3 step 4 -- the rate limit. Checked as a pre-gate: it defends
        # a connection sending faster than the budget regardless of what it
        # is sending, which decode-then-check cannot do for a flood of
        # oversized/garbage frames. A frame that is BOTH malformed and over
        # budget reports `rate_limited`, not `malformed` -- the one place
        # this module's ordering deviates from §4.5.3's literal 1-2-3-4
        # sequence, because steps 1-3 live entirely inside
        # `GameSession.handle_message` with no seam between them and step 4.
        if not state.bucket.consume():
            cid = None
            try:
                payload = json.loads(raw)
                cid = extract_cid(payload)
            except Exception:
                pass
            await self.session.reject(conn, "rate_limited", "slow down", cid)
            return

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            await self.session.reject(conn, "malformed", "frame is not valid JSON", None)
            return

        if isinstance(payload, dict):
            state.last_msg_type = payload.get("t")
        await self.session.handle_message(conn, payload)


# === HTTP-on-the-same-port (§8.1, §8.2) =====================================


def _metrics_payload(srv: BackgammonServer) -> dict:
    st = srv.session.state
    frame = srv.session.state_frame()
    return {
        "uptime_s": time.monotonic() - srv.metrics.started_at,
        "seats_occupied": sum(1 for s in SEAT_ORDER if st.seats[s].status != "open"),
        "phase": frame["phase"],
        "session_id": st.session_id,
        "games_started": srv.metrics.games_started,
        "games_finished": srv.metrics.games_finished,
        "games_abandoned": srv.metrics.games_abandoned,
        "blocked_visitors": srv.metrics.blocked_visitors,
        "avg_game_duration_s": srv.metrics.avg_game_duration_s,
        "errors_by_code": dict(srv.metrics.errors_by_code),
        "events_buffered": srv.sink._line_count,
        "analytics_endpoint_enabled": bool(os.environ.get("ANALYTICS_TOKEN")),
    }


def make_process_request(srv: BackgammonServer):
    """Builds the `process_request` callback for `serve()`. `/healthz`,
    `/metrics`, `/events.jsonl` are handled on this same port; anything else
    falls through (returns `None`) to the WebSocket upgrade (§4.2, §8.1/§8.2).
    """

    def process_request(conn: ServerConnection, request: Any):
        split = urlsplit(request.path)
        path = split.path

        if path == "/healthz":
            return conn.respond(200, "ok")

        if path == "/metrics":
            resp = conn.respond(200, json.dumps(_metrics_payload(srv)))
            resp.headers["Content-Type"] = "application/json"
            return resp

        if path == "/events.jsonl":
            token = os.environ.get("ANALYTICS_TOKEN", "")
            if not token:
                # §8.1 step 2: unset/empty token -> 404, never 401 -- an
                # unset token must not be distinguishable from "no such
                # endpoint," or the endpoint's disabled-ness is discoverable
                # from outside.
                return conn.respond(404, "not found")
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {token}":
                return conn.respond(401, "unauthorized")
            since_vals = parse_qs(split.query).get("since")
            since = since_vals[0] if since_vals else None
            body = "\n".join(srv.sink.read_since(since))
            resp = conn.respond(200, body)
            resp.headers["Content-Type"] = "application/x-ndjson"
            return resp

        return None                                # fall through to the WS upgrade

    return process_request


# === Entry point =============================================================


def create_server(
    *,
    host: str | None = None,
    port: int | None = None,
    dice_factory: Any = production_dice,
    events_path: Path | None = None,
) -> tuple[BackgammonServer, Any]:
    """Returns `(BackgammonServer, serve(...))`. The second element is the
    `websockets.asyncio.server` `Serve` object -- an async context manager --
    left unstarted so callers (production `main()`, or a test fixture binding
    port 0) control the lifecycle."""
    srv = BackgammonServer(dice_factory=dice_factory, events_path=events_path)
    ws = serve(
        srv.handler,
        host if host is not None else default_host(),
        port if port is not None else default_port(),
        origins=build_origins(),
        process_request=make_process_request(srv),
        ping_interval=20,
        ping_timeout=20,
        max_size=16384,
    )
    return srv, ws


async def main() -> None:                          # pragma: no cover -- real entry point
    srv, ws = create_server()
    async with ws as server:
        sockets = server.sockets or []
        for sock in sockets:
            # `flush=True` for the same reason the heartbeat needs it (see the
            # block comment above `_sniff_type`): stdout is block-buffered when
            # it is not a TTY, so without this the "listening on ..." banner is
            # INVISIBLE the moment anyone redirects the output -- which reads,
            # to the person starting the server before a demo, as "it did not
            # start." Measured, not assumed: `python3 -m backgammon.transport.server > log`
            # produced an empty log with the plain `print`.
            print(f"backgammon sync server listening on {sock.getsockname()}", flush=True)
        await asyncio.Future()                      # run forever


if __name__ == "__main__":                          # pragma: no cover
    asyncio.run(main())


__all__ = [
    "CLIENT_ORIGIN",
    "RATE_LIMIT_REFILL_PER_S",
    "RATE_LIMIT_BURST",
    "ANALYTICS_MAX_LINES",
    "default_host",
    "default_port",
    "build_origins",
    "AnalyticsSink",
    "TokenBucket",
    "Metrics",
    "BackgammonServer",
    "make_process_request",
    "create_server",
    "main",
]
