"""backgammon/rules.py -- pure engine rules: coordinate primitives, single-move
validation, apply_move, sequence enumeration and outcome classification
(§4.3.2 / §4.3.3 / §4.3.6).

PART 1 (wave 2A):
    - coordinate helpers / point indexing (destination, is_bear_off, behind, pip_of)
    - may_bear_off, bear_off_legal
    - the private error-typing classifier (see the binding note on it below)
    - apply_move
    - classify (§4.3.3 -- outcome typing: single | gammon | backgammon)

PART 2 (wave 3, appended below the classify section):
    - legal_turns / distinct_turns / legal_moves (§4.3.2)
    - is_turn_complete, must_forfeit, result, pip_count (§4.3.3)
apply_move calls `legal_moves` by name; it is a forward reference resolved at
call time (names are looked up when the function runs, not when it is defined),
which is the seam between the two parts.

LIFECYCLE lives in backgammon/engine.py, NOT here (§3.2: rules.py is
"coordinates, move generation, sequence enumeration, application, outcome";
engine.py is the §4.3 lifecycle surface). engine.py imports this module; this
module imports nothing of engine.py.

The import rule (§3.3): this module is part of the ENGINE and must NOT import
asyncio, time, websockets, or anything I/O. Pure and synchronous only.
"""

from __future__ import annotations

from .types import (
    BAR_POINT,
    HOME,
    OFF_EDGE,
    Board,
    EnginePhase,
    GameResult,
    GameState,
    Move,
    MoveResult,
    Outcome,
    Player,
    Turn,
)


# --- Errors (§4.3.6) --------------------------------------------------------
# Full engine error hierarchy declared here so apply_move's raise contract is
# self-contained; wave 3 raises the remaining ones (TurnNotComplete, WrongPhase,
# NothingToUndo, OpeningUnresolved) from functions it adds to this same module.
# NOTE: `DiceExhausted` is intentionally NOT redeclared here -- it already
# exists in backgammon/types.py as `DiceExhausted(Exception)`, which does not
# match §4.3.6's `class DiceExhausted(EngineError)`. That is a pre-existing
# mismatch in a file outside this unit's ownership; flagged in the build report,
# not fixed here.
class EngineError(Exception):
    """Base class for all engine errors."""


class IllegalMove(EngineError):
    """Move is not in legal_moves(state) at all."""


class NotAPrefix(IllegalMove):
    """Move is individually playable, but is not a prefix of any maximal turn."""


class TurnNotComplete(EngineError):
    """Raised by end_turn (wave 3) when is_turn_complete(state) is False."""


class WrongPhase(EngineError):
    """Raised by roll_dice (wave 3) outside AWAITING_ROLL."""


class NothingToUndo(EngineError):
    """Raised by undo_last_move (wave 3) when moves_this_turn is empty."""


class SchemaMismatch(EngineError):
    """Raised by wire deserialization on protocol/schema mismatch."""


class Malformed(EngineError):
    """Raised by wire deserialization on shape/range errors."""


class OpeningUnresolved(EngineError):
    """Raised by new_game_from_opening (wave 3) after OPENING_THROWS_MAX ties."""


# --- Coordinate helpers / point indexing (§4.1.1, §4.3.3) ------------------
# Adopted verbatim from the frozen coordinate frame: ONE absolute frame, 1..24,
# never renumbered. `dest = src + direction * die` is the single formula for
# every move including bar entry and bear-off (§4.1.1 / CR-2).

def board_destination(player: Player, move: Move) -> int:
    """`destination` without a GameState -- the same single formula, for the
    enumeration DFS (which walks boards, not states). §4.1.1 / CR-2: ONE
    formula for every move including bar entry and bear-off."""
    return move.src + player.direction * move.die


def destination(state: GameState, move: Move) -> int:
    """Raw arithmetic src + direction*die. <=0 (WHITE) or >=25 (BLACK) means borne off."""
    return board_destination(state.turn, move)


def is_bear_off(player: Player, dest: int) -> bool:
    """True iff `dest` is off the board edge for `player` (dest <= 0 for WHITE,
    dest >= 25 for BLACK)."""
    return player.direction * (dest - OFF_EDGE[player]) >= 0


def behind(player: Player, q: int, p: int) -> bool:
    """True iff point q is further from off than point p, for `player`."""
    return player.direction * (q - p) < 0


def pip_of(player: Player, p: int) -> int:
    """Pip distance of point p from off, for `player`. WHITE: p. BLACK: 25 - p."""
    return abs(p - OFF_EDGE[player])


# --- Bear-off eligibility (§4.3.3) ------------------------------------------

def may_bear_off(board: Board, player: Player) -> bool:
    """board.bar(player) == 0 AND no own checker outside HOME[player]."""
    if board.bar(player) != 0:
        return False
    home = HOME[player]
    for point in range(1, 25):
        if point in home:
            continue
        if board.mine(player, point) > 0:
            return False
    return True


def bear_off_legal(board: Board, player: Player, src: int, die: int) -> bool:
    """§4.3.3 R12, written in the frame's own primitives so it is player-agnostic
    by construction. "HIGHER point" is a WHITE-centric word and MUST NOT appear
    here -- "further from off" (via `behind`) is correct for both colours."""
    if not may_bear_off(board, player):
        return False
    src_pip = pip_of(player, src)
    if src_pip == die:
        return True
    if src_pip < die:
        # overshoot: legal iff nothing further from off than src
        for q in range(1, 25):
            if behind(player, q, src) and board.mine(player, q) > 0:
                return False
        return True
    return False


# --- Single-move validation (§4.3.6, error-typing only) ---------------------

def _is_individually_playable(state: GameState, move: Move) -> bool:
    """PRIVATE. EXPLICITLY NON-AUTHORITATIVE. FOR ERROR TYPING ONLY.

    Bar rule + destination rule (empty / own / blot / blocked) + bear_off_legal,
    ignoring turn maximality and ignoring the higher-die rule.

    BINDING (§4.3.6): may not be called from anywhere in move generation --
    not from legal_turns, not from legal_moves, not from distinct_turns, not
    from the DFS. Its only caller is apply_move's error branch below.
    """
    player = state.turn
    board = state.board

    if move.die not in state.dice_remaining:
        return False

    if move.src == BAR_POINT[player]:
        if board.bar(player) <= 0:
            return False
    else:
        # bar rule: a checker on the bar must re-enter before any other move
        if board.bar(player) > 0:
            return False
        if not (1 <= move.src <= 24):
            return False
        if board.mine(player, move.src) <= 0:
            return False

    dest = destination(state, move)

    if is_bear_off(player, dest):
        return bear_off_legal(board, player, move.src, move.die)

    if not (1 <= dest <= 24):
        return False

    return not board.is_blocked(player, dest)


# --- apply_move (§4.3.1) -----------------------------------------------------

def _board_after(board: Board, player: Player, move: Move) -> tuple[Board, bool, bool]:
    """Apply `move` to `board` for `player`, returning (new_board, hit, bore_off).

    THE SINGLE board-mutation implementation in the engine: apply_move uses it,
    and so does the enumeration DFS in part 2. Two copies of hit/bear-off
    arithmetic that can drift apart is precisely how a generator and an applier
    end up disagreeing about what a turn does.

    NO legality checking whatsoever -- callers have already established that.
    """
    dest = board_destination(player, move)
    bore_off = is_bear_off(player, dest)
    hit = False

    points = list(board.points)
    bar_white, bar_black = board.bar_white, board.bar_black
    off_white, off_black = board.off_white, board.off_black

    # remove checker from source
    if move.src == BAR_POINT[player]:
        if player is Player.WHITE:
            bar_white -= 1
        else:
            bar_black -= 1
    else:
        points[move.src] -= player.value

    if bore_off:
        if player is Player.WHITE:
            off_white += 1
        else:
            off_black += 1
    else:
        opponent = player.opponent
        if board.is_blot(player, dest):
            hit = True
            points[dest] = 0
            if opponent is Player.WHITE:
                bar_white += 1
            else:
                bar_black += 1
        points[dest] += player.value

    new_board = Board(
        points=tuple(points),
        bar_white=bar_white,
        bar_black=bar_black,
        off_white=off_white,
        off_black=off_black,
    )
    return new_board, hit, bore_off


def apply_move(state: GameState, move: Move) -> MoveResult:
    """Raises IllegalMove if move not in legal_moves(state). Detects the win
    the instant off == 15 -- MID-TURN, discarding remaining dice -- and
    populates state.result. Never mutates `state`.

    Raise contract (§4.3.6, frozen):
        if move in legal_moves(state):                    apply it
        elif the private single-move classifier accepts:  raise NotAPrefix
        else:                                             raise IllegalMove
    """
    if move not in legal_moves(state):
        if _is_individually_playable(state, move):
            raise NotAPrefix(move)
        raise IllegalMove(move)

    player = state.turn
    dest = destination(state, move)
    new_board, hit, bore_off = _board_after(state.board, player, move)

    new_dice_remaining = _remove_one(state.dice_remaining, move.die)
    new_moves_this_turn = state.moves_this_turn + (move,)

    won = new_board.off(player) == 15

    if won:
        game_result = GameResult(
            winner=player,
            outcome=classify(new_board, player),
            loser_off=new_board.off(player.opponent),
            turns=state.turn_number,
        )
        new_state = GameState(
            board=new_board,
            turn=state.turn,
            phase=EnginePhase.GAME_OVER,
            turn_number=state.turn_number,
            dice_rolled=state.dice_rolled,
            dice_remaining=(),  # discarded mid-turn on the winning move
            moves_this_turn=new_moves_this_turn,
            turn_start_board=state.turn_start_board,
            dice=state.dice,
            dice_drawn=state.dice_drawn,
            opening_rolls=state.opening_rolls,
            result=game_result,
        )
        return MoveResult(state=new_state, dest=dest, hit=hit, bore_off=bore_off)

    # Not a win: determine whether the turn can continue. A candidate state at
    # AWAITING_MOVE is passed to legal_moves (wave 3) to decide; if no
    # continuation exists the turn moves to AWAITING_COMMIT (commit or undo).
    candidate = GameState(
        board=new_board,
        turn=state.turn,
        phase=EnginePhase.AWAITING_MOVE,  # placeholder; corrected below
        turn_number=state.turn_number,
        dice_rolled=state.dice_rolled,
        dice_remaining=new_dice_remaining,
        moves_this_turn=new_moves_this_turn,
        turn_start_board=state.turn_start_board,
        dice=state.dice,
        dice_drawn=state.dice_drawn,
        opening_rolls=state.opening_rolls,
        result=None,
    )
    can_continue = len(legal_moves(candidate)) > 0  # noqa: F821 -- wave 3 forward reference
    final_phase = EnginePhase.AWAITING_MOVE if can_continue else EnginePhase.AWAITING_COMMIT
    new_state = GameState(
        board=candidate.board,
        turn=candidate.turn,
        phase=final_phase,
        turn_number=candidate.turn_number,
        dice_rolled=candidate.dice_rolled,
        dice_remaining=candidate.dice_remaining,
        moves_this_turn=candidate.moves_this_turn,
        turn_start_board=candidate.turn_start_board,
        dice=candidate.dice,
        dice_drawn=candidate.dice_drawn,
        opening_rolls=candidate.opening_rolls,
        result=candidate.result,
    )
    return MoveResult(state=new_state, dest=dest, hit=hit, bore_off=bore_off)


def _remove_one(dice: tuple[int, ...], die: int) -> tuple[int, ...]:
    """Remove exactly one occurrence of `die` from `dice`, preserving order."""
    out = list(dice)
    out.remove(die)
    return tuple(out)


# --- Outcome classification (§4.3.3) ----------------------------------------

def classify(board: Board, winner: Player) -> Outcome:
    """single: loser.off >= 1
    gammon: loser.off == 0, loser has NO checker on the bar and NONE in HOME[winner]
    backgammon: loser.off == 0 AND (loser on bar OR loser has a checker in HOME[winner])
    HOME[winner] is 1..6 when WHITE wins, 19..24 when BLACK wins. (D1: bar counts.)
    """
    loser = winner.opponent
    if board.off(loser) >= 1:
        return Outcome.SINGLE

    loser_on_bar = board.bar(loser) > 0
    loser_in_winner_home = any(board.mine(loser, q) > 0 for q in HOME[winner])

    if loser_on_bar or loser_in_winner_home:
        return Outcome.BACKGAMMON
    return Outcome.GAMMON


# ===========================================================================
# PART 2 (WAVE 3) -- move generation and turn-boundary queries (§4.3.2/§4.3.3)
# ===========================================================================
#
# THE SINGLE MOST IMPORTANT SENTENCE IN THE SPEC (§4.3.2):
#   A single move is legal if and only if it is the first move of at least one
#   MAXIMAL complete turn sequence. Legality is a property of TURNS, not of
#   individual moves. Anything that validates moves one at a time gets the
#   must-use-both-dice rule wrong.
#
# Hence: one DFS over whole turns, and every other question answered from it.


def _ply_moves(board: Board, player: Player, dice_values: tuple[int, ...]) -> list[Move]:
    """Every single move `player` may play on THIS ply from `board`, given the
    distinct die values still available.

    Per-ply rules only -- the bar rule, the destination rule (empty / own /
    blot / blocked) and bear-off eligibility, the last of which is re-evaluated
    HERE, per move, never snapshotted at turn start (§4.3.2 step 1).

    This is DFS machinery. It is deliberately NOT the private single-move
    classifier used for error typing (§4.3.6 forbids that one anywhere in move
    generation): this function knows nothing about `dice_remaining` ordering,
    is called with the DFS's own running dice multiset, and its answer is only
    ever a candidate edge -- never, on its own, "this move is legal".
    """
    out: list[Move] = []

    if board.bar(player) > 0:
        # bar rule: nothing else may move while a checker is on the bar
        src = BAR_POINT[player]
        for die in dice_values:
            move = Move(src, die)
            dest = board_destination(player, move)
            if 1 <= dest <= 24 and not board.is_blocked(player, dest):
                out.append(move)
        return out

    for die in dice_values:
        for src in range(1, 25):
            if board.mine(player, src) <= 0:
                continue
            move = Move(src, die)
            dest = board_destination(player, move)
            if is_bear_off(player, dest):
                if bear_off_legal(board, player, src, die):
                    out.append(move)
            elif 1 <= dest <= 24 and not board.is_blocked(player, dest):
                out.append(move)
    return out


def _enumerate_sequences(board: Board, player: Player, rolled: tuple[int, ...]) -> list[Turn]:
    """DFS: every MAXIMAL sequence playable from `board` over the `rolled`
    multiset (§4.3.2 step 1). A branch stops when no die can be played, or
    immediately when off == 15 (step 4 -- the win ends the turn and the
    remaining dice are discarded).

    Returns at least one sequence always; when nothing is playable that one
    sequence is the EMPTY sequence, which step 2 of `legal_turns` turns into
    `()` -- the empty tuple OF TURNS, never `((),)`.
    """
    sequences: list[Turn] = []

    def walk(cur: Board, remaining: tuple[int, ...], seq: Turn) -> None:
        if cur.off(player) == 15:
            sequences.append(seq)
            return
        candidates = _ply_moves(cur, player, tuple(sorted(set(remaining))))
        if not candidates:
            sequences.append(seq)
            return
        for move in candidates:
            nxt, _hit, _bore_off = _board_after(cur, player, move)
            walk(nxt, _remove_one(remaining, move.die), seq + (move,))

    walk(board, tuple(rolled), ())
    return sequences


def legal_turns(state: GameState) -> tuple[Turn, ...]:
    """THE primitive. UNDEDUPED. (§4.3.2, algorithm verbatim.)

    DOMAIN: every maximal complete sequence FOR THE WHOLE TURN, enumerated from
    `state.turn_start_board` over `state.dice_rolled` -- NOT continuations from
    the current board over `dice_remaining`. Reading it the other way silently
    forfeits a die on every turn of every game.

    INVARIANT: every returned turn has `state.moves_this_turn` as its prefix.
    ORDERING IS UNSPECIFIED; callers compare as SETS.
    Returns () when the turn is forfeit -- never ((),).
    """
    rolled = tuple(state.dice_rolled)
    if not rolled:
        return ()

    # 1. DFS from turn_start_board over the FULL rolled multiset.
    sequences = _enumerate_sequences(state.turn_start_board, state.turn, rolled)

    # 2. Keep only the maximal-length survivors. L == 0 => the turn is forfeit.
    longest = max(len(seq) for seq in sequences)
    if longest == 0:
        return ()
    survivors = [seq for seq in sequences if len(seq) == longest]

    # 3. The higher-die rule: only when NOT doubles and only when exactly one
    #    die can be played at all. `dice_rolled`, not `dice_remaining`.
    is_doubles = len(set(rolled)) == 1
    if not is_doubles and longest == 1:
        higher = max(rolled)
        if any(seq[0].die == higher for seq in survivors):
            survivors = [seq for seq in survivors if seq[0].die == higher]

    # 5. Filter to turns that continue what has already been played this turn.
    played = tuple(state.moves_this_turn)
    k = len(played)
    if k:
        survivors = [seq for seq in survivors if seq[:k] == played]

    return tuple(survivors)


def _turn_outcome(
    state: GameState, turn: Turn
) -> tuple[Board, tuple[int, ...], GameResult | None]:
    """(board, dice_remaining, result) reached by playing `turn` in full from
    `state.turn_start_board` -- i.e. exactly the three fields of the resulting
    GameState that `distinct_turns` keys on.

    Computed directly rather than by looping `apply_move`, because `apply_move`
    calls `legal_moves` (which calls `legal_turns`, which calls this) -- the
    dedupe key must not re-enter move generation. The win semantics are kept
    identical to `apply_move`'s: off == 15 populates `result` and DISCARDS the
    remaining dice.
    """
    player = state.turn
    board = state.turn_start_board
    remaining = tuple(state.dice_rolled)
    for move in turn:
        board, _hit, _bore_off = _board_after(board, player, move)
        remaining = _remove_one(remaining, move.die)
        if board.off(player) == 15:
            return (
                board,
                (),
                GameResult(
                    winner=player,
                    outcome=classify(board, player),
                    loser_off=board.off(player.opponent),
                    turns=state.turn_number,
                ),
            )
    return board, remaining, None


def distinct_turns(state: GameState) -> tuple[Turn, ...]:
    """`legal_turns()` deduped by RESULTING POSITION (§4.3.2).

    DEDUPE KEY, frozen and explicit:
        (result_state.board, result_state.dice_remaining, result_state.result)
    and NOTHING ELSE -- in particular NOT the whole GameState, which carries
    `moves_this_turn` and would make this function the identity.

    CR-3, the measured landmine: this dedupe is for the §7.5 matrix's
    SET/COUNT/UNIQUE rows and for a "distinct options" UI. It is NEVER the
    basis for `legal_moves()` -- deduping by final position was MEASURED to
    eliminate legitimate first moves (W 6(2),3(2), dice (6,1): 3 legal first
    moves, 2 distinct final positions). `legal_moves` reads `legal_turns`.

    The representative kept per key is the lexicographically-smallest ordering,
    compared on (src, die) pairs since `Move` is not itself ordered.
    """
    best: dict[tuple, tuple[tuple, Turn]] = {}
    for turn in legal_turns(state):
        board, remaining, outcome = _turn_outcome(state, turn)
        key = (board, remaining, outcome)
        ordering = tuple((m.src, m.die) for m in turn)
        current = best.get(key)
        if current is None or ordering < current[0]:
            best[key] = (ordering, turn)
    return tuple(entry[1] for entry in best.values())


def legal_moves(state: GameState) -> tuple[Move, ...]:
    """The moves playable RIGHT NOW: the k-th move of every maximal turn that
    continues what has been played this turn (§4.3.2). Order-preserving and
    duplicate-free; the empty tuple means the turn is forfeit or finished.

    Derived from the UNDEDUPED `legal_turns` (ENGINE Finding F1 / CR-3), never
    from `distinct_turns`.

    `len(turn) <= k` is skipped rather than indexed: `legal_turns` returns ()
    and never ((),) on forfeit, but a turn that has been played to its end is
    itself a maximal turn of length k, and `turn[k]` on it is the same uncaught
    IndexError inside a message handler that §4.3.2 exists to prevent. There is
    no move to offer there -- the turn is complete (§4.3.3).
    """
    played = tuple(state.moves_this_turn)
    k = len(played)
    out: list[Move] = []
    seen: set[Move] = set()
    for turn in legal_turns(state):
        if len(turn) <= k or turn[:k] != played:
            continue
        move = turn[k]
        if move not in seen:
            seen.add(move)
            out.append(move)
    return tuple(out)


# --- Turn-boundary queries (§4.3.3) -----------------------------------------

def is_turn_complete(state: GameState) -> bool:
    """True iff dice_rolled != () (REQUIRED GATE) AND (dice_remaining is empty
    OR legal_moves(state) is empty)."""
    if state.dice_rolled == ():
        return False
    if state.dice_remaining == ():
        return True
    return legal_moves(state) == ()


def must_forfeit(state: GameState) -> bool:
    """True iff the player on roll has dice and cannot play ANY of them.

    The ONE case the transport auto-advances without client input (§4.6, CR-7).
    Both phase and dice_rolled are REQUIRED GATES.
    """
    if state.phase not in (EnginePhase.AWAITING_MOVE, EnginePhase.AWAITING_COMMIT):
        return False
    if state.dice_rolled == ():
        return False
    if len(state.moves_this_turn) != 0:
        return False
    return legal_moves(state) == ()


def result(state: GameState) -> GameResult | None:
    """The terminal result, or None while the game is live."""
    return state.result


def pip_count(board: Board, player: Player) -> int:
    """Total pips `player` must still travel: 25 per checker on the bar plus
    each point's distance from off. 167 per side at the start."""
    total = board.bar(player) * pip_of(player, BAR_POINT[player])
    for point in range(1, 25):
        count = board.mine(player, point)
        if count:
            total += count * pip_of(player, point)
    return total
