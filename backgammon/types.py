"""backgammon/types.py -- canonical game state types, enums, constants.

This module contains ONLY types, enums, and constants from the frozen spec (§4.1).
NO behavior or logic.

The import rule (§3.3): this module may import ONLY from
{dataclasses, enum, typing, __future__}.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final, Protocol


class Player(IntEnum):
    """Game players: sign is the board representation, direction is travel direction."""
    WHITE = 1
    BLACK = -1
    # `value` IS the sign in Board.points. It is NOT the direction of travel --
    # direction is -value (WHITE: value +1, direction -1).

    @property
    def opponent(self) -> Player:
        return Player(-self.value)

    @property
    def direction(self) -> int:
        return -1 if self is Player.WHITE else 1


# Board geometry constants (§4.1.1)
NUM_POINTS: Final[int] = 24
CHECKERS_PER_PLAYER: Final[int] = 15
BAR_POINT: Final[dict[Player, int]] = {Player.WHITE: 25, Player.BLACK: 0}
OFF_EDGE: Final[dict[Player, int]] = {Player.WHITE: 0, Player.BLACK: 25}
HOME: Final[dict[Player, range]] = {
    Player.WHITE: range(1, 7),
    Player.BLACK: range(19, 25),
}


@dataclass(frozen=True, slots=True)
class Board:
    """The board state: signed integers for checker ownership and count.

    points[p] is a signed integer: +n = n WHITE checkers, -n = n BLACK, 0 = empty.
    Length must be 25; points[0] is always None.
    """
    points: tuple[int | None, ...]  # LENGTH 25. points[0] is ALWAYS None.
    bar_white: int = 0
    bar_black: int = 0
    off_white: int = 0
    off_black: int = 0

    def bar(self, p: Player) -> int:
        """Checkers on bar for player p."""
        return self.bar_white if p is Player.WHITE else self.bar_black

    def off(self, p: Player) -> int:
        """Checkers borne off for player p."""
        return self.off_white if p is Player.WHITE else self.off_black

    def mine(self, p: Player, point: int) -> int:
        """Own checkers on `point`, else 0.

        A non-negative COUNT (§4.1.2: "own checkers on `point`, else 0"), never
        the signed cell value: `points[p]` is signed, but every caller of this
        accessor -- `is_blocked` (theirs >= 2), `is_blot` (theirs == 1),
        `may_bear_off`, `classify`, and move generation -- compares against a
        magnitude. Returning the raw signed value here inverts all of them for
        BLACK (and, via `theirs`, blocking/hitting for WHITE).
        """
        if not (1 <= point <= 24):
            return 0
        val = self.points[point]
        if val is None:
            return 0
        return abs(val) if (val > 0) == (p is Player.WHITE) else 0

    def theirs(self, p: Player, point: int) -> int:
        """Opponent's checkers on `point`, else 0. Non-negative count -- see `mine`."""
        if not (1 <= point <= 24):
            return 0
        val = self.points[point]
        if val is None:
            return 0
        opp = p.opponent
        return abs(val) if (val > 0) == (opp is Player.WHITE) else 0

    def is_blocked(self, p: Player, point: int) -> bool:
        """True if opponent has 2+ checkers on `point`."""
        return self.theirs(p, point) >= 2

    def is_blot(self, p: Player, point: int) -> bool:
        """True if opponent has exactly 1 checker on `point`."""
        return self.theirs(p, point) == 1


# Starting position (§4.1.2): invariant-checked at module load
STARTING_POINTS: Final[tuple[int | None, ...]] = (
    None,  # index 0, reserved
    -2,  0,  0,  0,  0,  5,  0,  3,  0,  0,  0, -5,  # points  1..12
    5,  0,  0,  0, -3,  0, -5,  0,  0,  0,  0,  2,  # points 13..24
)

# Validate STARTING_POINTS invariants at import time (§4.1.2)
assert len(STARTING_POINTS) == 25, "STARTING_POINTS must have 25 entries"
assert STARTING_POINTS[0] is None, "STARTING_POINTS[0] must be None"

# Check 15 checkers per side
white_sum = sum(p for p in STARTING_POINTS[1:] if p is not None and p > 0)
black_sum = sum(-p for p in STARTING_POINTS[1:] if p is not None and p < 0)
assert white_sum == 15, f"WHITE must have 15 checkers, got {white_sum}"
assert black_sum == 15, f"BLACK must have 15 checkers, got {black_sum}"

# Check pip count (167 per side)
white_pip = sum(p * i for i, p in enumerate(STARTING_POINTS[1:], 1) if p and p > 0)
black_pip = sum(-p * (25 - i) for i, p in enumerate(STARTING_POINTS[1:], 1) if p and p < 0)
assert white_pip == 167, f"WHITE pip count must be 167, got {white_pip}"
assert black_pip == 167, f"BLACK pip count must be 167, got {black_pip}"

# Check mirror symmetry: points[p] == -points[25-p]
for p in range(1, 25):
    assert STARTING_POINTS[p] == -STARTING_POINTS[25 - p], \
        f"STARTING_POINTS[{p}] != -STARTING_POINTS[{25-p}]"


@dataclass(frozen=True, slots=True)
class Move:
    """A single move: source point and die value.

    dest is DERIVED, never stored: dest = src + player.direction * die
    """
    src: int  # 1..24, or BAR_POINT[player] (25 for WHITE, 0 for BLACK)
    die: int  # 1..6


Turn = tuple[Move, ...]  # ordered; a complete maximal sequence


@dataclass(frozen=True, slots=True)
class MoveResult:
    """Result of applying a move."""
    state: GameState  # new state after move
    dest: int  # raw arithmetic; <=0 or >=25 means borne off
    hit: bool  # opponent checker was captured
    bore_off: bool  # checker left the board


class EnginePhase(StrEnum):
    """Engine game phases (§4.1.3). Only engine states; never lobby/opening/abandoned."""
    AWAITING_ROLL = "awaiting_roll"  # dice_rolled == (); only `roll` advances it
    AWAITING_MOVE = "awaiting_move"  # legal_moves is non-empty
    AWAITING_COMMIT = "awaiting_commit"  # >=1 die played, no continuation; commit or undo
    GAME_OVER = "game_over"


class SessionPhase(StrEnum):
    """Session phases (§4.1.3, §4.1.5). Transport-owned, NOT in GameState.

    Note: SessionPhase is defined here (not in transport/session.py) because
    the opening-roll constants depend on it being available at types import time.
    """
    LOBBY = "lobby"  # < 2 seats live; no GameState exists yet
    OPENING_ROLL = "opening_roll"  # 2 seats live, each owes ONE opening die; no GameState yet
    ABANDONED = "abandoned"  # grace expired; no GameState exists yet


# Opening-roll constants (§4.1.3, Rev 3/5)
SEAT_DIE_INDEX: Final[dict[Player, int]] = {Player.WHITE: 0, Player.BLACK: 1}

# Rev 5: Baxter 06:30 "meet in the middle" (was 90.0 in Rev 3/4)
OPENING_ROLL_DEADLINE_S: Final[float] = 60.0

OPENING_INTERACTIVE_THROWS_MAX: Final[int] = 3
OPENING_THROWS_MAX: Final[int] = 64


class Outcome(StrEnum):
    """Game outcome types."""
    SINGLE = "single"
    GAMMON = "gammon"
    BACKGAMMON = "backgammon"


@dataclass(frozen=True, slots=True)
class GameResult:
    """Terminal game result."""
    winner: Player
    outcome: Outcome
    loser_off: int  # 0 for gammon/backgammon
    turns: int


@dataclass(frozen=True, slots=True)
class GameState:
    """The canonical game state. Immutable. Engine-owned.

    Every field is immutable. Every engine function returns a new GameState.
    GameState contains no session, seat, connection, sequence, presence,
    series, or timing data -- those live in transport.SessionState.
    """

    # --- position ---
    board: Board
    turn: Player  # whose turn it is. ALWAYS set.
    phase: EnginePhase  # engine states only; never lobby/abandoned
    turn_number: int  # 1 on the first real turn

    # --- this turn ---
    dice_rolled: tuple[int, ...]  # () | (d1,d2) | (d,d,d,d) on doubles
    dice_remaining: tuple[int, ...]  # not yet consumed
    moves_this_turn: Turn  # ordered, applied so far
    turn_start_board: Board  # undo anchor; undo is REPLAY, never inversion

    # --- dice authority (§4.4) ---
    dice: DiceSource  # a frozen VALUE, not a generator
    dice_drawn: int  # cursor into `dice`; makes GameState self-describing

    # --- opening ---
    opening_rolls: tuple[tuple[int, int], ...]  # every throw INCLUDING ties, in order

    # --- terminal ---
    result: GameResult | None = None


# Dice protocol (§4.3.4)
class DiceSource(Protocol):
    """Protocol for dice sources. The n-th die of the game (0-indexed) must be pure."""
    def die(self, n: int) -> int:
        """Return the n-th die of the game (0-indexed). Value is 1..6."""
        ...


class DiceExhausted(Exception):
    """Raised when ScriptedDice runs out of values."""
    pass
