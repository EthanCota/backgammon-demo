"""backgammon/wire.py -- Serialization and deserialization (wire protocol).

This module handles conversion between internal types and wire format (dicts).
Does NOT import json; that lives in transport/server.py only.

Frozen contract: §4.2 (wire shapes), §4.3.5 (function signatures), §4.3.6 (errors).
Three critical traps recorded in the spec:
  1. `from` is int | "bar", NOT 0/25. "bar" wins. BAR_POINT translation only here.
  2. `opening_die` event name (not `opening_die_recorded`), everywhere.
  3. `game_from_wire(game_to_wire(s)) == s` is IMPOSSIBLE; use projection assertion
     over WIRE_ROUNDTRIP_FIELDS instead.
"""

from __future__ import annotations

from typing import Any, Final, NamedTuple

from .types import (
    BAR_POINT,
    STARTING_POINTS,
    Board,
    DiceExhausted,
    EnginePhase,
    GameResult,
    GameState,
    Move,
    Outcome,
    Player,
)
from .rules import (
    EngineError,
    IllegalMove,
    NotAPrefix,
    NothingToUndo,
    OpeningUnresolved,
    SchemaMismatch,
    TurnNotComplete,
    WrongPhase,
    Malformed,
)


SCHEMA: Final[int] = 1


# === Error hierarchy (§4.3.6) ===
# NOTE: these are NOT redeclared here. They are the single canonical classes
# from backgammon.rules (and DiceExhausted from backgammon.types) -- re-exported
# so `from backgammon.wire import Malformed` and `from backgammon import Malformed`
# are the SAME class. Two independently-declared hierarchies with identical
# names is the exact defect this fixed: pytest.raises(Malformed) using the
# top-level import silently failed to catch wire.py's own separate Malformed.


# === Wire types ===

class PartialGameState(NamedTuple):
    """Display-only game state returned by game_from_wire.

    Explicitly partial: no DiceSource, no dice_drawn, no moves_this_turn,
    no turn_start_board. `opening_rolls` is populated (from the wire's
    `opening.rolls`) because it is a real GameState field and part of
    WIRE_ROUNDTRIP_FIELDS. For testing and replay rendering only;
    NOT an engine input (cannot apply_move to it).
    """
    board: Board
    turn: int  # 1 | -1 (Player.WHITE or Player.BLACK as int)
    phase: str  # EnginePhase as string
    turn_number: int
    dice_rolled: tuple[int, ...]
    dice_remaining: tuple[int, ...]
    opening_rolls: tuple[tuple[int, int], ...]
    result: dict[str, Any] | None


# === Serialization: internal -> wire ===

def board_to_wire(board: Board) -> dict:
    """Convert Board to wire format.

    Returns a dict with:
      "points": [None, int, int, ..., int]  (length 25)
      "bar": {"white": int, "black": int}
      "off": {"white": int, "black": int}
    """
    return {
        "points": list(board.points),
        "bar": {
            "white": board.bar_white,
            "black": board.bar_black,
        },
        "off": {
            "white": board.off_white,
            "black": board.off_black,
        },
    }


def game_to_wire(state: GameState) -> dict:
    """Serialize GameState to wire format (game half only, not session half).

    Returns dict with keys:
      phase, turn, turn_number, board, dice, opening, legal_moves,
      can_undo, can_end_turn, outcome

    Notes:
      - Emits `turn: 1 | -1` (Player value), NOT a seat name
      - `board` is always the current board
      - `opening` is the live opening object (only in opening_roll) or
        a display-only report (post-game_started)
    """
    # Convert dice to wire format
    dice_dict = {
        "rolled": list(state.dice_rolled),
        "remaining": list(state.dice_remaining),
        "is_double": len(state.dice_rolled) == 4 if state.dice_rolled else False,
    }

    # Convert result to wire format if present
    result_dict = None
    if state.result is not None:
        result_dict = {
            "winner": "white" if state.result.winner is Player.WHITE else "black",
            "type": state.result.outcome.value,
        }

    # Build the core dict
    return {
        "phase": state.phase.value,
        "turn": state.turn.value,  # 1 for WHITE, -1 for BLACK
        "turn_number": state.turn_number,
        "board": board_to_wire(state.board),
        "dice": dice_dict,
        "opening": None,  # Placeholder; opening is set by transport
        "legal_moves": [],  # Placeholder; legal_moves is set by transport
        "can_undo": len(state.moves_this_turn) > 0,
        "can_end_turn": state.phase is EnginePhase.AWAITING_COMMIT,
        "outcome": result_dict,
    }


def game_from_wire(payload: dict) -> PartialGameState:
    """Deserialize a wire dict to PartialGameState (display-only, not engine input).

    Raises SchemaMismatch for schema errors.
    This is a test/replay-rendering helper only; you cannot apply_move to it.
    """
    # Validate schema
    required_keys = {
        "phase", "turn", "turn_number", "board",
        "dice", "opening", "outcome"
    }
    if not required_keys.issubset(payload.keys()):
        missing = required_keys - payload.keys()
        raise SchemaMismatch(f"Missing required keys: {missing}")

    # Validate and convert phase
    phase_str = payload.get("phase")
    if phase_str not in {e.value for e in EnginePhase}:
        raise SchemaMismatch(f"Unknown phase: {phase_str}")

    # Validate and convert turn
    turn_val = payload.get("turn")
    if turn_val not in (1, -1):
        raise SchemaMismatch(f"turn must be 1 or -1, got {turn_val}")
    turn_player = Player.WHITE if turn_val == 1 else Player.BLACK

    # Validate turn_number
    turn_number = payload.get("turn_number")
    if not isinstance(turn_number, int) or turn_number < 0:
        raise SchemaMismatch(f"turn_number must be non-negative int, got {turn_number}")

    # Deserialize board
    board = _board_from_wire(payload.get("board"))

    # Deserialize dice
    dice_payload = payload.get("dice", {})
    dice_rolled = tuple(dice_payload.get("rolled", []))
    dice_remaining = tuple(dice_payload.get("remaining", []))

    # Validate dice values
    for d in dice_rolled:
        if not isinstance(d, int) or not (1 <= d <= 6):
            raise SchemaMismatch(f"Invalid die value: {d}")
    for d in dice_remaining:
        if not isinstance(d, int) or not (1 <= d <= 6):
            raise SchemaMismatch(f"Invalid die value: {d}")

    # Deserialize opening_rolls from the wire's opening.rolls (may be absent
    # or None in non-opening phases -- opening_rolls must be () not None,
    # so it compares equal to a fresh GameState's ()).
    opening_payload = payload.get("opening")
    if opening_payload is None:
        opening_rolls: tuple[tuple[int, int], ...] = ()
    else:
        opening_rolls = tuple(
            tuple(pair) for pair in opening_payload.get("rolls", [])
        )

    # Deserialize outcome (may be None)
    outcome = payload.get("outcome")

    return PartialGameState(
        board=board,
        turn=turn_val,
        phase=phase_str,
        turn_number=turn_number,
        dice_rolled=dice_rolled,
        dice_remaining=dice_remaining,
        opening_rolls=opening_rolls,
        result=outcome,
    )


def _board_from_wire(board_payload: Any) -> Board:
    """Helper to deserialize board from wire format.

    Raises SchemaMismatch on schema errors.
    """
    if not isinstance(board_payload, dict):
        raise SchemaMismatch(f"board must be dict, got {type(board_payload)}")

    points_list = board_payload.get("points")
    if not isinstance(points_list, list) or len(points_list) != 25:
        raise SchemaMismatch(f"points must be list of length 25, got {len(points_list) if isinstance(points_list, list) else type(points_list)}")

    if points_list[0] is not None:
        raise SchemaMismatch("points[0] must be None")

    # Validate points[1..24] are ints or None
    points_tuple = []
    for i, p in enumerate(points_list):
        if i == 0:
            points_tuple.append(None)
        else:
            if not isinstance(p, int):
                raise SchemaMismatch(f"points[{i}] must be int, got {type(p)}")
            points_tuple.append(p)

    bar_payload = board_payload.get("bar", {})
    bar_white = bar_payload.get("white", 0)
    bar_black = bar_payload.get("black", 0)

    if not isinstance(bar_white, int) or not isinstance(bar_black, int):
        raise SchemaMismatch("bar values must be ints")

    off_payload = board_payload.get("off", {})
    off_white = off_payload.get("white", 0)
    off_black = off_payload.get("black", 0)

    if not isinstance(off_white, int) or not isinstance(off_black, int):
        raise SchemaMismatch("off values must be ints")

    return Board(
        points=tuple(points_tuple),
        bar_white=bar_white,
        bar_black=bar_black,
        off_white=off_white,
        off_black=off_black,
    )


def move_from_wire(payload: dict, state: GameState) -> Move:
    """Deserialize a move from wire format.

    Payload: {"from": int | "bar", "die": int}

    Maps "bar" -> BAR_POINT[state.turn] (the ONE place this translation lives).
    Rejects numeric 0 or 25 as Malformed.

    Raises Malformed on shape/range errors.
    """
    if not isinstance(payload, dict):
        raise Malformed(f"move must be dict, got {type(payload)}")

    from_val = payload.get("from")
    die_val = payload.get("die")

    # Validate die
    if not isinstance(die_val, int) or not (1 <= die_val <= 6):
        raise Malformed(f"die must be int 1..6, got {die_val}")

    # Validate and convert from
    if isinstance(from_val, str):
        if from_val != "bar":
            raise Malformed(f"from string must be 'bar', got {from_val}")
        src = BAR_POINT[state.turn]
    elif isinstance(from_val, int):
        # Reject 0 and 25 explicitly; only 1..24 are valid
        if from_val == 0 or from_val == 25:
            raise Malformed(f"from must be 'bar' for bar points, not numeric {from_val}")
        if not (1 <= from_val <= 24):
            raise Malformed(f"from must be 1..24 or 'bar', got {from_val}")
        src = from_val
    else:
        raise Malformed(f"from must be int or 'bar', got {type(from_val)}")

    return Move(src=src, die=die_val)


def frame_to_wire(state: GameState, seq: int, kind: str, seat: str | None,
                  move: Move | None) -> dict:
    """Serialize a replay frame for the wire.

    Used in replay/regression artifact format (§4.2.3).

    Returns a frame dict for inclusion in a replay message.
    """
    frame_dict: dict[str, Any] = {
        "seq": seq,
        "turn_number": state.turn_number if kind != "start" else 0,
        "seat": seat,
        "kind": kind,
        "board": board_to_wire(state.board) if kind != "start" or state is not None else board_to_wire(Board(points=STARTING_POINTS)),
        "dice": None,
        "move": None,
    }

    # Add dice info if the state has rolled
    if state.dice_rolled:
        frame_dict["dice"] = {
            "rolled": list(state.dice_rolled),
            "remaining": list(state.dice_remaining),
            "is_double": len(state.dice_rolled) == 4,
        }

    # Add move info if present
    if move is not None:
        # Encode move.src as int or "bar"
        from_val: int | str
        if move.src == BAR_POINT[Player.WHITE]:
            from_val = "bar"
        elif move.src == BAR_POINT[Player.BLACK]:
            from_val = "bar"
        else:
            from_val = move.src

        # Compute destination
        dest = move.src + state.turn.direction * move.die

        # Normalize to board representation
        to_val = dest
        if dest <= 0 or dest >= 25:
            to_val = "off"

        frame_dict["move"] = {
            "from": from_val,
            "to": to_val,
            "die": move.die,
            "hit": False,  # Placeholder; actual hit info comes from board analysis
        }

    return frame_dict


# === Roundtrip fields ===

WIRE_ROUNDTRIP_FIELDS: Final[frozenset[str]] = frozenset({
    "board",
    "turn",
    "phase",
    "turn_number",
    "dice_rolled",
    "dice_remaining",
    "opening_rolls",
    "result",
})


__all__ = [
    "SCHEMA",
    "EngineError",
    "IllegalMove",
    "NotAPrefix",
    "TurnNotComplete",
    "WrongPhase",
    "NothingToUndo",
    "SchemaMismatch",
    "Malformed",
    "DiceExhausted",
    "OpeningUnresolved",
    "PartialGameState",
    "board_to_wire",
    "game_to_wire",
    "game_from_wire",
    "move_from_wire",
    "frame_to_wire",
    "WIRE_ROUNDTRIP_FIELDS",
]
