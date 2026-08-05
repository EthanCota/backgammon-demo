"""backgammon/transport/protocol.py -- the message-layer contract (§4.2, §4.5).

Owns:
  - construction/validation of all 7 client -> server message types (§4.2.2)
  - construction of all 5 server -> client message types (§4.2.3)
  - the ten wire error codes, the close codes (§4.2.4), and the machinery to
    emit both (§4.5.4)
  - the parts of THE REJECTION ORDER (§4.5.3) that are pure functions of a
    message or an engine exception alone -- see the module-level note above
    REJECTION_ORDER for exactly which steps that is and which are 5B's.

Does NOT own: session state, seats, CAS application, the opening-roll phase,
timers, or the server loop -- those are transport/session.py (5B) and the
server loop (5C). This module never imports asyncio and mutates nothing.

`transport/` MAY import asyncio/time in general (§3 binding rule), but this
module in particular needs neither -- it is pure message shape + mapping.

Frozen contract: §4.2.2 (7 client types), §4.2.3 (5 server types), §4.2.4
(close codes), §4.5.1-4.5.3 (authority, CAS, rejection order), §4.5.4 (the
ten error codes).

wire.py already owns GameState/Board/Move serialization (board_to_wire,
game_to_wire, move_from_wire, frame_to_wire) -- this module does not
duplicate any of that. Callers pass already wire-serialized board/dice/move
payloads into this module's `state`/`replay` constructors.
"""

from __future__ import annotations

from typing import Any, Final, NamedTuple

from ..engine import (
    EngineError,
    IllegalMove,
    Malformed,
    NotAPrefix,
    NothingToUndo,
    OpeningUnresolved,
    TurnNotComplete,
    WrongPhase,
)


# === The ten error codes (§4.5.4) -- frozen at exactly ten ===

ERROR_CODES: Final[frozenset[str]] = frozenset({
    "malformed",
    "not_seated",
    "protocol_mismatch",
    "rate_limited",
    "not_your_turn",
    "wrong_phase",
    "stale_state",
    "illegal_move",
    "not_a_prefix",
    "nothing_to_undo",
})


# === Close codes (§4.2.4) ===

class CloseCode:
    """WebSocket close codes, §4.2.4."""
    NORMAL = 1000              # no reconnect
    POLICY = 1008               # 3 malformed frames on one connection
    SEAT_REBOUND = 4000          # duplicate tab; no retry
    PROTOCOL_MISMATCH = 4001    # location.reload(true)
    BLOCKED = 4003               # no seat; do not retry-loop
    GRACE_EXPIRED = 4004         # offer rejoin
    ABNORMAL = 1006              # backoff reconnect
    SERVER_ERROR = 1011          # backoff reconnect


class ProtocolError(Exception):
    """Raised by this module's parsers/checks when a frame fails validation.

    Carries the wire error `code` (one of ERROR_CODES) and a human `message`.
    Callers (5B) catch this and route it through construct_error().
    """

    def __init__(self, code: str, message: str):
        assert code in ERROR_CODES, f"unknown error code: {code!r}"
        super().__init__(message)
        self.code = code
        self.message = message


def extract_cid(payload: Any) -> str | None:
    """Best-effort `cid` for an error frame's echo (§4.5.4: "echo of the
    client's cid, or null"). Works even on a payload that failed shape
    validation elsewhere. Never raises.
    """
    if isinstance(payload, dict):
        cid = payload.get("cid")
        if isinstance(cid, str):
            return cid
    return None


def construct_error(code: str, message: str, cid: str | None, state_seq: int) -> dict:
    """Build an `error` frame (§4.5.4).

    Per §4.2.3: "Always followed immediately by an unsolicited fresh `state`
    on the same connection" -- sending that state is 5B's job, not this
    function's.
    """
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")
    return {"t": "error", "code": code, "message": message, "cid": cid, "state_seq": state_seq}


# === Engine exception -> wire error code (§4.5.3 step 8) ===

def map_engine_error(exc: EngineError) -> str | None:
    """Map an engine exception to a wire error code (§4.5.3 step 8):

        NotAPrefix -> not_a_prefix
        other IllegalMove -> illegal_move
        NothingToUndo -> nothing_to_undo
        TurnNotComplete -> wrong_phase
        WrongPhase -> wrong_phase
        Malformed -> malformed
        OpeningUnresolved -> None (NO wire code -- Rev 3: caught at its
            single call site and routed into the abandoned recovery path,
            §4.3.6; never returned to a client, never kills a socket. Caller
            MUST special-case a None return.)

    NotAPrefix is a subclass of IllegalMove (backgammon/rules.py) so it is
    checked first -- order here is load-bearing.
    """
    if isinstance(exc, OpeningUnresolved):
        return None
    if isinstance(exc, NotAPrefix):
        return "not_a_prefix"
    if isinstance(exc, IllegalMove):
        return "illegal_move"
    if isinstance(exc, NothingToUndo):
        return "nothing_to_undo"
    if isinstance(exc, TurnNotComplete):
        return "wrong_phase"
    if isinstance(exc, WrongPhase):
        return "wrong_phase"
    if isinstance(exc, Malformed):
        return "malformed"
    raise ValueError(f"unmapped engine error type: {type(exc).__name__}")


# === Rejection order (§4.5.3) -- what this module owns vs. what 5B owns ===
#
# The order is frozen and numbered 1-8, first failure wins. Steps 2, 4, 5, 6,
# 7 need session state this module deliberately does not hold (is this
# connection seated? token bucket? active_seat? current phase? state_seq
# CAS?) -- that is transport/session.py's job, including the full E1/E2/E3
# opening-roll exemption logic of §4.5.2b, which is phase/session-dependent
# through and through.
#
# This module supplies the machinery for the steps -- and sub-steps -- that
# are pure functions of the message/exception alone:
#
#   1. shape/malformed              -> parse_client_message() (raises ProtocolError)
#   3. protocol != 1 (on `hello`)   -> check_hello_protocol()
#   8. engine exception -> code     -> map_engine_error()
#
# 5B calls these, in order, interleaved with its own session-state checks
# for steps 2/4/5/6/7, and turns any resulting code into a frame via
# construct_error().
REJECTION_ORDER: Final[tuple[str, ...]] = (
    "1_malformed",
    "2_not_seated",
    "3_protocol_mismatch",
    "4_rate_limited",
    "5_not_your_turn",
    "6_wrong_phase",
    "7_stale_state",
    "8_engine_error",
)


# === Client -> server message types (§4.2.2, 7 types) ===

class HelloMessage(NamedTuple):
    protocol: int
    resume_token: str | None


class RollMessage(NamedTuple):
    cid: str
    # Present + checked (CAS) in the ordinary awaiting_roll flow; accepted
    # and IGNORED during opening_roll (§4.2.2, §4.5.2b E2). Optional here
    # because the wire shape itself omits it on the opening-roll frame.
    state_seq: int | None


class MoveMessage(NamedTuple):
    state_seq: int
    cid: str
    from_: int | str  # 1..24 or "bar" -- range/literal check is wire.move_from_wire's job
    die: int


class UndoMessage(NamedTuple):
    state_seq: int
    cid: str


class EndTurnMessage(NamedTuple):
    state_seq: int
    cid: str


class RematchMessage(NamedTuple):
    cid: str
    want: bool
    state_seq: int | None  # accepted, ignored (§4.5.2a)


class ReplayRequestMessage(NamedTuple):
    cid: str
    state_seq: int | None  # accepted, ignored (§4.5.2a)


ClientMessage = (
    HelloMessage
    | RollMessage
    | MoveMessage
    | UndoMessage
    | EndTurnMessage
    | RematchMessage
    | ReplayRequestMessage
)


CLIENT_MESSAGE_TYPES: Final[frozenset[str]] = frozenset({
    "hello", "roll", "move", "undo", "end_turn", "rematch", "replay_request",
})


def _is_int(v: Any) -> bool:
    """True for genuine ints, false for bool (bool is an int subclass)."""
    return isinstance(v, int) and not isinstance(v, bool)


def _require_str(p: dict, key: str, t: str) -> str:
    v = p.get(key)
    if not isinstance(v, str):
        raise ProtocolError("malformed", f"{t}.{key} must be string, got {type(v).__name__}")
    return v


def _require_int(p: dict, key: str, t: str) -> int:
    v = p.get(key)
    if not _is_int(v):
        raise ProtocolError("malformed", f"{t}.{key} must be int, got {type(v).__name__}")
    return v


def _optional_int(p: dict, key: str, t: str) -> int | None:
    v = p.get(key)
    if v is not None and not _is_int(v):
        raise ProtocolError("malformed", f"{t}.{key} must be int if present, got {type(v).__name__}")
    return v


def _parse_hello(p: dict) -> HelloMessage:
    protocol = p.get("protocol")
    if not _is_int(protocol):
        raise ProtocolError("malformed", f"hello.protocol must be int, got {type(protocol).__name__}")
    resume_token = p.get("resume_token")
    if resume_token is not None and not isinstance(resume_token, str):
        raise ProtocolError("malformed", "hello.resume_token must be string or null")
    return HelloMessage(protocol=protocol, resume_token=resume_token)


def _parse_roll(p: dict) -> RollMessage:
    cid = _require_str(p, "cid", "roll")
    state_seq = _optional_int(p, "state_seq", "roll")
    return RollMessage(cid=cid, state_seq=state_seq)


def _parse_move(p: dict) -> MoveMessage:
    state_seq = _require_int(p, "state_seq", "move")
    cid = _require_str(p, "cid", "move")
    from_val = p.get("from")
    if not (isinstance(from_val, str) or _is_int(from_val)):
        raise ProtocolError(
            "malformed", f"move.from must be int or 'bar' string, got {type(from_val).__name__}"
        )
    die = p.get("die")
    if not _is_int(die):
        raise ProtocolError("malformed", f"move.die must be int, got {type(die).__name__}")
    return MoveMessage(state_seq=state_seq, cid=cid, from_=from_val, die=die)


def _parse_undo(p: dict) -> UndoMessage:
    state_seq = _require_int(p, "state_seq", "undo")
    cid = _require_str(p, "cid", "undo")
    return UndoMessage(state_seq=state_seq, cid=cid)


def _parse_end_turn(p: dict) -> EndTurnMessage:
    state_seq = _require_int(p, "state_seq", "end_turn")
    cid = _require_str(p, "cid", "end_turn")
    return EndTurnMessage(state_seq=state_seq, cid=cid)


def _parse_rematch(p: dict) -> RematchMessage:
    cid = _require_str(p, "cid", "rematch")
    want = p.get("want")
    if not isinstance(want, bool):
        raise ProtocolError("malformed", f"rematch.want must be bool, got {type(want).__name__}")
    state_seq = _optional_int(p, "state_seq", "rematch")
    return RematchMessage(cid=cid, want=want, state_seq=state_seq)


def _parse_replay_request(p: dict) -> ReplayRequestMessage:
    cid = _require_str(p, "cid", "replay_request")
    state_seq = _optional_int(p, "state_seq", "replay_request")
    return ReplayRequestMessage(cid=cid, state_seq=state_seq)


_CLIENT_PARSERS: Final[dict[str, Any]] = {
    "hello": _parse_hello,
    "roll": _parse_roll,
    "move": _parse_move,
    "undo": _parse_undo,
    "end_turn": _parse_end_turn,
    "rematch": _parse_rematch,
    "replay_request": _parse_replay_request,
}


def parse_client_message(payload: Any) -> ClientMessage:
    """Parse + validate a decoded-JSON payload into a typed client message.

    Implements §4.5.3 step 1 (frame shape) for the message layer: not a
    dict / missing `t` / unknown `t` / wrong field types on the fields this
    module owns. Extra/unknown keys are ignored, never merged (§4.5.1 --
    "A `move` frame carrying extra keys is ignored, not merged").

    Value-range / literal validation that also depends on engine state
    (die 1..6, `from` "bar" -> BAR_POINT[player] mapping and rejecting
    numeric 0/25) is intentionally NOT duplicated here -- see
    wire.move_from_wire, whose Malformed maps to this same "malformed" code
    at rejection-order step 8. Both paths land on the same wire code.

    Raises ProtocolError(code="malformed", ...) on any shape failure.
    """
    if not isinstance(payload, dict):
        raise ProtocolError(
            "malformed", f"frame must be a JSON object, got {type(payload).__name__}"
        )

    t = payload.get("t")
    if not isinstance(t, str) or t not in CLIENT_MESSAGE_TYPES:
        raise ProtocolError("malformed", f"unknown or missing message type: {t!r}")

    return _CLIENT_PARSERS[t](payload)


def check_hello_protocol(msg: HelloMessage) -> None:
    """§4.5.3 step 3 / §4.6.1 step 1: `protocol != 1` -> protocol_mismatch,
    close 4001.

    Only `hello` carries a `protocol` field (§4.2.2) -- every other client
    message type has none. Skipping this step for non-hello frames is 5B's
    call to make (there is nothing on those frames to check), not this
    function's.

    Raises ProtocolError(code="protocol_mismatch", ...) on mismatch.
    """
    if msg.protocol != 1:
        raise ProtocolError("protocol_mismatch", f"protocol must be 1, got {msg.protocol}")


# === Server -> client message types (§4.2.3, 5 types) ===
#
# These are envelope constructors: every field is already computed by the
# caller (5B derives active_seat/awaiting_input/opening/legal_moves/etc.;
# wire.py serializes board/dice/moves). This module only assembles the
# envelope and enforces the handful of shape invariants stated directly in
# §4.2.3 -- it does not compute game data.

def construct_seat_assigned(
    *, seat: str, player: int, resume_token: str, session_id: str, resumed: bool
) -> dict:
    """§4.2.3 `seat_assigned` -- exactly once, immediately after a
    successful `hello`, before the first `state`."""
    if seat not in ("white", "black"):
        raise ValueError(f"seat must be 'white' or 'black', got {seat!r}")
    return {
        "t": "seat_assigned",
        "protocol": 1,
        "seat": seat,
        "player": player,
        "resume_token": resume_token,
        "session_id": session_id,
        "resumed": resumed,
    }


def construct_blocked(*, reason: str, seats_occupied: int, session_started_at: str) -> dict:
    """§4.2.3 `blocked` -- to visitor #3, then close 4003. No `retry_hint_s`
    (Rev 2 deleted it -- there is no automatic retry from `blocked`)."""
    return {
        "t": "blocked",
        "reason": reason,
        "seats_occupied": seats_occupied,
        "session_started_at": session_started_at,
    }


def construct_state(
    *,
    session_id: str,
    game_id: str,
    state_seq: int,
    phase: str,
    active_seat: str | None,
    awaiting_input: bool,
    turn_number: int,
    board: dict,
    dice: dict,
    opening: dict | None,
    legal_moves: list,
    can_undo: bool,
    can_end_turn: bool,
    players: dict,
    series: dict,
    rematch: dict,
    outcome: dict | None,
    last_event: dict | None,
    server_time: str,
) -> dict:
    """§4.2.3 `state` -- the authoritative snapshot.

    `board` is NEVER null, never omitted, never an empty points array, in
    ANY phase (§4.2.3 Rev 5/WO2) -- when `session.game is None` the caller
    must supply `wire.board_to_wire(Board(points=STARTING_POINTS))`. This
    function only refuses `None`; it does not know how to build the
    fallback board itself (that's wire.py's job).
    """
    if board is None:
        raise ValueError("state.board must never be None (§4.2.3 Rev 5/WO2)")
    return {
        "t": "state",
        "protocol": 1,
        "session_id": session_id,
        "game_id": game_id,
        "state_seq": state_seq,
        "phase": phase,
        "active_seat": active_seat,
        "awaiting_input": awaiting_input,
        "turn_number": turn_number,
        "board": board,
        "dice": dice,
        "opening": opening,
        "legal_moves": legal_moves,
        "can_undo": can_undo,
        "can_end_turn": can_end_turn,
        "players": players,
        "series": series,
        "rematch": rematch,
        "outcome": outcome,
        "last_event": last_event,
        "server_time": server_time,
    }


def construct_replay(*, game_id: str, frames: list) -> dict:
    """§4.2.3 `replay` -- once, in response to `replay_request`, only in
    `game_over`. `frames` are already wire-serialized positions
    (wire.frame_to_wire) -- the client never applies them (§4.5 authority
    rule)."""
    return {"t": "replay", "game_id": game_id, "frames": frames}


SERVER_MESSAGE_TYPES: Final[frozenset[str]] = frozenset({
    "seat_assigned", "blocked", "state", "error", "replay",
})


__all__ = [
    "ERROR_CODES",
    "CloseCode",
    "ProtocolError",
    "extract_cid",
    "construct_error",
    "map_engine_error",
    "REJECTION_ORDER",
    "HelloMessage",
    "RollMessage",
    "MoveMessage",
    "UndoMessage",
    "EndTurnMessage",
    "RematchMessage",
    "ReplayRequestMessage",
    "ClientMessage",
    "CLIENT_MESSAGE_TYPES",
    "parse_client_message",
    "check_hello_protocol",
    "construct_seat_assigned",
    "construct_blocked",
    "construct_state",
    "construct_replay",
    "SERVER_MESSAGE_TYPES",
]
