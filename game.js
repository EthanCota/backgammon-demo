"use strict";
console.log("[backgammon] build 20260817-071052-0089d4a");

// ============================================================================
// SYNC HOST — single source of truth for the WebSocket endpoint.
// Wave 8 will replace this placeholder with the provisioned host's wss:// URL.
// Do NOT hardcode a URL anywhere else in this file.
// wss:// only — the page is served over HTTPS and a browser hard-blocks
// ws:// from an HTTPS page as mixed content.
// ============================================================================
const SYNC_HOST_WS_URL = "wss://REPLACE_ME_SYNC_HOST.example.invalid/ws";

// §4.4 item 1: ?ws= query string wins, falling back to the build-time constant.
function resolveWsUrl() {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("ws");
  const url = fromQuery || window.BACKGAMMON_WS || SYNC_HOST_WS_URL;
  if (!/^wss:\/\//.test(url)) {
    console.error("[backgammon] refusing non-wss:// URL:", url);
  }
  return url;
}

const PROTOCOL = 1;
const RESUME_TOKEN_KEY = "backgammon_resume_token";

// §4.2.4 / §4.4 item 5 — backoff schedule, cap 8s, jitter +/-20%.
const BACKOFF_SCHEDULE_S = [0.5, 1, 2, 4, 8];
function backoffDelayMs(attempt) {
  const base = BACKOFF_SCHEDULE_S[Math.min(attempt, BACKOFF_SCHEDULE_S.length - 1)];
  const jitter = base * 0.2 * (Math.random() * 2 - 1);
  return Math.max(50, (base + jitter) * 1000);
}

// ---------------------------------------------------------------------------
// Clock — injectable so the L4 "waking the table" test does not wait 90s.
// ---------------------------------------------------------------------------
const Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (id) => window.clearTimeout(id),
  setInterval: (fn, ms) => window.setInterval(fn, ms),
  clearInterval: (id) => window.clearInterval(id),
};
window.__BACKGAMMON_CLOCK__ = Clock; // test hook: replace before connect() to inject a fake clock

// All internal call sites read the clock through this indirection (never the
// `Clock` const directly) so that replacing `window.__BACKGAMMON_CLOCK__`
// actually takes effect, including for timers already in flight.
function getClock() {
  return window.__BACKGAMMON_CLOCK__ || Clock;
}

// ---------------------------------------------------------------------------
// Connection state
// ---------------------------------------------------------------------------
const Conn = {
  ws: null,
  firstAttemptAt: null,
  everConnected: false,
  reconnectAttempt: 0,
  wakingTimer: null,
  giveUpTimer: null,
  statusTicker: null,
  seat: null,
  session_id: null,
  resume_token: localStorage.getItem(RESUME_TOKEN_KEY) || null,
  highestSeqRendered: -1,
  lastState: null,
  cidCounter: 0,
  pendingClock: { deadline_remaining_s: null, grace_remaining_s: null, at: 0 },
  noAutoRetry: false, // set true on 1000/1008/4000/4003; a manual click can still call connect()
};

function nextCid() {
  Conn.cidCounter += 1;
  return "c" + Conn.cidCounter.toString(36) + "_" + Math.random().toString(36).slice(2, 6);
}

function setStatus(status) {
  document.body.setAttribute("data-status", status);
}

function setConnBanner(copy, showRetry) {
  const el = document.querySelector("[data-conn-copy]");
  if (el) el.textContent = copy;
  const btn = document.querySelector("[data-action='retry']");
  if (btn) btn.hidden = !showRetry;
}

// ---------------------------------------------------------------------------
// §4.4 item 5 — the "waking the table..." affordance, with a bound.
// ---------------------------------------------------------------------------
function armWakingAffordance() {
  clearWakingTimers();
  setStatus("connecting");
  setConnBanner("Connecting…", false);

  Conn.wakingTimer = getClock().setTimeout(() => {
    setStatus("waking");
    setConnBanner("The table has been idle — waking it up can take up to a minute.", false);
  }, 10 * 1000);

  Conn.giveUpTimer = getClock().setTimeout(() => {
    setStatus("give_up");
    setConnBanner("Couldn't reach the table.", true);
  }, 90 * 1000);
}

function clearWakingTimers() {
  if (Conn.wakingTimer != null) { getClock().clearTimeout(Conn.wakingTimer); Conn.wakingTimer = null; }
  if (Conn.giveUpTimer != null) { getClock().clearTimeout(Conn.giveUpTimer); Conn.giveUpTimer = null; }
}

// ---------------------------------------------------------------------------
// Connection lifecycle
// ---------------------------------------------------------------------------
function connect() {
  Conn.noAutoRetry = false;
  if (Conn.firstAttemptAt == null) {
    Conn.firstAttemptAt = getClock().now();
    armWakingAffordance();
  }

  const url = resolveWsUrl();
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  Conn.ws = ws;

  ws.addEventListener("open", () => {
    Conn.everConnected = true;
    clearWakingTimers();
    Conn.reconnectAttempt = 0;
    sendHello();
  });

  ws.addEventListener("message", (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch (e) {
      return; // malformed frames are the server's problem to police; ignore locally
    }
    handleMessage(msg);
  });

  ws.addEventListener("close", (evt) => {
    handleClose(evt.code, evt.reason);
  });

  ws.addEventListener("error", () => {
    // close will follow; nothing to do here
  });
}

function sendHello() {
  send({ t: "hello", protocol: PROTOCOL, resume_token: Conn.resume_token });
}

function send(obj) {
  if (Conn.ws && Conn.ws.readyState === WebSocket.OPEN) {
    Conn.ws.send(JSON.stringify(obj));
  }
}

// §4.2.4 close-code table, driving reconnect behavior.
function handleClose(code, reason) {
  clearWakingTimers();
  switch (code) {
    case 1000:
      Conn.noAutoRetry = true;
      setStatus("closed_no_retry");
      setConnBanner("Disconnected.", false);
      return;
    case 1008:
      Conn.noAutoRetry = true;
      setStatus("closed_no_retry");
      setConnBanner("Connection closed after repeated bad messages.", false);
      return;
    case 4000:
      Conn.noAutoRetry = true;
      setStatus("closed_no_retry");
      setConnBanner("This game was opened elsewhere.", false);
      return;
    case 4001: // protocol_mismatch's dedicated close, but 'error' already triggers the reload too
      window.location.reload(true);
      return;
    case 4003:
      Conn.noAutoRetry = true;
      setStatus("closed_no_retry");
      setConnBanner("This table already has two players.", false);
      return;
    case 4004:
      Conn.noAutoRetry = true;
      setStatus("offer_rejoin");
      setConnBanner("Your seat was released after being away too long.", true);
      return;
    case 1006:
    case 1011:
    default:
      scheduleReconnect();
      return;
  }
}

function scheduleReconnect() {
  if (Conn.noAutoRetry) return;

  // Before the first successful connection: honor the waking/give-up bound.
  if (!Conn.everConnected) {
    const elapsed = getClock().now() - (Conn.firstAttemptAt || getClock().now());
    if (elapsed >= 90 * 1000) {
      setStatus("give_up");
      setConnBanner("Couldn't reach the table.", true);
      return; // stop retrying past give_up, per §4.4 item 5
    }
  }

  const delay = backoffDelayMs(Conn.reconnectAttempt);
  Conn.reconnectAttempt += 1;
  getClock().setTimeout(connect, delay);
}

// Manual retry button (give_up / offer_rejoin states)
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.querySelector("[data-action='retry']");
  if (btn) {
    btn.addEventListener("click", () => {
      Conn.firstAttemptAt = null;
      Conn.reconnectAttempt = 0;
      connect();
    });
  }
  wireBoardClicks();
  wireControlClicks();
  connect();
});

// ---------------------------------------------------------------------------
// Message handling
// ---------------------------------------------------------------------------
function handleMessage(msg) {
  switch (msg.t) {
    case "seat_assigned":
      Conn.seat = msg.seat;
      Conn.session_id = msg.session_id;
      Conn.resume_token = msg.resume_token;
      localStorage.setItem(RESUME_TOKEN_KEY, msg.resume_token);
      renderSeatBadge();
      break;
    case "blocked":
      setStatus("closed_no_retry");
      setConnBanner("This table already has two players.", false);
      break;
    case "state":
      Conn.pendingClock = {
        deadline_remaining_s: msg.opening ? msg.opening.deadline_remaining_s : null,
        grace_remaining_s: extractGraceRemaining(msg),
        at: getClock().now(),
      };
      if (msg.state_seq > Conn.highestSeqRendered) {
        Conn.highestSeqRendered = msg.state_seq;
      }
      Conn.lastState = msg;
      render(msg);
      break;
    case "error":
      handleError(msg);
      break;
    case "replay":
      renderReplay(msg);
      break;
    default:
      break;
  }
}

function extractGraceRemaining(state) {
  if (!state.players || !Conn.seat) return null;
  const opp = Conn.seat === "white" ? "black" : "white";
  const p = state.players[opp];
  return p ? p.grace_remaining_s : null;
}

// §4.5.4 — the ten error codes and their client behavior.
function handleError(msg) {
  switch (msg.code) {
    case "protocol_mismatch":
      window.location.reload(true); // GitHub Pages caches; a stale client can outlive a redeploy
      return;
    case "malformed":
    case "not_seated":
    case "rate_limited":
    case "not_your_turn":
    case "wrong_phase":
    case "stale_state":
    case "illegal_move":
    case "not_a_prefix":
    case "nothing_to_undo":
      clearPendingIntent();
      showTransientError(msg);
      // "Always followed immediately by an unsolicited fresh `state`" — nothing else to fetch.
      return;
    default:
      return;
  }
}

function clearPendingIntent() {
  // ⚠️ Selector is deliberately NOT scoped to `.point`. The bear-off
  // destination highlight lands on `.off-stack` (`[data-off-color]`), which is
  // not a `.point` — a `.point.legal-*` selector would add that class and then
  // never remove it, leaving a permanent phantom highlight on the off-stack.
  // Match on the CLASSES, which are only ever set by `onPointClick`, so any
  // future non-`.point` highlight target is cleared for free.
  document.querySelectorAll(".legal-source, .legal-dest").forEach((el) => {
    el.classList.remove("legal-source", "legal-dest");
  });
  Selection.from = null;
}

function showTransientError(msg) {
  const log = document.querySelector("[data-last-event]");
  if (log) log.textContent = msg.message || msg.code;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderSeatBadge() {
  const el = document.querySelector("[data-seat]");
  if (el) el.textContent = Conn.seat ? ("You are " + Conn.seat) : "";
}

function render(state) {
  renderBoard(state.board);
  renderDice(state);
  renderSeries(state.series);
  renderOpening(state);
  renderTurnInfo(state);
  renderTurnIndicator(state);
  renderControls(state);
  renderLastEvent(state.last_event);

  // Once a real `state` frame has arrived we are past the wake affordance entirely,
  // unless the opening-roll affordance (below) or a normal-phase status applies.
  if (!["opening_roll"].includes(state.phase)) {
    if (document.body.getAttribute("data-status") === "connecting" ||
        document.body.getAttribute("data-status") === "waking" ||
        document.body.getAttribute("data-status") === "give_up") {
      setStatus(state.phase);
      setConnBanner("", false);
    } else if (!["connecting", "waking", "give_up", "closed_no_retry", "offer_rejoin",
                 "opening_my_roll", "opening_waiting", "opening_tie"].includes(
                   document.body.getAttribute("data-status"))) {
      setStatus(state.phase);
    }
  }
}

function renderBoard(board) {
  document.querySelectorAll(".point").forEach((el) => {
    el.innerHTML = "";
  });
  for (let p = 1; p <= 24; p++) {
    const n = board.points[p];
    if (!n) continue;
    const color = n > 0 ? "white" : "black";
    const count = Math.abs(n);
    const pointEl = document.querySelector('[data-point="' + p + '"]');
    if (!pointEl) continue;
    for (let i = 0; i < count; i++) {
      const checker = document.createElement("div");
      checker.className = "checker";
      checker.setAttribute("data-color", color);
      pointEl.appendChild(checker);
    }
  }
  ["white", "black"].forEach((color) => {
    const barEl = document.querySelector('[data-bar-color="' + color + '"]');
    if (barEl) {
      barEl.innerHTML = "";
      const n = board.bar[color] || 0;
      for (let i = 0; i < n; i++) {
        const checker = document.createElement("div");
        checker.className = "checker";
        checker.setAttribute("data-color", color);
        barEl.appendChild(checker);
      }
    }
    const offEl = document.querySelector('[data-off-color="' + color + '"]');
    if (offEl) {
      offEl.innerHTML = "";
      const n = board.off[color] || 0;
      const label = document.createElement("span");
      label.className = "count";
      label.textContent = n + " off";
      offEl.appendChild(label);
    }
  });
}

// Item 2: real dice faces (pip dots), not numerals.
// Standard 1-9 (row-major, 3x3) pip layouts for values 1-6.
const DIE_PIP_LAYOUT = {
  1: [5],
  2: [1, 9],
  3: [1, 5, 9],
  4: [1, 3, 7, 9],
  5: [1, 3, 5, 7, 9],
  6: [1, 3, 4, 6, 7, 9],
};

function buildDieFace(value) {
  const face = document.createElement("div");
  face.className = "die-face";
  const onCells = new Set(DIE_PIP_LAYOUT[value] || []);
  for (let i = 1; i <= 9; i++) {
    const pip = document.createElement("span");
    pip.className = "pip" + (onCells.has(i) ? " pip-on" : "");
    face.appendChild(pip);
  }
  return face;
}

// Item 3: dice colored to match the player who rolled. `active_seat` is the
// seat whose turn it currently is -- during normal play that is also the
// seat whose roll is on display (the dice belong to whoever is mid-turn).
// The wire's dice payload carries no per-die seat tag (see wire.py), so
// outside a live turn (opening roll / lobby / game over, where active_seat
// is null) the dice render with the neutral/default face rather than
// guessing an owner.
function renderDice(state) {
  const el = document.querySelector("[data-dice]");
  if (!el) return;
  el.innerHTML = "";
  const remaining = state.dice.remaining.slice();
  const rollerColor = state.active_seat === "white" || state.active_seat === "black"
    ? state.active_seat
    : null;
  (state.dice.rolled || []).forEach((d) => {
    const die = document.createElement("div");
    die.className = "die";
    if (rollerColor) die.setAttribute("data-color", rollerColor);
    die.appendChild(buildDieFace(d));
    const idx = remaining.indexOf(d);
    const used = idx === -1;
    if (!used) remaining.splice(idx, 1);
    die.setAttribute("data-used", used ? "true" : "false");
    el.appendChild(die);
  });
}

function renderSeries(series) {
  if (!series) return;
  const w = document.querySelector('[data-series="white"]');
  const b = document.querySelector('[data-series="black"]');
  if (w) w.textContent = String(series.white);
  if (b) b.textContent = String(series.black);
}

// §4.4 item 6 — opening-roll affordance, driven purely by opening.awaiting.
function renderOpening(state) {
  const wrap = document.querySelector("[data-opening]");
  const copyEl = document.querySelector("[data-opening-copy]");
  if (!wrap || !copyEl) return;

  if (state.phase !== "opening_roll" || !state.opening) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const opening = state.opening;
  const mySeat = Conn.seat;
  const awaiting = opening.awaiting || [];

  if (state.last_event && state.last_event.kind === "opening_tie") {
    setStatus("opening_tie");
    const die = state.last_event.detail ? state.last_event.detail.die : "?";
    copyEl.textContent = "Tied on " + die + " — roll again.";
  } else if (mySeat && awaiting.includes(mySeat)) {
    setStatus("opening_my_roll");
    copyEl.textContent = "Roll for first move";
  } else if (awaiting.length > 0) {
    setStatus("opening_waiting");
    const secs = opening.deadline_remaining_s != null ? tickedDeadline() : null;
    copyEl.textContent = "Waiting for your opponent to roll…" +
      (secs != null ? " (" + Math.max(0, Math.ceil(secs)) + "s)" : "");
  }
}

function tickedDeadline() {
  if (Conn.pendingClock.deadline_remaining_s == null) return null;
  const elapsedS = (getClock().now() - Conn.pendingClock.at) / 1000;
  return Conn.pendingClock.deadline_remaining_s - elapsedS;
}

function renderTurnInfo(state) {
  const el = document.querySelector("[data-turn-copy]");
  if (!el) return;
  if (state.phase === "lobby") {
    el.textContent = "Waiting for a second player…";
  } else if (state.phase === "abandoned") {
    el.textContent = "The previous game was abandoned. Waiting for players…";
  } else if (state.phase === "game_over" && state.outcome) {
    el.textContent = state.outcome.winner + " wins (" + state.outcome.type + ")";
  } else if (state.active_seat) {
    el.textContent = state.active_seat === Conn.seat
      ? "Your turn (" + state.phase + ")"
      : "Opponent's turn (" + state.phase + ")";
  } else {
    el.textContent = "";
  }
}

// Item 3: a clear top-of-board indicator of whose turn it is (black vs white).
function renderTurnIndicator(state) {
  const wrap = document.querySelector("[data-turn-indicator]");
  const dot = document.querySelector("[data-turn-indicator-dot]");
  const copy = document.querySelector("[data-turn-indicator-copy]");
  if (!wrap || !dot || !copy) return;

  let seat = null;
  if (state.active_seat === "white" || state.active_seat === "black") {
    seat = state.active_seat;
  } else if (state.phase === "opening_roll" && state.opening) {
    const awaiting = state.opening.awaiting || [];
    if (awaiting.length === 1) seat = awaiting[0];
  }

  if (!seat) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  dot.setAttribute("data-color", seat);
  const label = seat === "white" ? "White" : "Black";
  copy.textContent = seat === Conn.seat ? label + " to move (you)" : label + " to move";
}

// Item 4: adaptive buttons. While waiting on the opponent there is nothing
// legal to click, so dead buttons are hidden (not merely disabled) and a
// waiting hint takes their place -- rather than requiring the player to
// figure out which of four buttons happens to be the live one.
function renderControls(state) {
  const rollBtn = document.querySelector('[data-action="roll"]');
  const endBtn = document.querySelector('[data-action="end-turn"]');
  const undoBtn = document.querySelector('[data-action="undo"]');
  const rematchBtn = document.querySelector('[data-action="rematch"]');
  const waitingHint = document.querySelector("[data-waiting-hint]");

  let canRoll = false;
  if (state.phase === "opening_roll") {
    const awaiting = state.opening ? state.opening.awaiting || [] : [];
    canRoll = !!(Conn.seat && awaiting.includes(Conn.seat));
  } else {
    canRoll = state.phase === "awaiting_roll" && state.active_seat === Conn.seat;
  }
  const canEndTurn = !!state.can_end_turn;
  const canUndo = !!state.can_undo;
  const canRematch = state.phase === "game_over";

  if (rollBtn) { rollBtn.disabled = !canRoll; rollBtn.hidden = !canRoll; }
  if (endBtn) { endBtn.disabled = !canEndTurn; endBtn.hidden = !canEndTurn; }
  if (undoBtn) { undoBtn.disabled = !canUndo; undoBtn.hidden = !canUndo; }
  if (rematchBtn) { rematchBtn.disabled = !canRematch; rematchBtn.hidden = !canRematch; }

  // The waiting hint fills the gap only during active gameplay where it's
  // genuinely someone else's move -- not in lobby/opening/game_over, which
  // already have their own copy elsewhere (opening-area / turn-info).
  const isMidGameWaiting = !canRoll && !canEndTurn && !canUndo &&
    state.active_seat && state.active_seat !== Conn.seat &&
    state.phase !== "opening_roll" && state.phase !== "game_over";
  if (waitingHint) waitingHint.hidden = !isMidGameWaiting;
}

// §4.4 item 6 — `opening_timeout` is reported on the `awaiting_move` frame that
// resolves it (server merges the timeout and the game start into one transition,
// one broadcast). Non-blocking, exactly like the generic `turn_forfeited` notice
// below: this just sets text, it never gates input and dismissing it (the next
// frame overwriting it) sends nothing to the server.
function renderLastEvent(evt) {
  const el = document.querySelector("[data-last-event]");
  if (!el) return;
  if (!evt) { el.textContent = ""; return; }
  if (evt.kind === "opening_timeout") {
    const seats = (evt.detail && evt.detail.seats) || [];
    el.textContent = "Rolled for you." + (seats.length ? " (" + seats.join(", ") + ")" : "");
    return;
  }
  el.textContent = evt.kind + (evt.seat ? " (" + evt.seat + ")" : "");
}

function renderReplay(msg) {
  // Frames are wire-serialized positions; the client never applies anything, only displays.
  window.__BACKGAMMON_LAST_REPLAY__ = msg;
}

// ---------------------------------------------------------------------------
// Input — the client sends intents only; it computes no legality itself.
// ---------------------------------------------------------------------------
const Selection = { from: null };

function wireBoardClicks() {
  document.querySelectorAll(".point").forEach((el) => {
    el.addEventListener("click", () => onPointClick(el.getAttribute("data-point")));
  });
  document.querySelectorAll("[data-bar-color]").forEach((el) => {
    el.addEventListener("click", () => onPointClick("bar"));
  });
  document.querySelectorAll("[data-off-color]").forEach((el) => {
    el.addEventListener("click", () => onPointClick("off"));
  });
}

function onPointClick(rawFrom) {
  const state = Conn.lastState;
  if (!state) return;
  const legal = state.legal_moves || [];

  // Second click: is this a legal destination for the current selection?
  if (Selection.from != null) {
    const match = legal.find((m) =>
      String(m.from) === String(Selection.from) && String(m.to) === String(rawFrom));
    if (match) {
      sendMove(match.from, match.die);
      clearPendingIntent();
      return;
    }
  }

  // First click (or a re-pick): only meaningful if this point is a legal `from`.
  const from = /^\d+$/.test(rawFrom) ? Number(rawFrom) : rawFrom;
  const candidates = legal.filter((m) => String(m.from) === String(from));
  clearPendingIntent();
  if (candidates.length === 0) return;

  Selection.from = from;
  const sourceEl = from === "bar"
    ? null
    : document.querySelector('[data-point="' + from + '"]');
  if (sourceEl) sourceEl.classList.add("legal-source");
  candidates.forEach((m) => {
    const destEl = destElementFor(m.to, state);
    if (destEl) destEl.classList.add("legal-dest");
  });
}

// The wire's bear-off destination is the STRING "off" (§4.2.1), and no
// element's `data-point` attribute is ever "off" — so a plain
// `[data-point="off"]` lookup silently matches nothing and the off-stack never
// lights up. That is the least discoverable move in the game and the one that
// wins it, so it gets its own resolution rather than a `if (destEl)` shrug.
// (Same shape as `wireBoardClicks`' off-stack special case — one input path,
// not a parallel one.)
function destElementFor(to, state) {
  if (String(to) !== "off") {
    return document.querySelector('[data-point="' + to + '"]');
  }
  // Which off-stack: the MOVING seat's. `legal_moves` is broadcast to both
  // seats (it describes the active seat's options), so keying off `Conn.seat`
  // would highlight the wrong stack on the idle page. `active_seat` is the
  // checker's actual destination; fall back to our own seat only if it is
  // absent.
  const color = (state && state.active_seat) || Conn.seat;
  return color ? document.querySelector('[data-off-color="' + color + '"]') : null;
}

function sendMove(from, die) {
  if (!Conn.lastState) return;
  send({ t: "move", state_seq: Conn.lastState.state_seq, cid: nextCid(), from, die });
}

function wireControlClicks() {
  const rollBtn = document.querySelector('[data-action="roll"]');
  const endBtn = document.querySelector('[data-action="end-turn"]');
  const undoBtn = document.querySelector('[data-action="undo"]');
  const rematchBtn = document.querySelector('[data-action="rematch"]');

  if (rollBtn) rollBtn.addEventListener("click", () => {
    const state = Conn.lastState;
    const cid = nextCid();
    if (state && state.phase === "opening_roll") {
      send({ t: "roll", cid }); // exempt from state_seq/seat-check, §4.5.2b
    } else if (state) {
      send({ t: "roll", state_seq: state.state_seq, cid });
    }
  });
  if (endBtn) endBtn.addEventListener("click", () => {
    if (!Conn.lastState) return;
    send({ t: "end_turn", state_seq: Conn.lastState.state_seq, cid: nextCid() });
  });
  if (undoBtn) undoBtn.addEventListener("click", () => {
    if (!Conn.lastState) return;
    send({ t: "undo", state_seq: Conn.lastState.state_seq, cid: nextCid() });
  });
  if (rematchBtn) rematchBtn.addEventListener("click", () => {
    send({ t: "rematch", cid: nextCid(), want: true }); // CAS-exempt, §4.5.2a
  });
}

// ---------------------------------------------------------------------------
// §4.4 item 2 — window.__GAME__.snapshot()
// ---------------------------------------------------------------------------
window.__GAME__ = {
  snapshot() {
    const s = Conn.lastState;
    if (!s) {
      return {
        state_seq: null, phase: null, active_seat: null, seat: Conn.seat,
        points: null, bar: null, off: null, dice: null, legal_moves: null,
        outcome_type: null, series: null, opening: null,
      };
    }
    return {
      state_seq: s.state_seq,
      phase: s.phase,
      active_seat: s.active_seat,
      seat: Conn.seat,
      points: s.board.points,
      bar: s.board.bar,
      off: s.board.off,
      dice: s.dice,
      legal_moves: s.legal_moves,
      outcome_type: s.outcome ? s.outcome.type : null,
      series: s.series,
      opening: s.opening,
    };
  },
};

// Test hook: allow a fresh connect() call to be triggered externally (e.g. L4 harness).
window.__BACKGAMMON_CONNECT__ = connect;
