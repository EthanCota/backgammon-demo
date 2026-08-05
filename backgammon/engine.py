"""backgammon/engine.py -- the §4.3 lifecycle surface (wave 3).

    new_game_from_opening / new_game   (§4.3.1)
    roll_dice / end_turn / undo_last_move
    GameRecord / replay                (§4.3.7)

Move generation, single-move application and outcome typing live in
backgammon/rules.py (§3.2); this module imports them and never the reverse.

The import rule (§3.3): part of the ENGINE. No asyncio, no time, no
websockets, no json, no randomness, no I/O. Every function here is pure:
same inputs -> same outputs, and no GameState is ever mutated.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import (
    EngineError,
    IllegalMove,
    Malformed,
    NotAPrefix,
    NothingToUndo,
    OpeningUnresolved,
    SchemaMismatch,
    TurnNotComplete,
    WrongPhase,
    apply_move,
    destination,
    is_turn_complete,
    legal_moves,
    legal_turns,
    must_forfeit,
)
from .types import (
    OPENING_THROWS_MAX,
    STARTING_POINTS,
    Board,
    DiceSource,
    EnginePhase,
    GameState,
    Move,
    Player,
)


# --- Opening / new game (§4.3.1) --------------------------------------------

def _validated_prefix(
    dice: DiceSource, submitted: tuple[tuple[int, int], ...]
) -> list[tuple[int, int]]:
    """Steps 1-3 of `new_game_from_opening`: shape, provenance, terminality.

    Returns the prefix as a list of (white_die, black_die) pairs. Raises
    Malformed on any violation -- loudly, never silently corrected.
    """
    if not isinstance(submitted, (tuple, list)):
        raise Malformed(f"opening prefix must be a sequence, got {type(submitted).__name__}")

    rolls: list[tuple[int, int]] = []

    # 1. SHAPE -- every entry is a 2-tuple of ints in 1..6.
    for k, entry in enumerate(submitted):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise Malformed(f"opening throw {k} is not a 2-tuple: {entry!r}")
        white, black = entry
        for value in (white, black):
            if isinstance(value, bool) or not isinstance(value, int):
                raise Malformed(f"opening throw {k} has a non-int die: {entry!r}")
            if not (1 <= value <= 6):
                raise Malformed(f"opening throw {k} has a die outside 1..6: {entry!r}")
        rolls.append((white, black))

    # 2. PROVENANCE -- re-derive every value from the source rather than
    #    trusting the transport. The die is a function of (source, throw, seat)
    #    alone, so a value can never be injected from outside and a transport
    #    bug cannot desync the dice cursor silently.
    for k, (white, black) in enumerate(rolls):
        expected = (dice.die(2 * k), dice.die(2 * k + 1))
        if (white, black) != expected:
            raise Malformed(
                f"opening throw {k} is {(white, black)!r}, but the dice source "
                f"says {expected!r}"
            )

    # 3. TERMINALITY -- a decisive throw ends the opening, so nothing may
    #    follow one. Every entry except the last must be a tie.
    for k, (white, black) in enumerate(rolls[:-1]):
        if white != black:
            raise Malformed(
                f"opening throw {k} is decisive ({white},{black}) but is followed "
                f"by {len(rolls) - k - 1} more throw(s)"
            )

    return rolls


def new_game_from_opening(
    dice: DiceSource,
    submitted: tuple[tuple[int, int], ...] = (),
) -> GameState:
    """Build the started GameState from a (possibly empty, possibly all-tie)
    prefix of ALREADY-THROWN opening throws (§4.3.1).

    `submitted[k] == (white_die, black_die)` is throw k, thrown interactively,
    drawn by the server on a timeout, or both. It may be empty, may end in a
    tie, and may be all ties.

    Contract, in order: validate shape, validate provenance against the dice
    source, validate terminality, resolve any remaining ties SERVER-SIDE, then
    build. Raises Malformed on 1-3 and OpeningUnresolved at OPENING_THROWS_MAX.

    INVARIANT: the returned `dice_rolled` is NEVER doubles -- the resolve loop
    exits only on a decisive throw.

    PURE: reads only `dice.die(n)`. It knows nothing about seats, sockets,
    deadlines, or who clicked.
    """
    rolls = _validated_prefix(dice, submitted)

    # 4. RESOLVE. BOUNDED BY CONSTRUCTION, not by probability (§4.6.0 (D)):
    #    a loop that terminates "almost surely" is a deadlock in a costume.
    while not rolls or rolls[-1][0] == rolls[-1][1]:
        k = len(rolls)
        if k >= OPENING_THROWS_MAX:
            raise OpeningUnresolved(k)
        rolls.append((dice.die(2 * k), dice.die(2 * k + 1)))

    # 5. BUILD.
    white_die, black_die = rolls[-1]
    board = Board(points=STARTING_POINTS)
    return GameState(
        board=board,
        turn=Player.WHITE if white_die > black_die else Player.BLACK,
        phase=EnginePhase.AWAITING_MOVE,
        turn_number=1,
        dice_rolled=(white_die, black_die),  # AS THROWN, never sorted
        dice_remaining=(white_die, black_die),
        moves_this_turn=(),
        turn_start_board=board,
        dice=dice,
        dice_drawn=2 * len(rolls),
        opening_rolls=tuple(rolls),
        result=None,
    )


def new_game(dice: DiceSource) -> GameState:
    """EXACTLY `new_game_from_opening(dice, ())` -- the degenerate call, not a
    second implementation (§4.3.1). This is why "the server resolves the
    opening itself" is structurally the same code path as the interactive one,
    and why the Rev 5 rematch path cannot rot separately from it.
    """
    return new_game_from_opening(dice, ())


# --- Dice / turn boundary (§4.3.1) ------------------------------------------

def roll_dice(state: GameState) -> GameState:
    """Draw EXACTLY 2 dice from `state.dice` at cursor `state.dice_drawn`.

    Doubles expand to (d,)*4 in dice_rolled/dice_remaining -- still 2 DRAWS
    (§4.3.4). "Doubles draw two more" is the obvious wrong implementation and
    it silently desyncs replay.

    Raises WrongPhase unless `state.phase` is AWAITING_ROLL.

    PHASE RETURNED -- an interpretation, stated rather than buried. §4.3.1
    reads "Returns AWAITING_MOVE, or AWAITING_ROLL-for-the-opponent when the
    drawn dice admit no legal move (the transport auto-advances -- §4.6)".
    This function returns AWAITING_MOVE in BOTH cases, because §4.6's own
    table evaluates `must_forfeit()` on the state this call returns and then
    auto-advances "within the SAME transition as that `roll`" -- and §4.3.3
    gates `must_forfeit` on `phase in {AWAITING_MOVE, AWAITING_COMMIT}` and
    `dice_rolled != ()`. Advancing the turn here instead would make
    `must_forfeit` unobservable by its only caller and would leave the
    transport unable to emit `last_event.kind = "turn_forfeited"`. The
    parenthetical in §4.3.1 describes the composite transport transition; the
    engine half of it is this function plus `end_turn`.
    """
    if state.phase is not EnginePhase.AWAITING_ROLL:
        raise WrongPhase(f"roll_dice requires AWAITING_ROLL, got {state.phase}")

    first = state.dice.die(state.dice_drawn)
    second = state.dice.die(state.dice_drawn + 1)
    rolled = (first,) * 4 if first == second else (first, second)

    return GameState(
        board=state.board,
        turn=state.turn,
        phase=EnginePhase.AWAITING_MOVE,
        turn_number=state.turn_number,
        dice_rolled=rolled,
        dice_remaining=rolled,
        moves_this_turn=(),
        turn_start_board=state.board,
        dice=state.dice,
        dice_drawn=state.dice_drawn + 2,  # EXACTLY 2, doubles or not
        opening_rolls=state.opening_rolls,
        result=None,
    )


def end_turn(state: GameState) -> GameState:
    """Pass the turn. DRAWS NOTHING (§4.3.1, a corrected BLOCKING defect).

    Only `roll_dice` draws. This is what makes the mutual-closeout case
    terminate: each forfeit still costs one client `roll`, so the server never
    advances the game on its own timer and cannot spin.

    Raises TurnNotComplete if `is_turn_complete(state)` is False.
    """
    if not is_turn_complete(state):
        raise TurnNotComplete("the current turn still has a playable die")

    return GameState(
        board=state.board,
        turn=state.turn.opponent,
        phase=EnginePhase.AWAITING_ROLL,
        turn_number=state.turn_number + 1,
        dice_rolled=(),
        dice_remaining=(),
        moves_this_turn=(),
        turn_start_board=state.board,
        dice=state.dice,
        dice_drawn=state.dice_drawn,
        opening_rolls=state.opening_rolls,
        result=state.result,
    )


def undo_last_move(state: GameState) -> GameState:
    """Undo the last move of the CURRENT turn. Never crosses a turn boundary.

    Implemented as REPLAY of `moves_this_turn[:-1]` onto `turn_start_board`,
    never as inversion: inverting a hit requires remembering what was hit, and
    replay remembers nothing (§4.3.1).

    Raises NothingToUndo when `moves_this_turn` is empty.
    """
    played = tuple(state.moves_this_turn)
    if not played:
        raise NothingToUndo("no move has been played this turn")

    rewound = GameState(
        board=state.turn_start_board,
        turn=state.turn,
        phase=EnginePhase.AWAITING_MOVE,
        turn_number=state.turn_number,
        dice_rolled=state.dice_rolled,
        dice_remaining=state.dice_rolled,
        moves_this_turn=(),
        turn_start_board=state.turn_start_board,
        dice=state.dice,
        dice_drawn=state.dice_drawn,
        opening_rolls=state.opening_rolls,
        result=None,
    )
    for move in played[:-1]:
        rewound = apply_move(rewound, move).state
    return rewound


# --- Replay / regression artifact (§4.3.7) ----------------------------------

@dataclass(frozen=True, slots=True)
class GameRecord:
    """The TEST/REGRESSION artifact: a bug found in play is filed as a record
    and becomes a regression test with no extra machinery. Deliberately NOT the
    wire replay format (CR-12)."""

    dice: DiceSource
    moves: tuple[Move, ...]  # every move in order, across all turns


def replay(record: GameRecord) -> GameState:
    """`new_game`, then apply each move (calling `end_turn` at each boundary),
    ASSERTING every move was in `legal_moves` at the time (§4.3.7).

    Turn boundaries are crossed by the engine's own rules rather than by any
    marker in the record: a turn ends when it is complete, the next turn's dice
    come from `roll_dice` (never from `end_turn`), and a turn nobody can play
    is forfeited -- still drawing its 2 dice (§4.3.4).
    """
    state = new_game(record.dice)
    for move in record.moves:
        state = _advance_to_movable(state)
        if move not in legal_moves(state):
            raise AssertionError(
                f"replayed move {move!r} was not legal at turn "
                f"{state.turn_number} for {state.turn.name}"
            )
        state = apply_move(state, move).state
    return state


def _advance_to_movable(state: GameState) -> GameState:
    """Walk a replay forward over turn boundaries, dice draws and forfeits
    until the side to move actually has a move. Pure; no client input."""
    current = state
    while current.phase is not EnginePhase.GAME_OVER:
        if current.phase is EnginePhase.AWAITING_COMMIT:
            current = end_turn(current)
            continue
        if current.phase is EnginePhase.AWAITING_ROLL:
            current = roll_dice(current)
            continue
        # AWAITING_MOVE
        if must_forfeit(current):
            current = end_turn(current)
            continue
        return current
    return current


__all__ = [
    "EngineError",
    "GameRecord",
    "IllegalMove",
    "NotAPrefix",
    "SchemaMismatch",
    "destination",
    "end_turn",
    "legal_turns",
    "new_game",
    "new_game_from_opening",
    "replay",
    "roll_dice",
    "undo_last_move",
]
