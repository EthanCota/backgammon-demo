"""backgammon/transport/session.py -- SessionState + GameSession (wave 5B).

Owns (§9.2's 5B row):
  - `SessionState` / `SeatRecord` exactly as frozen in §4.1.5
  - seats and the `hello` resolution order (§4.6.1), grace, auto-reseat
  - CAS, the rejection-order steps protocol.py deliberately does not hold
    (2 / 5 / 6 / 7) and the E1/E2/E3 exemptions (§4.5.2b)
  - series, frames, rematch (including the Rev 5-final-4 DECLINE RESET)
  - THE WHOLE OF §4.6.0: the `opening_roll` SessionPhase, `session.dice` +
    `dice_factory`, the deadline `asyncio.Task`, `opening_epoch`,
    `opening_interactive_available`, and transitions O-1..O-14
  - `_pre_apply_hook` -- awaited exactly ONCE per transition, immediately
    before that transition's effect list, and the effect list contains NO
    `await` (§4.6.0 (G) Rule 1, freeze item 15).

Does NOT own (wave 5C, `transport/server.py`): the serve loop,
`process_request`, the origin check, the token-bucket rate limiter
(rejection-order step 4), the 3-malformed-frames `1008` counter, and the
analytics JSONL append. `GameSession` exposes `reject()` so 5C can emit
those rejections through the same machinery (`error` + unsolicited fresh
`state`, §4.5.4).

`asyncio` and `time` are PERMITTED in this module (§3.3 / §4.1.5).

THE SIX NON-NEGOTIABLES (§9.2's 5B row), and where each one lives here:
  1. the deadline is a self-firing `asyncio.Task` -- `_arm_deadline` /
     `_opening_deadline`. Nothing polls `opening_deadline_at`; it exists
     ONLY to project `deadline_remaining_s` (§4.1.5).
  2. arm BEFORE `phase` is set, with no `await` between, and on arming
     failure do NOT enter the phase -- `_o1_entry_effect`.
  3. cancel the deadline AND bump `opening_epoch` on every exit -- and the
     cancel is `if task is not asyncio.current_task()` (`_cancel_deadline`),
     because O-5 runs INSIDE that task and a bare `.cancel()` kills this
     transition's own post-effect broadcast (§4.6.0 (E) hazard 1).
  4. every transition re-validates after its single hook await -- per
     (G)'s SCOPE table, which prescribes a DIFFERENT re-check per row. See
     `_RECHECK` below for the row-by-row map actually implemented.
  5. `session.dice` is re-minted on every `game_id` increment.
  6. `opening_interactive_available` resets on `abandoned` AND on a rematch
     DECLINE, persists across a rematch ACCEPT, and is READ IN EXACTLY ONE
     PLACE: O-1's guard in `_o1_entry_effect`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from ..dice import production_dice
from ..engine import end_turn as engine_end_turn
from ..engine import new_game_from_opening, roll_dice, undo_last_move
from ..engine import (
    EngineError,
    apply_move,
    destination,
    legal_moves,
    must_forfeit,
)
from ..types import (
    BAR_POINT,
    OPENING_INTERACTIVE_THROWS_MAX,
    OPENING_ROLL_DEADLINE_S,
    STARTING_POINTS,
    SEAT_DIE_INDEX,
    Board,
    DiceSource,
    EnginePhase,
    GameState,
    Move,
    Player,
    SessionPhase,
)
from ..wire import board_to_wire, frame_to_wire, move_from_wire
from .protocol import (
    CloseCode,
    EndTurnMessage,
    HelloMessage,
    MoveMessage,
    ProtocolError,
    RematchMessage,
    ReplayRequestMessage,
    RollMessage,
    UndoMessage,
    check_hello_protocol,
    construct_blocked,
    construct_error,
    construct_replay,
    construct_seat_assigned,
    construct_state,
    extract_cid,
    map_engine_error,
    parse_client_message,
)

Seat = Literal["white", "black"]

SEAT_ORDER: tuple[Seat, ...] = ("white", "black")
SEAT_PLAYER: dict[str, Player] = {"white": Player.WHITE, "black": Player.BLACK}
PLAYER_SEAT: dict[Player, str] = {Player.WHITE: "white", Player.BLACK: "black"}

# §4.6.1 -- a judgment call, explicitly not a measured number. Read as a BARE
# MODULE GLOBAL at use time so `backgammon.transport.session.GRACE_SECONDS` is
# the one legal monkeypatch target (§4.6.1's Rev 4 box). The identical rule
# governs OPENING_ROLL_DEADLINE_S, which is imported above and likewise read
# bare inside `_arm_deadline` -- never captured into a default argument, a
# class attribute, or an import-time constant.
GRACE_SECONDS: float = 90.0

# §4.6.1 step 5 -- default ON. Read at call time so tests can flip it.
ALLOW_UNTOKENED_GRACE_CLAIM: bool = os.environ.get("ALLOW_UNTOKENED_GRACE_CLAIM", "1") != "0"


# --- (G)'s SCOPE TABLE, as implemented ---------------------------------------
#
# This is a comment, not a lookup table, because each row's re-check is
# inlined at its own call site -- but the map is written down here so it can
# be checked against §4.6.0 (G) without reading the whole module.
#
#   O-2, O-5            LITERAL TEMPLATE: epoch + `phase is OPENING_ROLL` +
#                       own guard re-read.  (`_handle_opening_roll`,
#                       `_apply_o5`)
#   O-6, O-7, O-8       NO re-check of their own -- fall-through legs of
#                       O-2/O-5, one transition and one await under Rule 1.
#                       Their Guard-column conditions are evaluated INSIDE
#                       the effect list, after the dice are written, which is
#                       the only point at which they are evaluable at all.
#                       (`_resolve_throw`)
#   O-3, O-4, O-10      PURE REJECTIONS: no hook await, no re-check. A
#                       re-check must never suppress O-3's `not_your_turn`.
#   O-1                 `phase is lobby` + the resolved seat is still `open`
#                       + the OTHER seat is still `live` + the flag re-read.
#                       NO epoch clause. An aborted O-1 does NOT return bare:
#                       it re-runs §4.6.1's resolution against post-await
#                       state (the ENTRY-TRANSITION CARVE-OUT).
#                       (`_handle_hello`)
#   O-9                 NO phase clause (grace expiry is valid from EVERY
#                       phase), NO epoch clause; re-read guard = the seat is
#                       still `grace` AND its `grace_deadline` has still
#                       elapsed. (`_apply_o9`)
#   O-11                NO re-check -- runs inside O-6 / O-8 / O-7's
#                       re-arm-failure fallback, i.e. inside an effect list.
#   O-12                `phase is game_over` + `game_id` unchanged + the OTHER
#                       seat has not WITHDRAWN a vote it had already cast.
#                       NO epoch clause. ⚠️ The withdrawal test is `was true
#                       AND is now false`, NOT `!= what it was`: a favourable
#                       concurrent vote (False -> True in our window) is the
#                       simultaneous-accept case, not an invalidation.
#                       (`_handle_rematch`)
#   O-13                NO phase clause, NO epoch clause; re-read guard = the
#                       seat is still in the status the resolving step read
#                       it in. An aborted O-13 re-runs §4.6.1's resolution,
#                       exactly like O-1. (`_handle_hello`)
#   O-14                NO re-check AT ALL -- runs inside another
#                       transition's `except` block.
#
# Generalisation 3, which the rows above obey: a post-await re-read may name
# ONLY state that was already true BEFORE the effect list ran.


async def _noop() -> None:
    """The default `_pre_apply_hook` (§4.6.1). Never yields to the loop."""
    return None


async def _send_nowhere(conn: object, frame: dict) -> None:
    """Default sender: a session with no sockets attached (unit tests, 5C
    wiring it up later). Frames are still constructed and dropped here."""
    return None


async def _close_nothing(conn: object, code: int, reason: str) -> None:
    return None


def _now_iso() -> str:
    """RFC-3339 UTC with `Z` (§4.2)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Event:
    """`SessionState.last_event` (§4.2.3). Presentational, non-authoritative."""
    kind: str
    seat: str | None = None
    detail: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {"kind": self.kind, "seat": self.seat, "detail": dict(self.detail)}


@dataclass
class SeatRecord:
    """§4.1.5."""
    conn: object | None = None                 # websockets ServerConnection
    token: str | None = None                   # secrets.token_urlsafe(24)
    status: Literal["open", "live", "grace"] = "open"
    grace_deadline: float | None = None        # monotonic clock
    since: str | None = None                   # RFC-3339 Z


@dataclass
class SessionState:
    """§4.1.5 -- MUTABLE, single-writer (one asyncio loop).

    Field-for-field as frozen. Nothing is added: the grace TIMER tasks and
    the replay frames' per-frame metadata live on `GameSession` as private
    attributes, precisely so this dataclass keeps its frozen shape.
    """

    session_id: str
    game_id: str
    state_seq: int
    phase: SessionPhase | None
    game: GameState | None
    seats: dict[str, SeatRecord]
    series: dict[str, int]
    rematch: dict[str, bool]
    frames: list[GameState]
    last_event: Event | None

    # Rev 4 -- the session OWNS the DiceSource; re-minted on every `game_id`
    # increment (§4.1.5).
    dice: DiceSource

    # Rev 3 -- the interactive opening roll (§4.6.0). ALL TRANSPORT-ONLY.
    opening_dice: dict[str, int | None]
    opening_rolls: list[tuple[int, int]]
    opening_timed_out: bool

    # Rev 4 -- the deadline is TWO fields and the timer is a REAL asyncio
    # object. `opening_deadline_at` EXISTS ONLY to compute
    # `deadline_remaining_s`; NOTHING may branch on it.
    opening_deadline: "asyncio.Task[None] | None"
    opening_deadline_at: float | None

    # NEVER RESET. Monotonic for the life of the session, like state_seq.
    opening_epoch: int

    # Rev 5 / Rev 5-final-4 -- the first-match predicate. Read in exactly ONE
    # place: O-1's guard.
    opening_interactive_available: bool


class GameSession:
    """One session = one pair of seats = one process (§3.1).

    5C drives it:

        session = GameSession(send=..., close=...)
        await session.handle_message(conn, decoded_json)   # steps 1,2,3,5,6,7,8
        await session.connection_lost(conn)                # seat -> grace
        await session.reject(conn, "rate_limited", "...", cid)   # step 4 is 5C's

    `send(conn, frame)` and `close(conn, code, reason)` are awaitables. Every
    frame this class emits goes through them, and every one is emitted AFTER
    the transition's effect list (§4.6.0 (G) Rule 1).
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        dice_factory: Callable[[], DiceSource] = production_dice,
        send: Callable[[object, dict], Awaitable[None]] = _send_nowhere,
        close: Callable[[object, int, str], Awaitable[None]] = _close_nothing,
        analytics: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._dice_factory = dice_factory
        self._send = send
        self._close = close
        self._analytics = analytics
        self._pre_apply_hook: Callable[[], Awaitable[None]] = _noop

        self._grace_tasks: dict[str, asyncio.Task[None]] = {}
        # Parallel to SessionState.frames, appended in lockstep by
        # `_record_frame` so the two cannot desync. §4.1.5 types `frames` as
        # `list[GameState]`; §4.2.3's replay frames additionally carry
        # kind/seat/move, which wire.frame_to_wire needs -- that metadata is
        # private to this class rather than a new SessionState field.
        self._frame_meta: list[tuple[str, str | None, Move | None]] = []
        self._series_counted_for: str | None = None
        self._game_counter = 1
        self._started_at = _now_iso()
        self._outbox: list[tuple[object, dict]] = []
        self._closes: list[tuple[object, int, str]] = []

        sid = session_id or ("s_" + secrets.token_hex(4))
        self.state = SessionState(
            session_id=sid,
            game_id="g_1",
            state_seq=0,
            phase=SessionPhase.LOBBY,
            game=None,
            seats={s: SeatRecord() for s in SEAT_ORDER},
            series={s: 0 for s in SEAT_ORDER},
            rematch={s: False for s in SEAT_ORDER},
            frames=[],
            last_event=None,
            dice=dice_factory(),
            opening_dice={s: None for s in SEAT_ORDER},
            opening_rolls=[],
            opening_timed_out=False,
            opening_deadline=None,
            opening_deadline_at=None,
            opening_epoch=0,
            opening_interactive_available=True,
        )

    # --- the hook (§4.6.1, freeze item 15) ----------------------------------

    @property
    def pre_apply_hook(self) -> Callable[[], Awaitable[None]]:
        return self._pre_apply_hook

    @pre_apply_hook.setter
    def pre_apply_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._pre_apply_hook = hook

    # ------------------------------------------------------------------
    # Invariant (freeze item 16.2) -- asserted after EVERY session transition
    # ------------------------------------------------------------------

    def _assert_invariant(self) -> None:
        st = self.state
        assert (st.phase is SessionPhase.OPENING_ROLL) == (st.opening_deadline is not None), (
            "freeze item 16.2 violated: phase="
            f"{st.phase!r} opening_deadline={st.opening_deadline!r}"
        )

    # ------------------------------------------------------------------
    # Frame plumbing. Nothing here mutates SessionState.
    # ------------------------------------------------------------------

    def _seated_conns(self) -> list[object]:
        out: list[object] = []
        for s in SEAT_ORDER:
            rec = self.state.seats[s]
            if rec.conn is not None and rec.status == "live":
                out.append(rec.conn)
        return out

    def _queue(self, conn: object, frame: dict) -> None:
        self._outbox.append((conn, frame))

    def _queue_close(self, conn: object, code: int, reason: str) -> None:
        self._closes.append((conn, code, reason))

    def _queue_broadcast(self) -> dict:
        """ONE frame object, queued to every seated connection -- byte-identical
        to both seats (§4.2.3)."""
        frame = self.state_frame()
        for conn in self._seated_conns():
            self._queue(conn, frame)
        return frame

    async def _flush(self) -> None:
        """Post-effect I/O. This is the await that a self-cancelled deadline
        task would die on -- see `_cancel_deadline`."""
        outbox, self._outbox = self._outbox, []
        closes, self._closes = self._closes, []
        for conn, frame in outbox:
            await self._send(conn, frame)
        for conn, code, reason in closes:
            await self._close(conn, code, reason)

    def _bump(self) -> None:
        self.state.state_seq += 1

    # ------------------------------------------------------------------
    # The `state` frame (§4.2.3)
    # ------------------------------------------------------------------

    def _opening_projection(self) -> dict | None:
        st = self.state
        if st.phase is SessionPhase.OPENING_ROLL:
            remaining = None
            if st.opening_deadline_at is not None:
                remaining = max(0.0, st.opening_deadline_at - time.monotonic())
            return {
                "white": st.opening_dice["white"],
                "black": st.opening_dice["black"],
                "rolls": [list(r) for r in st.opening_rolls],
                "awaiting": [s for s in SEAT_ORDER if st.opening_dice[s] is None],
                "deadline_remaining_s": remaining,
                "throw": len(st.opening_rolls),
                # §4.2.3's frozen live example pins this: `rolls: [[4,4]]`
                # (len 1) shows `interactive_throws_remaining: 2` with
                # MAX = 3 -- so it counts the throw IN PROGRESS, and is
                # `MAX - len(opening_rolls)`, not `MAX - len - 1`.
                "interactive_throws_remaining": max(
                    0, OPENING_INTERACTIVE_THROWS_MAX - len(st.opening_rolls)
                ),
            }
        if st.game is None:
            # `lobby` and `abandoned` -- the live opening object exists ONLY in
            # `opening_roll` (§4.2.3 WO2 table).
            return None
        rolls = st.game.opening_rolls
        white, black = (rolls[-1] if rolls else (None, None))
        return {
            "white": white,
            "black": black,
            "rolls": [list(r) for r in rolls],
            "awaiting": [],
            "deadline_remaining_s": None,
            "throw": None,
            "interactive_throws_remaining": 0,
        }

    def _legal_moves_wire(self) -> list[dict]:
        game = self.state.game
        if game is None or game.phase is EnginePhase.GAME_OVER:
            return []
        out: list[dict] = []
        for mv in legal_moves(game):
            dest = destination(game, mv)
            off = dest <= 0 or dest >= 25
            out.append({
                "from": "bar" if mv.src == BAR_POINT[game.turn] else mv.src,
                "to": "off" if off else dest,
                "die": mv.die,
                "hit": (not off) and game.board.is_blot(game.turn, dest),
            })
        return out

    def _players_wire(self) -> dict:
        now = time.monotonic()
        out = {}
        for s in SEAT_ORDER:
            rec = self.state.seats[s]
            grace_remaining = None
            if rec.status == "grace" and rec.grace_deadline is not None:
                grace_remaining = max(0.0, rec.grace_deadline - now)
            out[s] = {
                "present": rec.status == "live",
                "since": rec.since,
                "grace_remaining_s": grace_remaining,
            }
        return out

    def state_frame(self) -> dict:
        """The authoritative snapshot (§4.2.3), including the Rev 5/WO2 rule
        that `board` is NEVER null in ANY phase."""
        st = self.state
        game = st.game
        if game is None:
            phase = (st.phase or SessionPhase.LOBBY).value
            board = board_to_wire(Board(points=STARTING_POINTS))
            dice = {"rolled": [], "remaining": [], "is_double": False}
            active_seat = None
            turn_number = 0
            legal = []
            can_undo = False
            can_end_turn = False
            outcome = None
            opening = self._opening_projection()
            awaiting_input = bool(
                st.phase is SessionPhase.OPENING_ROLL and opening and opening["awaiting"]
            )
        else:
            phase = game.phase.value
            board = board_to_wire(game.board)
            dice = {
                "rolled": list(game.dice_rolled),
                "remaining": list(game.dice_remaining),
                "is_double": len(game.dice_rolled) == 4,
            }
            active_seat = PLAYER_SEAT[game.turn]
            turn_number = game.turn_number
            legal = self._legal_moves_wire()
            can_undo = len(game.moves_this_turn) > 0
            can_end_turn = game.phase is EnginePhase.AWAITING_COMMIT
            outcome = None
            if game.result is not None:
                outcome = {
                    "winner": PLAYER_SEAT[game.result.winner],
                    "type": game.result.outcome.value,
                    "turns": game.result.turns,
                }
            opening = self._opening_projection()
            awaiting_input = game.phase in (
                EnginePhase.AWAITING_ROLL,
                EnginePhase.AWAITING_MOVE,
                EnginePhase.AWAITING_COMMIT,
            )

        return construct_state(
            session_id=st.session_id,
            game_id=st.game_id,
            state_seq=st.state_seq,
            phase=phase,
            active_seat=active_seat,
            awaiting_input=awaiting_input,
            turn_number=turn_number,
            board=board,
            dice=dice,
            opening=opening,
            legal_moves=legal,
            can_undo=can_undo,
            can_end_turn=can_end_turn,
            players=self._players_wire(),
            series=dict(st.series),
            rematch=dict(st.rematch),
            outcome=outcome,
            last_event=st.last_event.to_wire() if st.last_event else None,
            server_time=_now_iso(),
        )

    # ------------------------------------------------------------------
    # Errors (§4.5.4) -- every error is followed by an unsolicited fresh state
    # ------------------------------------------------------------------

    async def reject(self, conn: object, code: str, message: str, cid: str | None) -> None:
        """Emit `error` + the unsolicited fresh `state` (§4.5.4). Does NOT bump
        `state_seq` -- a unicast is a re-send, not a mutation (§4.2.3).

        Public because 5C needs it for the two rejection-order steps it owns
        (step 1's malformed counter and step 4's rate limiter).
        """
        self._queue(conn, construct_error(code, message, cid, self.state.state_seq))
        self._queue(conn, self.state_frame())
        await self._flush()

    # ------------------------------------------------------------------
    # The deadline (§4.1.5 firing mechanism, §4.6.0 (A), (E), (G))
    # ------------------------------------------------------------------

    def _arm_deadline(self) -> None:
        """Bump the epoch, write `opening_deadline_at`, create the TASK.

        NO `await` anywhere in here, and the caller sets `phase` AFTER it
        (§4.6.0 (A) item 2). May raise (`RuntimeError: no running event loop`
        under a bare harness, a loop mid-shutdown): every caller has a
        prescribed failure path and NONE of them enter/stay in the phase with
        no timer.
        """
        st = self.state
        st.opening_epoch += 1
        epoch = st.opening_epoch
        delay = OPENING_ROLL_DEADLINE_S            # BARE module-global read, at arm time
        st.opening_deadline_at = time.monotonic() + delay
        st.opening_deadline = asyncio.create_task(self._opening_deadline(epoch, delay))

    async def _opening_deadline(self, epoch: int, delay: float) -> None:
        """THE FIRING MECHANISM. Self-scheduling; nothing polls it."""
        await asyncio.sleep(delay)
        await self._apply_o5(epoch)

    def _cancel_deadline(self) -> None:
        """Cancel the armed timer, EXCEPT when it is the task we are running in.

        🔴 §4.6.0 (E) hazard 1 / Rev 5-final-3. O-5 runs INSIDE
        `opening_deadline` and falls through to O-6 / O-7 / O-8 (and via their
        `EngineError` into O-11). `Task.cancel()` on a RUNNING task sets
        `_must_cancel`, so the next future the coroutine awaits -- this
        transition's own post-effect broadcast in `_flush` -- raises
        `CancelledError` and the frame is never sent, with server state
        already correct. Skipping the cancel leaks nothing: that task returns
        as soon as the broadcast completes.

        The field is nulled EITHER WAY (the caller may reassign it, as O-7's
        re-arm does).
        """
        task = self.state.opening_deadline
        self.state.opening_deadline = None
        if task is None:
            return
        try:
            running = asyncio.current_task()
        except RuntimeError:                       # no running loop
            running = None
        if task is not running:
            task.cancel()

    def _clear_opening_fields(self) -> None:
        """Everything §4.1.5 calls an opening field -- and NOT `opening_epoch`,
        which is monotonic for the life of the session, nor `phase`, which is
        not an opening field and is set explicitly by each exit (O-8's
        Rev 5-final-2 correction)."""
        st = self.state
        st.opening_dice = {s: None for s in SEAT_ORDER}
        st.opening_rolls = []
        st.opening_timed_out = False
        st.opening_deadline_at = None
        # `opening_deadline` itself is nulled by `_cancel_deadline`, which
        # every exit calls first (so the current_task carve-out is honoured).
        st.opening_deadline = None

    def _draw(self, k: int, seat: str) -> int:
        """`draw(seat)` -- THE SAME CALL ON EVERY PATH (§4.6.0, §4.3.4)."""
        return self.state.dice.die(2 * k + SEAT_DIE_INDEX[SEAT_PLAYER[seat]])

    # ------------------------------------------------------------------
    # Frames (replay buffer for the CURRENT game_id only)
    # ------------------------------------------------------------------

    def _record_frame(self, kind: str, seat: str | None, move: Move | None) -> None:
        game = self.state.game
        if game is None:
            return
        self.state.frames.append(game)
        self._frame_meta.append((kind, seat, move))

    def _reset_frames(self) -> None:
        self.state.frames = []
        self._frame_meta = []

    # ------------------------------------------------------------------
    # O-1 / O-12 / O-14: the server-resolved start and its error path
    # ------------------------------------------------------------------

    def _server_resolved_start(self) -> None:
        """`new_game_from_opening(session.dice, ())` -- Rev 2's behaviour BY
        IDENTITY (§4.3.1), reached from O-1's guard-`False` branch, O-1's
        arming-failure fallback, and O-12. On `EngineError` -> O-14.

        Runs INSIDE an effect list: no await, no re-check.
        """
        st = self.state
        st.opening_deadline_at = None               # REV 5-FINAL defensive clear
        try:
            game = new_game_from_opening(st.dice, ())
        except EngineError as exc:
            self._apply_o14(exc)
            return
        st.game = game
        st.phase = None
        st.opening_interactive_available = False
        st.last_event = Event("game_started", None, {"server_resolved": True})
        self._reset_frames()
        self._record_frame("start", None, None)

    def _o1_entry_effect(self, seat: str | None = None) -> None:
        """O-1's effect list. Called from the `hello` route AND, verbatim, from
        an auto-reseat that leaves two `live` seats (§4.6.1's Rev 4 binding).

        Runs INSIDE an effect list: no await. Its post-await re-check, when it
        has one at all, has already been performed by the caller.
        """
        st = self.state

        # 🔴 THE ONE AND ONLY READ OF `opening_interactive_available`.
        if not st.opening_interactive_available:
            # Guard-FALSE branch. UNREACHABLE BY CONSTRUCTION since the
            # Rev 5-final-4 decline reset (§4.6.0's O-1 row gives the three
            # reasons it is KEPT anyway; its effect list is still reached by
            # the arming-failure fallback below). No `game_id++` and no
            # dice re-mint here, deliberately.
            self._server_resolved_start()
            return

        try:
            self._arm_deadline()                    # epoch++, deadline_at, TASK
        except BaseException:
            # ARMING FAILED -> DO NOT ENTER THE PHASE (§4.6.0 (A) item 3).
            # `opening_deadline_at` was written before the create_task that
            # raised; clear it (REV 5-FINAL).
            st.opening_deadline = None
            st.opening_deadline_at = None
            self._server_resolved_start()
            return

        st.opening_dice = {s: None for s in SEAT_ORDER}
        st.opening_rolls = []
        st.opening_timed_out = False
        st.last_event = Event("seat_joined", seat, {})
        st.phase = SessionPhase.OPENING_ROLL        # LAST, no await since the arm

    # ------------------------------------------------------------------
    # O-6 / O-7 / O-8 -- fall-through legs. NO re-check of their own.
    # ------------------------------------------------------------------

    def _resolve_throw(self, completing_seat: str | None) -> None:
        """Select and run O-6 / O-7 / O-8 (§4.6.0).

        Called only from INSIDE O-2's or O-5's effect list, after that
        transition's single hook await and after ITS post-await re-check. The
        guards below are evaluated here, after the dice have been written,
        which is the only point at which they are evaluable at all
        (Rev 5-final-2). No await anywhere in this call tree.
        """
        st = self.state

        # 🔴 THE O-6 / O-8 TIMEOUT CARVE-OUT (§4.6.0's O-6 and O-8 rows,
        # §7.7 assertion ②). O-5 falls through into this function IN THE SAME
        # TRANSITION, with ONE broadcast at the end, having already written
        # `last_event = opening_timeout{seats:[...]}`. O-6 and O-8 must NOT
        # overwrite that with `game_started`, or the single frame both clients
        # receive cannot distinguish "your opponent rolled" from "the server
        # drew your opponent's die after they went silent" -- which is the one
        # thing that frame exists to make observable.
        #
        # CAPTURED HERE, NEVER RE-READ AT THE WRITE SITE: `_clear_opening_fields()`
        # resets `opening_timed_out` to False and runs BEFORE the `last_event`
        # write in BOTH O-6 and O-8, so a read at the write site is always
        # False and the carve-out would silently never fire.
        #
        # O-7 gets NO carve-out and needs none: its guard requires
        # `not opening_timed_out`, so O-7 is UNREACHABLE from O-5.
        #
        # The carve-out deliberately does NOT extend to the O-11 error path:
        # if `new_game_from_opening` raises here, `_abandoned_recovery` writes
        # `opponent_abandoned`, and that must win -- no game started at all.
        timeout_originated = st.opening_timed_out

        white = st.opening_dice["white"]
        black = st.opening_dice["black"]
        if white is None or black is None:
            # Throw incomplete -- O-2's "the OTHER seat still owes a die" leg.
            st.last_event = Event(
                "opening_die", completing_seat,
                {"die": st.opening_dice[completing_seat] if completing_seat else None},
            )
            return

        rolls = list(st.opening_rolls) + [(white, black)]
        throw_index = len(st.opening_rolls)

        if white != black:
            # --- O-6 --------------------------------------------------------
            st.opening_epoch += 1
            self._cancel_deadline()                 # current_task carve-out
            try:
                game = new_game_from_opening(st.dice, tuple(rolls))
            except EngineError as exc:
                self._apply_o11(exc)                # O-11, interactive call site
                return
            st.game = game
            st.phase = None
            self._clear_opening_fields()
            st.opening_interactive_available = False
            self._set_game_started(timeout_originated, {})   # carve-out, see above
            self._reset_frames()
            self._record_frame("start", None, None)
            return

        if len(rolls) < OPENING_INTERACTIVE_THROWS_MAX and not st.opening_timed_out:
            # --- O-7 -- interactive re-roll ---------------------------------
            # `rolls` INCLUDES the just-completed throw: with MAX = 3 that is
            # 3 interactive throws total and two consecutive ties to reach the
            # 180 s worst case of §4.6.0 (D).
            st.opening_rolls = rolls
            st.opening_dice = {s: None for s in SEAT_ORDER}
            self._cancel_deadline()                 # current_task carve-out
            try:
                self._arm_deadline()                # epoch++ + new TASK
            except BaseException:
                # RE-ARM FAILURE (Rev 5-final-2, BLOCKING). §4.6.0 (A) item 3's
                # fallback is entry-transition-specific and vacuous here, so
                # this takes O-8's resolution instead. NO second epoch bump --
                # `_arm_deadline` already bumped before it raised, and that one
                # bump serves as this exit's bump.
                st.opening_deadline = None
                # `timeout_originated` is provably False on this leg -- O-7's
                # guard above required `not opening_timed_out` to get here. It
                # is threaded anyway so the carve-out has exactly one shape.
                self._resolve_server_side_from_opening_roll(
                    timeout_originated=timeout_originated)
                return
            st.last_event = Event("opening_tie", None, {"throw": throw_index, "die": white})
            return

        # --- O-8 -- the tie cap, or the timeout latch -------------------------
        st.opening_rolls = rolls
        st.opening_epoch += 1
        self._cancel_deadline()                     # current_task carve-out
        self._resolve_server_side_from_opening_roll(timeout_originated=timeout_originated)

    def _set_game_started(self, timeout_originated: bool, detail: dict) -> None:
        """Write O-6 / O-8's `game_started` event -- UNLESS this transition
        originated in O-5's opening timeout.

        🔴 THE ONE AND ONLY WRITE OF `game_started` ON THE INTERACTIVE
        (`From = opening_roll`) EXITS, and the whole of the O-6 / O-8 timeout
        carve-out. `_resolve_throw` documents why it exists and why the flag
        must be captured before `_clear_opening_fields()` rather than read
        here. §4.6.0's O-6 and O-8 rows carry the same carve-out, so the state
        table and §7.7 assertion ② no longer contradict each other.

        `_server_resolved_start` (O-1's guard-`False` branch, O-1's
        arming-failure fallback, O-12) does NOT route through here: those
        paths never enter `opening_roll`, so O-5 cannot have run and there is
        no timeout event to preserve.
        """
        if timeout_originated:
            return                                  # `opening_timeout` survives
        self.state.last_event = Event("game_started", None, detail)

    def _resolve_server_side_from_opening_roll(self, *, timeout_originated: bool) -> None:
        """O-8's resolution -- shared verbatim with O-7's re-arm-failure exit.

        `opening_rolls` EXACTLY as it stands: the engine re-derives every
        remaining throw from the same pure indexed source (§4.3.1 steps 2/4).
        `EngineError` here routes into O-11, not O-14: `From` is still
        `opening_roll`, so this is an INTERACTIVE call site.

        `timeout_originated` is REQUIRED (no default) so a future caller has to
        answer the question rather than inherit a silent `False`: it carries
        the O-8 timeout carve-out from `_resolve_throw`, which captured it
        before `_clear_opening_fields()` cleared the flag.
        """
        st = self.state
        try:
            game = new_game_from_opening(st.dice, tuple(st.opening_rolls))
        except EngineError as exc:
            self._apply_o11(exc)
            return
        st.game = game
        st.phase = None                             # REV 5-FINAL-2: NOT an opening field
        self._clear_opening_fields()
        st.opening_interactive_available = False
        self._set_game_started(timeout_originated, {})   # O-8's carve-out
        self._reset_frames()
        self._record_frame("start", None, None)

    # ------------------------------------------------------------------
    # O-9 / O-11 / O-14 -- one recovery, reached three ways
    # ------------------------------------------------------------------

    def _emit_analytics(self, event: str, detail: dict) -> None:
        if self._analytics is not None:
            self._analytics(event, detail)

    def _apply_o11(self, exc: EngineError) -> None:
        """O-11 -- the INTERACTIVE error path (`From = opening_roll`)."""
        self._emit_analytics("opening_unresolved", {
            "game_id": self.state.game_id, "error": type(exc).__name__, "path": "interactive",
        })
        self._abandoned_recovery(reason="opening_unresolved")

    def _apply_o14(self, exc: EngineError) -> None:
        """O-14 -- the SERVER-RESOLVED error path (`From = game_over` or
        `lobby`). NO re-check at all; runs inside another effect list."""
        self._emit_analytics("opening_unresolved", {
            "game_id": self.state.game_id, "error": type(exc).__name__, "path": "server_resolved",
        })
        self._abandoned_recovery(reason="opening_unresolved")

    def _next_game_id(self) -> str:
        self._game_counter += 1
        return f"g_{self._game_counter}"

    def _abandoned_recovery(self, *, reason: str) -> None:
        """The `abandoned` reset + auto-reseat (§4.6.1, O-9's effect list).

        Runs INSIDE an effect list: no await. Shared verbatim by O-9, O-11 and
        O-14 -- the spec is emphatic that there is ONE recovery, not three.
        """
        st = self.state
        st.opening_epoch += 1
        self._cancel_deadline()                     # current_task carve-out (O-11 route)

        survivors: list[object] = []
        for s in SEAT_ORDER:
            rec = st.seats[s]
            if rec.conn is not None and rec.status == "live":
                survivors.append(rec.conn)
            self._cancel_grace_timer(s)
            st.seats[s] = SeatRecord()              # -> open, no conn, no token

        self._clear_opening_fields()
        st.phase = SessionPhase.ABANDONED
        st.game = None
        st.series = {s: 0 for s in SEAT_ORDER}
        st.rematch = {s: False for s in SEAT_ORDER}
        st.game_id = self._next_game_id()
        st.dice = self._dice_factory()              # RE-MINT on every game_id++
        self._reset_frames()
        # ORDERING IS BINDING: the flag is True BEFORE the auto-reseat runs, so
        # O-1's guard passes and the reseat really does arm an interactive
        # opening (§4.6.1's auto-reseat box).
        st.opening_interactive_available = True
        st.last_event = Event("opponent_abandoned", None, {"reason": reason})

        st.phase = SessionPhase.LOBBY
        self._auto_reseat(survivors)

    def _auto_reseat(self, survivors: list[object]) -> None:
        """§4.6.1: every still-open connection is IMMEDIATELY AUTO-RESEATED in
        connection order (white first), each receiving a fresh `seat_assigned`
        with a new `resume_token`. Runs step 4's EFFECT, and if that leaves two
        `live` seats it executes O-1's entry effect in the same transition."""
        st = self.state
        for conn in survivors:
            seat = self._first_open_seat()
            if seat is None:
                break
            self._bind_seat(seat, conn)
            self._queue(conn, construct_seat_assigned(
                seat=seat,
                player=int(SEAT_PLAYER[seat]),
                resume_token=st.seats[seat].token or "",
                session_id=st.session_id,
                resumed=False,
            ))
        if all(st.seats[s].status == "live" for s in SEAT_ORDER):
            self._o1_entry_effect(seat=None)        # no re-check: no window of its own

    # ------------------------------------------------------------------
    # Seats
    # ------------------------------------------------------------------

    def _first_open_seat(self) -> str | None:
        for s in SEAT_ORDER:
            if self.state.seats[s].status == "open":
                return s
        return None

    def _seat_of_conn(self, conn: object) -> str | None:
        for s in SEAT_ORDER:
            if self.state.seats[s].conn is conn and self.state.seats[s].status == "live":
                return s
        return None

    def seat_of(self, conn: object) -> str | None:
        """Public: which seat this connection currently holds, if any."""
        return self._seat_of_conn(conn)

    def _bind_seat(self, seat: str, conn: object, *, keep_token: bool = False) -> None:
        rec = self.state.seats[seat]
        token = rec.token if (keep_token and rec.token) else secrets.token_urlsafe(24)
        self.state.seats[seat] = SeatRecord(
            conn=conn, token=token, status="live", grace_deadline=None, since=_now_iso(),
        )
        self._cancel_grace_timer(seat)

    def _cancel_grace_timer(self, seat: str) -> None:
        task = self._grace_tasks.pop(seat, None)
        if task is not None:
            try:
                running = asyncio.current_task()
            except RuntimeError:
                running = None
            if task is not running:
                task.cancel()

    # ------------------------------------------------------------------
    # `hello` (§4.6.1) -- O-1, O-10, O-13
    # ------------------------------------------------------------------

    def _resolve_hello(self, token: str | None) -> tuple[str, str | None]:
        """§4.6.1's resolution order, as a PURE read. Returns (step, seat)."""
        st = self.state
        if token:
            for s in SEAT_ORDER:                             # step 2
                if st.seats[s].token == token and st.seats[s].status == "grace":
                    return ("resume", s)
            for s in SEAT_ORDER:                             # step 3
                if st.seats[s].token == token and st.seats[s].status == "live":
                    return ("rebind", s)
        seat = self._first_open_seat()                       # step 4
        if seat is not None:
            return ("seat", seat)
        if ALLOW_UNTOKENED_GRACE_CLAIM:                      # step 5
            for s in SEAT_ORDER:
                if st.seats[s].status == "grace":
                    return ("claim", s)
        return ("blocked", None)                             # step 6

    def _hello_recheck(self, step: str, seat: str | None) -> bool:
        """(G)'s SCOPE table for the two `hello` rows.

        O-1 (step 4): `phase is lobby` + the resolved seat is STILL `open`.
          The "other seat still live" clause and the flag re-read are folded
          into the effect itself (`_o1_entry_effect` only runs when both seats
          are live after this seating) -- naming them here as an abort
          condition would forbid an ordinary FIRST seating, whose other seat is
          legitimately `open`.
        O-13 (steps 2/3/5): NO phase clause, NO epoch clause -- the seat is
          still in the status the resolving step read it in.
        """
        st = self.state
        if seat is None:
            return False
        rec = st.seats[seat]
        if step == "seat":
            return st.phase is SessionPhase.LOBBY and rec.status == "open"
        if step in ("resume", "claim"):
            return rec.status == "grace"
        if step == "rebind":
            return rec.status == "live"
        return False

    async def handle_hello(self, conn: object, msg: HelloMessage) -> None:
        """§4.6.1 steps 1-6. ONE hook await, then one effect list."""
        st = self.state

        try:
            check_hello_protocol(msg)                        # step 1 / order step 3
        except ProtocolError as exc:
            self._queue(conn, construct_error(exc.code, exc.message, None, st.state_seq))
            self._queue_close(conn, CloseCode.PROTOCOL_MISMATCH, "protocol mismatch")
            await self._flush()
            return

        step, seat = self._resolve_hello(msg.resume_token)
        if step == "blocked":                                # O-10: nothing mutates
            self._queue(conn, construct_blocked(
                reason="game_in_progress",
                seats_occupied=sum(1 for s in SEAT_ORDER if st.seats[s].status != "open"),
                session_started_at=self._started_at,
            ))
            self._queue_close(conn, CloseCode.BLOCKED, "no seat available")
            await self._flush()
            return

        await self._pre_apply_hook()                          # THE window
        # --- effect list. NO await below this line. ---
        if not self._hello_recheck(step, seat):
            # ENTRY-TRANSITION CARVE-OUT: never a bare return. Re-run §4.6.1's
            # resolution against POST-await state, inside this same effect
            # list, awaiting no second hook -- so it cannot loop.
            step, seat = self._resolve_hello(msg.resume_token)
            if step == "blocked":
                self._queue(conn, construct_blocked(
                    reason="game_in_progress",
                    seats_occupied=sum(1 for s in SEAT_ORDER if st.seats[s].status != "open"),
                    session_started_at=self._started_at,
                ))
                self._queue_close(conn, CloseCode.BLOCKED, "no seat available")
                await self._flush()
                return

        assert seat is not None
        resumed = step in ("resume", "rebind", "claim")
        older_conn = st.seats[seat].conn if step == "rebind" else None

        self._bind_seat(seat, conn, keep_token=(step in ("resume", "rebind")))
        st.last_event = Event("seat_resumed" if resumed else "seat_joined", seat, {})

        if older_conn is not None and older_conn is not conn:
            self._queue_close(older_conn, CloseCode.SEAT_REBOUND, "opened elsewhere")

        self._queue(conn, construct_seat_assigned(
            seat=seat,
            player=int(SEAT_PLAYER[seat]),
            resume_token=st.seats[seat].token or "",
            session_id=st.session_id,
            resumed=resumed,
        ))

        # O-1: the 2nd seat is seated and the session is in `lobby`.
        if (
            st.phase is SessionPhase.LOBBY
            and all(st.seats[s].status == "live" for s in SEAT_ORDER)
        ):
            self._o1_entry_effect(seat=seat)

        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # O-2 / O-3 -- `roll` during `opening_roll`
    # ------------------------------------------------------------------

    async def _handle_opening_roll(self, conn: object, seat: str, msg: RollMessage) -> None:
        st = self.state

        # O-3 (PURE REJECTION): no hook await, no re-check, and no re-check may
        # ever be allowed to suppress this frame (E1, §4.5.2b).
        if st.opening_dice[seat] is not None:
            await self.reject(
                conn, "not_your_turn",
                "You've already rolled -- waiting on your opponent.", msg.cid,
            )
            return

        # --- O-2: the LITERAL TEMPLATE (epoch + phase + own guard re-read) ---
        epoch = st.opening_epoch
        await self._pre_apply_hook()                          # THE window
        if (
            st.opening_epoch != epoch
            or st.phase is not SessionPhase.OPENING_ROLL
            or st.opening_dice[seat] is not None
        ):
            return                                            # ABORT AS A NO-OP, silently
        # --- effect list. NO await below this line. ---
        k = len(st.opening_rolls)                             # derived only NOW
        st.opening_dice[seat] = self._draw(k, seat)
        self._resolve_throw(completing_seat=seat)
        self._bump()                                          # exactly once per transition
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # O-5 -- the deadline fires
    # ------------------------------------------------------------------

    async def _apply_o5(self, epoch: int) -> None:
        st = self.state
        if (
            st.opening_epoch != epoch
            or st.phase is not SessionPhase.OPENING_ROLL
            or not any(st.opening_dice[s] is None for s in SEAT_ORDER)
        ):
            return
        await self._pre_apply_hook()                          # THE window
        if (
            st.opening_epoch != epoch
            or st.phase is not SessionPhase.OPENING_ROLL
            or not any(st.opening_dice[s] is None for s in SEAT_ORDER)
        ):
            return                                            # ABORT AS A NO-OP
        # --- effect list. NO await below this line. ---
        k = len(st.opening_rolls)
        owed = [s for s in SEAT_ORDER if st.opening_dice[s] is None]
        for s in owed:
            st.opening_dice[s] = self._draw(k, s)
        st.opening_timed_out = True
        st.last_event = Event("opening_timeout", None, {"seats": list(owed)})
        self._resolve_throw(completing_seat=None)             # O-6 / O-7 / O-8
        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # O-9 -- grace expiry
    # ------------------------------------------------------------------

    async def connection_lost(self, conn: object) -> None:
        """A seated connection dropped: seat goes `live` -> `grace` and the
        grace timer is armed (§4.2.3's `seat_left` trigger)."""
        seat = self._seat_of_conn(conn)
        if seat is None:
            return
        st = self.state
        await self._pre_apply_hook()
        if self._seat_of_conn(conn) != seat:
            return
        # --- effect list ---
        delay = GRACE_SECONDS                                 # BARE module-global read
        rec = st.seats[seat]
        st.seats[seat] = SeatRecord(
            conn=None, token=rec.token, status="grace",
            grace_deadline=time.monotonic() + delay, since=rec.since,
        )
        st.last_event = Event("seat_left", seat, {})
        try:
            self._grace_tasks[seat] = asyncio.create_task(self._grace_deadline(seat, delay))
        except RuntimeError:
            # No running loop (bare harness): the seat still holds, but nothing
            # will expire it. Surfacing this as a hard failure would kill a
            # healthy connection; the session stays exitable via `hello`.
            self._grace_tasks.pop(seat, None)
        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    async def _grace_deadline(self, seat: str, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._apply_o9(seat)

    async def _apply_o9(self, seat: str) -> None:
        """O-9. Per (G): NO phase re-check (grace expiry is valid from EVERY
        phase) and NO epoch clause; the re-read guard is the seat is still
        `grace` AND its `grace_deadline` has still elapsed."""
        st = self.state
        rec = st.seats[seat]
        if rec.status != "grace":
            return
        await self._pre_apply_hook()                          # THE window
        rec = st.seats[seat]
        if rec.status != "grace" or rec.grace_deadline is None:
            return                                            # they came back
        if rec.grace_deadline > time.monotonic():
            return
        # --- effect list. NO await below this line. ---
        self._grace_tasks.pop(seat, None)
        self._abandoned_recovery(reason="grace_expired")
        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # The ordinary turn loop (§4.6)
    # ------------------------------------------------------------------

    def _finish_if_over(self) -> None:
        """§4.6: `series[winner] += 1` IN THIS TRANSITION, at game end.

        Freeze item 13: at GAME END, never at rematch, and once per `game_id`.
        """
        game = self.state.game
        if game is None or game.result is None:
            return
        if self._series_counted_for == self.state.game_id:
            return
        self._series_counted_for = self.state.game_id
        winner = PLAYER_SEAT[game.result.winner]
        self.state.series[winner] += 1
        self.state.last_event = Event("game_finished", winner, {
            "type": game.result.outcome.value, "turns": game.result.turns,
        })

    async def _handle_game_message(self, conn: object, seat: str, msg: Any) -> None:
        """`roll` (in-game) / `move` / `undo` / `end_turn`.

        The post-await re-check here names ONLY pre-effect state (the CAS
        token and the engine phase), per (G)'s generalisation 3.
        """
        st = self.state
        assert st.game is not None
        seq = st.state_seq
        phase_before = st.game.phase

        await self._pre_apply_hook()                          # THE window
        game = st.game
        if game is None or st.phase is not None or st.state_seq != seq or game.phase is not phase_before:
            return                                            # ABORT AS A NO-OP
        # --- effect list. NO await below this line. ---
        original = game                                       # for the rollback below
        frames_len = len(st.frames)
        try:
            if isinstance(msg, RollMessage):
                game = roll_dice(game)
                st.game = game
                if must_forfeit(game):
                    st.game = engine_end_turn(game)
                    st.last_event = Event("turn_forfeited", seat, {"reason": "no_legal_move"})
                    self._record_frame("forfeit", seat, None)
                else:
                    st.last_event = Event("dice_rolled", seat, {"dice": list(game.dice_rolled)})
                    self._record_frame("roll", seat, None)
            elif isinstance(msg, MoveMessage):
                move = move_from_wire({"from": msg.from_, "die": msg.die}, game)
                res = apply_move(game, move)
                st.game = res.state
                st.last_event = Event("move_applied", seat, {
                    "from": msg.from_, "die": msg.die, "hit": res.hit,
                })
                self._record_frame("move", seat, move)
                self._finish_if_over()
            elif isinstance(msg, UndoMessage):
                st.game = undo_last_move(game)
                st.last_event = Event("move_undone", seat, {})
                self._record_frame("undo", seat, None)
            elif isinstance(msg, EndTurnMessage):
                st.game = engine_end_turn(game)
                st.last_event = Event("turn_ended", seat, {})
                self._record_frame("end_turn", seat, None)
            else:                                             # pragma: no cover
                raise AssertionError(f"unroutable message {msg!r}")
        except EngineError as exc:
            # Nothing is committed by a rejected message: every engine call
            # here is pure, so restoring the pre-transition GameState (and any
            # frame appended before the raise) is a complete rollback.
            st.game = original
            del st.frames[frames_len:]
            del self._frame_meta[frames_len:]
            code = map_engine_error(exc)
            if code is None:                                  # OpeningUnresolved
                self._apply_o11(exc)
                self._bump()
                self._queue_broadcast()
                self._assert_invariant()
                await self._flush()
                return
            await self.reject(conn, code, str(exc) or code, getattr(msg, "cid", None))
            return

        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # `rematch` -- O-12, and the Rev 5-final-4 DECLINE RESET
    # ------------------------------------------------------------------

    async def _handle_rematch(self, conn: object, seat: str, msg: RematchMessage) -> None:
        st = self.state
        other = "black" if seat == "white" else "white"

        if not msg.want:
            # --- THE DECLINE (§4.6's diagram edge; Rev 5-final-4). Not an
            # O-row, and deliberately not put in §4.6.0's table.
            game_id_before = st.game_id
            await self._pre_apply_hook()                      # THE window
            if st.game_id != game_id_before or st.game is None or st.phase is not None:
                return
            if st.game.phase is not EnginePhase.GAME_OVER:
                return
            # --- effect list ---
            self._cancel_grace_timer(seat)
            st.seats[seat] = SeatRecord()                     # status -> open
            st.rematch = {s: False for s in SEAT_ORDER}
            st.game_id = self._next_game_id()
            st.dice = self._dice_factory()                    # RE-MINT
            st.game = None
            st.phase = SessionPhase.LOBBY
            # 🔴 THE DECLINE RESET (Baxter 2026-08-04 14:33): whoever next
            # takes the reopened seat starts a FRESH FIRST MATCH.
            st.opening_interactive_available = True
            st.last_event = Event("rematch_started", seat, {"want": False})
            self._reset_frames()
            self._bump()
            frame = self._queue_broadcast()
            # Courtesy unicast to the just-released connection so its view
            # updates; it is a re-send, not a mutation, and bumps nothing.
            if conn not in self._seated_conns():
                self._queue(conn, frame)
            self._assert_invariant()
            await self._flush()
            return

        # --- want == True ------------------------------------------------
        other_flag_before = st.rematch[other]
        game_id_before = st.game_id
        await self._pre_apply_hook()                          # THE window
        # (G)'s O-12 re-check: `phase is game_over` + `game_id` unchanged + the
        # OTHER seat has not WITHDRAWN a vote it had already cast. The SENDER's
        # own flag is written by this effect list and therefore cannot be a
        # precondition.
        if st.game is None or st.phase is not None or st.game.phase is not EnginePhase.GAME_OVER:
            return
        if st.game_id != game_id_before:
            return
        # 🔴 EQUALITY IS NOT TRUTH. This clause used to read
        # `st.rematch[other] != other_flag_before`, which aborts on ANY change
        # -- including a FAVOURABLE one. When the other seat's flag flips
        # False -> True inside our await window (the textbook simultaneous-
        # accept case §7.8's "Simultaneous rematch" row exists to prove), that
        # read as an invalidation and this transition returned as a silent
        # no-op: no error, no state, our own `rematch[seat] = True` never
        # written, and BOTH players stranded on a pressed Rematch button --
        # the exact failure freeze item 15 records as fixed for O-12, reached
        # again through equality-vs-truth. Only a genuine WITHDRAWAL
        # (True -> False) invalidates this effect list; the one route that
        # produces it -- a decline -- also bumps `game_id`, which the clause
        # above already catches.
        if other_flag_before and not st.rematch[other]:
            return
        # --- effect list. NO await below this line. ---
        st.rematch[seat] = True
        if st.rematch[other]:
            # --- O-12: SERVER-RESOLVED. No opening_roll, no deadline, no
            # opening field, no epoch bump.
            st.game_id = self._next_game_id()
            self._reset_frames()
            st.rematch = {s: False for s in SEAT_ORDER}
            st.dice = self._dice_factory()                    # RE-MINT
            st.opening_interactive_available = False          # explicit, not order-dependent
            self._server_resolved_start()                     # EngineError -> O-14
        else:
            st.last_event = Event("rematch_started", seat, {"want": True})
        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    async def _handle_replay_request(self, conn: object, seat: str, msg: ReplayRequestMessage) -> None:
        st = self.state
        assert st.game is not None
        frames = [
            frame_to_wire(state, i, kind, fseat, move)
            for i, (state, (kind, fseat, move)) in enumerate(zip(st.frames, self._frame_meta))
        ]
        self._queue(conn, construct_replay(game_id=st.game_id, frames=frames))
        # §4.2.3 lists `replay_request` among the accepted messages that
        # broadcast and bump. It mutates nothing else.
        self._bump()
        self._queue_broadcast()
        self._assert_invariant()
        await self._flush()

    # ------------------------------------------------------------------
    # THE REJECTION ORDER (§4.5.3). 5C owns step 4 and step 1's 1008 counter.
    # ------------------------------------------------------------------

    def _phase_valid(self, msg: Any) -> bool:
        """Step 6. `roll` is valid in EXACTLY TWO phases, dispatched on
        `session.phase is SessionPhase.OPENING_ROLL` and never on frame
        content (freeze item 8 / E3)."""
        st = self.state
        if isinstance(msg, RollMessage):
            if st.phase is SessionPhase.OPENING_ROLL:
                return True
            return st.phase is None and st.game is not None and (
                st.game.phase is EnginePhase.AWAITING_ROLL
            )
        if st.phase is not None or st.game is None:
            return False                                      # lobby/opening/abandoned
        gp = st.game.phase
        if isinstance(msg, MoveMessage):
            return gp is EnginePhase.AWAITING_MOVE
        if isinstance(msg, (UndoMessage, EndTurnMessage)):
            return gp in (EnginePhase.AWAITING_MOVE, EnginePhase.AWAITING_COMMIT)
        if isinstance(msg, (RematchMessage, ReplayRequestMessage)):
            return gp is EnginePhase.GAME_OVER
        return False

    async def handle_message(self, conn: object, payload: Any) -> None:
        """Rejection-order steps 1, 2, 3, 5, 6, 7, 8 (§4.5.3), then dispatch.

        Step 4 (token bucket) and step 1's three-strikes `1008` close are 5C's
        (§9.2) and wrap this call; `reject()` is the seam for both.
        """
        st = self.state

        # --- step 1: shape ------------------------------------------------
        try:
            msg = parse_client_message(payload)
        except ProtocolError as exc:
            await self.reject(conn, exc.code, exc.message, extract_cid(payload))
            return

        if isinstance(msg, HelloMessage):                     # steps 3 + §4.6.1
            await self.handle_hello(conn, msg)
            return

        # --- step 2: seated ------------------------------------------------
        seat = self._seat_of_conn(conn)
        if seat is None:
            await self.reject(conn, "not_seated", "this connection holds no seat", msg.cid)
            return

        # --- step 5: seat check, with the three-way `roll` split ------------
        if isinstance(msg, RollMessage):
            if st.phase is SessionPhase.OPENING_ROLL:
                pass                                          # E1: handled by O-2/O-3
            elif st.phase is None and st.game is not None:
                if PLAYER_SEAT[st.game.turn] != seat:
                    await self.reject(conn, "not_your_turn", "it is not your turn", msg.cid)
                    return
            # LOBBY / ABANDONED: step 5 is a NO-OP for `roll` -- it falls
            # through to step 6 and is rejected `wrong_phase`.
        elif isinstance(msg, (MoveMessage, UndoMessage, EndTurnMessage)):
            if st.phase is None and st.game is not None and PLAYER_SEAT[st.game.turn] != seat:
                await self.reject(conn, "not_your_turn", "it is not your turn", msg.cid)
                return
        # `rematch` / `replay_request` are NOT seat-checked (§4.5.2a).

        # --- step 6: phase validity ----------------------------------------
        if not self._phase_valid(msg):
            await self.reject(conn, "wrong_phase", "not valid in this phase", msg.cid)
            return

        # --- step 7: CAS ---------------------------------------------------
        if isinstance(msg, (MoveMessage, UndoMessage, EndTurnMessage)) or (
            isinstance(msg, RollMessage) and st.phase is not SessionPhase.OPENING_ROLL
        ):
            # E2: `roll` skips CAS during `opening_roll` -- only the CHECK is
            # skipped, never the increment (§4.5.2b).
            if msg.state_seq != st.state_seq:
                await self.reject(conn, "stale_state", "state has moved on", msg.cid)
                return

        # --- dispatch (step 8's engine errors are mapped inside) ------------
        if isinstance(msg, RollMessage) and st.phase is SessionPhase.OPENING_ROLL:
            await self._handle_opening_roll(conn, seat, msg)
        elif isinstance(msg, RematchMessage):
            await self._handle_rematch(conn, seat, msg)
        elif isinstance(msg, ReplayRequestMessage):
            await self._handle_replay_request(conn, seat, msg)
        else:
            await self._handle_game_message(conn, seat, msg)


__all__ = [
    "Event",
    "GameSession",
    "SeatRecord",
    "SessionState",
    "Seat",
    "SEAT_ORDER",
    "SEAT_PLAYER",
    "PLAYER_SEAT",
    "GRACE_SECONDS",
    "ALLOW_UNTOKENED_GRACE_CLAIM",
    "OPENING_ROLL_DEADLINE_S",
]
