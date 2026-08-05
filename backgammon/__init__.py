"""backgammon -- the engine package.

Re-exports the §4.3 public surface and nothing else (§3.2). Importing this
package pulls in `types`, `dice`, `rules` and `engine` only -- never `wire`,
never anything under `transport`, so the import rule (§3.3) still reads
strictly downward from here.
"""

from .types import DiceExhausted
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
    bear_off_legal,
    behind,
    classify,
    destination,
    distinct_turns,
    is_bear_off,
    is_turn_complete,
    legal_moves,
    legal_turns,
    may_bear_off,
    must_forfeit,
    pip_count,
    pip_of,
    result,
)
from .engine import (
    GameRecord,
    end_turn,
    new_game,
    new_game_from_opening,
    replay,
    roll_dice,
    undo_last_move,
)

__all__ = [
    # move generation and rules (§4.3.2, §4.3.3)
    "apply_move",
    "bear_off_legal",
    "behind",
    "classify",
    "destination",
    "distinct_turns",
    "is_bear_off",
    "is_turn_complete",
    "legal_moves",
    "legal_turns",
    "may_bear_off",
    "must_forfeit",
    "pip_count",
    "pip_of",
    "result",
    # lifecycle (§4.3.1, §4.3.7)
    "GameRecord",
    "end_turn",
    "new_game",
    "new_game_from_opening",
    "replay",
    "roll_dice",
    "undo_last_move",
    # errors (§4.3.6)
    "DiceExhausted",
    "EngineError",
    "IllegalMove",
    "Malformed",
    "NotAPrefix",
    "NothingToUndo",
    "OpeningUnresolved",
    "SchemaMismatch",
    "TurnNotComplete",
    "WrongPhase",
]
