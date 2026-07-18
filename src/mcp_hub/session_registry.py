"""Session registry — tracks live MCP sessions bound to agent names.

This module owns the correctness of "which agent has a live wakeable session
right now?" — the load-bearing question for channel push semantics.

The naive implementation (a bare `dict[name, ServerSession]` updated on
register and reduced only on push failure) has two failure modes that bit us
in production:

1. **Zombie bindings.** When a Claude Code session exits, its underlying SSE
   connection may stay warm on the server side (transport-level idle keepalive
   in StreamableHTTPSessionManager). The hub doesn't notice. The dict still
   carries a reference. `list_agents` reports the agent as ⚡ wakeable — a lie.

2. **Silent push loss.** A push to a zombie session may not raise an exception
   on the write side (writes go to a buffer that nobody reads), so the hub
   thinks it succeeded. The recipient never wakes; the message is "delivered"
   only to the persistent inbox.

This module fixes both with two complementary mechanisms:

- **Deterministic disconnect detection.** We monkey-patch
  `BaseSession.__aexit__` (the shared session base class in the MCP SDK) to
  fire registered close handlers when *any* session ends. The patch is
  idempotent and process-global. Each `SessionRegistry` instance subscribes
  itself, so when a session ends the registry drops every name bound to it.
  This is the primary mechanism — it catches normal disconnects within
  microseconds of the connection actually closing.

- **Active liveness check on push.** `SessionRegistry.push()` sends an MCP
  ping with a tight timeout *before* the actual notification. If the ping
  fails or times out (zombie connection that hasn't fired __aexit__ yet),
  we drop the binding and report push failure. This is the safety net for
  cases the lifecycle hook can't catch (e.g., transport-level keepalive
  zombies, network partitions where the underlying socket is dead but the
  server-side state hasn't noticed).

The combination means:
- `is_bound(name)` reflects the truth at second-level granularity (not
  millisecond — pushes do the actual liveness check).
- A push that returns True genuinely reached the recipient (the ping
  proved liveness immediately before the send).
- A push that returns False either had no binding to start with, or proved
  the connection was dead and cleaned up — the binding will not return
  without a fresh `register()` call.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import anyio
from mcp.server.session import ServerSession
from mcp.shared.session import BaseSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-global lifecycle hook
# ---------------------------------------------------------------------------

# Close handlers — called with the closing session as their only argument when
# any BaseSession's __aexit__ runs. Each SessionRegistry registers itself here.
_close_handlers: list[Callable[[BaseSession], None]] = []
_close_handlers_lock = threading.Lock()
_aexit_patched = False
_original_aexit: Callable[..., Any] | None = None


def _ensure_aexit_patched() -> None:
    """Install the close-handler dispatch on `BaseSession.__aexit__`.

    Idempotent: only patches once per process. Safe in multi-server-in-process
    setups because handlers receive the closing session and can filter by
    object identity to ignore sessions they don't own.
    """
    global _aexit_patched, _original_aexit
    if _aexit_patched:
        return

    _original_aexit = BaseSession.__aexit__

    async def patched_aexit(
        self: BaseSession,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool | None:
        try:
            assert _original_aexit is not None
            return await _original_aexit(self, exc_type, exc_val, exc_tb)
        finally:
            # Snapshot under lock so a handler that triggers another patch
            # call doesn't see itself mid-mutation.
            with _close_handlers_lock:
                handlers = list(_close_handlers)
            for handler in handlers:
                try:
                    handler(self)
                except Exception:  # noqa: BLE001
                    logger.exception("session close handler raised; continuing")

    BaseSession.__aexit__ = patched_aexit  # type: ignore[method-assign]
    _aexit_patched = True


# Install the patch at import time so any code path that uses BaseSession
# (registry-aware or not) participates in the close-handler dispatch. The
# patch is a single method replacement; no overhead until sessions actually
# close.
_ensure_aexit_patched()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SessionRegistry:
    """Bidirectional name↔session mapping with deterministic disconnect detection.

    Public surface:
        bind(name, session)        — bind name to session, replacing any prior
        unbind_name(name)          — drop a single name's binding
        get(name) -> session|None  — current binding for name (or None)
        is_bound(name) -> bool     — does this name have a live binding?
        __contains__(name) -> bool — alias for is_bound
        names() -> list[str]       — all currently-bound names
        push(name, notification)   — async, ping-then-send; returns True on
                                      successful push, False on no-binding /
                                      ping-failure / send-failure
        close()                    — detach from the global close hook (only
                                      needed if multiple registries co-exist)

    Thread-safety: all mutations and reads take an internal `threading.Lock`.
    Operations are O(1) and microsecond-fast, so blocking the event loop
    momentarily is fine.
    """

    # Loose enough to ride out normal network jitter, transient client
    # slowdowns (Claude Code mid-tool-call may delay ping response), and
    # CF/Traefik intermediaries. Tight enough that genuinely-dead sessions
    # still get reaped reasonably quickly. 5s observed empirically: 2s
    # was producing false-positive drops where my own session got reaped
    # mid-conversation despite being healthy.
    PING_TIMEOUT_SECONDS: float = 5.0

    # Cadence of the background liveness sweep. Cheap because the check is
    # an in-memory timestamp comparison (not a server-initiated ping).
    REAPER_INTERVAL_SECONDS: float = 30.0

    # Drop a binding if no touch_session call for this many seconds. The
    # reaper used to use server-initiated pings for liveness, but Claude
    # Code's MCP client cycles streamable-http sessions every ~30s
    # (DELETE /mcp + new POST), making the bound session_id dead within
    # ~30s of any tool call. Server-pings against those dead session_ids
    # always fail, which made the reaper drop live agents on every cycle.
    # Activity-based liveness (any tool call from the agent's session
    # refreshes the timestamp via touch_session) reflects reality:
    # "agent is engaged with the hub" is what we actually care about
    # for ⚡. 60 min generous-but-not-forever — accommodates long thinking
    # turns / multi-task chains / quiet stretches between conversations
    # without persisting truly-abandoned bindings.
    ACTIVITY_TIMEOUT_SECONDS: float = 3600.0  # 60 minutes

    # Wake-ack expectation: after the hub pushes a wake, the woken agent
    # always produces SOME observable activity (a tool call that binds, or
    # at minimum its Stop-hook draining messages). A client whose SSE stream
    # is half-dead accepts the push but renders nothing — binding fresh,
    # deliverability probe green, ⚡ lying. The server can't introspect the
    # far end, so it uses evidence it CAN see: no ack within the timeout is
    # a strike; WAKE_STRIKES_TO_DROP consecutive unacked wakes drop the
    # binding through the reaper path (truthful offline → Stop-hook nag),
    # and on_wake_dead lets the server queue relaunch guidance.
    WAKE_ACK_TIMEOUT_SECONDS: float = 90.0
    WAKE_STRIKES_TO_DROP: int = 2

    def __init__(
        self,
        on_reap: Callable[[str], None] | None = None,
        liveness_probe: Callable[[ServerSession], bool] | None = None,
        on_wake_dead: Callable[[str], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # Optional callback invoked (outside the lock) when the reaper drops a
        # binding for inactivity. The server wires this to mark the agent
        # offline in the DB so `list_agents` status stays truthful — a binding
        # the reaper gave up on means the agent is no longer connected. Kept as
        # an injected callback so the registry stays DB/transport-agnostic.
        self._on_reap = on_reap
        # Optional probe: given a bound session, return True if a push would
        # actually land RIGHT NOW (live GET /mcp listener). The server wires
        # this to `_can_deliver_push`. The reaper uses it so a still-deliverable
        # binding is NOT dropped for tool-call inactivity: a `--channels`
        # session sitting idle holds a live connection, which IS the liveness
        # signal — the open connection is the heartbeat. Without this, the
        # reaper dropped genuinely-wakeable idle agents after ACTIVITY_TIMEOUT,
        # marking the whole fleet offline if left idle past the timeout. Kept
        # injected so the registry stays transport-agnostic.
        self._liveness_probe = liveness_probe
        # Forward index: agent name -> session
        self._by_name: dict[str, ServerSession] = {}
        # Reverse index: id(session) -> set of names bound to it. One session
        # bound to multiple names is unusual but legal (e.g., aliases).
        self._by_session_id: dict[int, set[str]] = {}
        # Last activity timestamp per name. Updated on every bind() call,
        # which is itself called on every tool that takes an agent's name
        # (via touch_session in server.py). Reaper uses this to identify
        # truly-abandoned bindings vs. agents who are just between tool calls.
        self._last_activity: dict[str, float] = {}
        # Consecutive undeliverable heartbeats per name (see heartbeat_touch).
        # Reset by any successful probe or a fresh bind.
        self._undeliverable_beats: dict[str, int] = {}
        # Wake-ack tracking: name -> ack deadline for the most recent pushed
        # wake, and consecutive unacked-wake strikes. Cleared by bind() or an
        # explicit wake_ack() (message drain); NOT by daemon heartbeats — the
        # daemon proves the process lives, not that the human-facing session
        # heard anything.
        self._on_wake_dead = on_wake_dead
        self._wake_expect: dict[str, float] = {}
        self._wake_strikes: dict[str, int] = {}

        _ensure_aexit_patched()
        # Note: we do NOT auto-subscribe `_on_session_close` to the global
        # close-handler list. Reason: empirically (verified via prod-1
        # journalctl 2026-05-09), Claude Code's MCP client closes the
        # streamable-http session via `DELETE /mcp` after every tool call
        # and creates a new session for the next call. The lifecycle hook
        # fires on each DELETE, dropping the binding before the next call's
        # auto-bind can refresh it — leaving agents drifted between calls.
        #
        # The reaper (3-strike threshold) and push-time ping cover correctness
        # without needing the lifecycle hook drop. Dead sessions get cleaned
        # up by the reaper within ~90s, and any push attempt to a dead session
        # fails its ping check and drops cleanly.
        #
        # Tests that need the lifecycle behaviour can opt-in via
        # `subscribe_to_session_close()`.

    # -- mutation ------------------------------------------------------------

    def bind(self, name: str, session: ServerSession) -> None:
        """Bind `name` to `session`, replacing any prior binding for `name`.

        Re-binding the same name to the same session is a no-op for the
        index but still refreshes the activity timestamp — that's the
        signal the reaper uses to distinguish active agents from
        truly-abandoned bindings.
        """
        now = time.time()
        with self._lock:
            old = self._by_name.get(name)
            if old is session:
                # Same session — refresh activity, don't touch indexes.
                # Still counts as a wake-ack: the agent's interactive
                # session made a tool call.
                self._last_activity[name] = now
                self._wake_expect.pop(name, None)
                self._wake_strikes.pop(name, None)
                return
            if old is not None:
                old_id = id(old)
                names = self._by_session_id.get(old_id)
                if names is not None:
                    names.discard(name)
                    if not names:
                        del self._by_session_id[old_id]

            self._by_name[name] = session
            self._by_session_id.setdefault(id(session), set()).add(name)
            self._last_activity[name] = now
            # A fresh binding starts with a clean undeliverable-beat slate,
            # and any interactive bind is also a wake-ack (the agent acted).
            self._undeliverable_beats.pop(name, None)
            self._wake_expect.pop(name, None)
            self._wake_strikes.pop(name, None)

    def unbind_name(self, name: str) -> None:
        """Drop binding for `name` (if any). Idempotent."""
        with self._lock:
            self._unbind_name_locked(name)

    def touch_activity(self, name: str) -> bool:
        """Refresh `_last_activity[name]` IF a binding exists. Returns True
        if refreshed, False if no binding (heartbeat from an unbound agent
        is a no-op, not a bind).

        Used by the heartbeat path: a per-minute daemon spawned by an async
        SessionStart hook calls the hub from a separate process to prove
        the agent's Claude Code session is still alive. We don't want that
        daemon's ephemeral streamablehttp_client to be bound (same wake-
        clobber problem as the Stop hook), so heartbeat just keeps the
        existing binding's timestamp fresh.

        If the agent has no binding when the heartbeat arrives, the
        heartbeat is meaningless — the agent's interactive session must
        register() to establish the bind first; daemon heartbeats only
        keep it alive thereafter.
        """
        with self._lock:
            if name not in self._by_name:
                return False
            self._last_activity[name] = time.time()
            return True

    # After this many CONSECUTIVE undeliverable heartbeats the binding is
    # dropped. At the daemon's 60s cadence that's ~3 minutes — long enough
    # that a transient GET-stream flicker (one bad probe) never kills a
    # healthy binding, short enough that a genuinely dead binding stops
    # lying about wakeability quickly (vs. the 60-min activity reaper).
    UNDELIVERABLE_BEATS_TO_DROP: int = 3

    def heartbeat_touch(self, name: str) -> str:
        """Deliverability-verified activity refresh for the heartbeat path.

        Returns "refreshed" | "unbound" | "undeliverable" | "dropped".

        Plain touch_activity refreshes a binding UNCONDITIONALLY — which is
        the stale-binding blind spot: when the agent's client reconnects
        (hub redeploy, network blip), the binding points at the dead old
        session, but the daemon's heartbeats kept its activity fresh, so
        the reaper never dropped it and the agent looked online while
        wakes vanished into a dead socket (observed live 2026-07-18).

        This variant probes the bound session first (same probe as the
        reaper). Deliverable → refresh + reset the failure counter.
        Undeliverable → do NOT refresh; after UNDELIVERABLE_BEATS_TO_DROP
        consecutive failures, drop the binding via the reaper's exact
        path (unbind + on_reap → agent marked offline in the DB → the
        existing Stop-hook nag drives re-register). Worst case on a false
        drop is one nag + one idempotent re-register.

        No probe configured (test mode / non-introspectable transport) →
        behaves like touch_activity: refresh, trust the binding.
        """
        with self._lock:
            session = self._by_name.get(name)
        if session is None:
            return "unbound"

        deliverable = True
        if self._liveness_probe is not None:
            # Probe OUTSIDE the lock — it introspects transport state.
            try:
                deliverable = self._liveness_probe(session)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "heartbeat: liveness_probe(%s) raised; treating as "
                    "undeliverable", name,
                )
                deliverable = False

        if deliverable:
            with self._lock:
                if name not in self._by_name:
                    return "unbound"
                self._last_activity[name] = time.time()
                self._undeliverable_beats.pop(name, None)
                return "refreshed"

        dropped = False
        with self._lock:
            # Re-check under the lock: a register() may have replaced the
            # binding while we probed — a fresh binding must not inherit
            # the dead one's strikes.
            if self._by_name.get(name) is not session:
                self._undeliverable_beats.pop(name, None)
                return "refreshed" if name in self._by_name else "unbound"
            beats = self._undeliverable_beats.get(name, 0) + 1
            if beats >= self.UNDELIVERABLE_BEATS_TO_DROP:
                logger.info(
                    "heartbeat: dropping %s after %d consecutive "
                    "undeliverable beats (stale binding)", name, beats,
                )
                self._unbind_name_locked(name)
                dropped = True
            else:
                self._undeliverable_beats[name] = beats
                logger.info(
                    "heartbeat: %s undeliverable (beat %d/%d) — not "
                    "refreshed", name, beats, self.UNDELIVERABLE_BEATS_TO_DROP,
                )
        if dropped:
            # Same contract as the reaper: callback OUTSIDE the lock (it
            # does a DB write to mark the agent offline).
            if self._on_reap is not None:
                try:
                    self._on_reap(name)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "heartbeat: on_reap(%s) callback raised; continuing",
                        name,
                    )
            return "dropped"
        return "undeliverable"

    def _unbind_name_locked(self, name: str) -> None:
        session = self._by_name.pop(name, None)
        # Drop activity timestamp — a future re-bind starts fresh.
        self._last_activity.pop(name, None)
        self._undeliverable_beats.pop(name, None)
        self._wake_expect.pop(name, None)
        self._wake_strikes.pop(name, None)
        if session is None:
            return
        sid = id(session)
        names = self._by_session_id.get(sid)
        if names is not None:
            names.discard(name)
            if not names:
                del self._by_session_id[sid]

    # -- query ---------------------------------------------------------------

    def get(self, name: str) -> ServerSession | None:
        with self._lock:
            return self._by_name.get(name)

    def is_bound(self, name: str) -> bool:
        with self._lock:
            return name in self._by_name

    def __contains__(self, name: str) -> bool:
        return self.is_bound(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._by_name.keys())

    # -- lifecycle hook ------------------------------------------------------

    def _on_session_close(self, session: BaseSession) -> None:
        """Drop every name bound to `session`. Called by the global hook."""
        sid = id(session)
        with self._lock:
            dropped = self._by_session_id.pop(sid, None)
            if not dropped:
                return
            for name in dropped:
                self._by_name.pop(name, None)
        logger.info(
            "session closed; dropped bindings: %s", sorted(dropped)
        )

    def close(self) -> None:
        """Detach from the global close-handler list.

        Only needed if multiple registries co-exist in one process (e.g.,
        tests). Idempotent. No-op for registries that haven't subscribed
        (the default).
        """
        with _close_handlers_lock:
            try:
                _close_handlers.remove(self._on_session_close)
            except ValueError:
                pass

    def subscribe_to_session_close(self) -> None:
        """Opt-in: drop bindings when a session's __aexit__ fires.

        Off by default because Claude Code's MCP client tears down and
        re-creates streamable-http sessions per tool call, which would
        otherwise cause bindings to flap. Tests that want to verify the
        old lifecycle-drop behaviour can call this explicitly.
        """
        with _close_handlers_lock:
            if self._on_session_close not in _close_handlers:
                _close_handlers.append(self._on_session_close)

    # -- push ----------------------------------------------------------------

    async def push(self, name: str, notification: Any) -> bool:
        """Push `notification` to `name`. On any failure, return False but
        DO NOT unbind.

        The previous implementation pinged before sending; that ping was
        observed in production to fail false-positively against live
        sessions (Claude Code's interactive MCP client may not respond to
        `ping` requests at all, even when the underlying connection is
        fine and the channel-notification listener is alive). The ping was
        therefore a worse-than-useless gate — it short-circuited valid
        sends because of an unrelated client-side ping handler quirk.

        New contract: just send. Trust the bound session is alive; if it
        isn't, send raises and we report False. Same correctness as the
        ping-then-send version, half the round-trips, no false negatives
        from ping handler absence.

        Failure handling unchanged from the post-67f5ea4 contract:
        binding is kept on send failure. The activity-based reaper is
        the only authoritative drop path. Caller has already persisted
        the message in SQLite, so a False return doesn't mean message
        loss — recipient picks it up via Stop-hook auto-pull on their
        next turn end.
        """
        session = self.get(name)
        if session is None:
            return False

        try:
            await session.send_notification(notification)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "push %s: send failed (%s: %s); binding kept "
                "(activity reaper owns lifecycle)",
                name, type(exc).__name__, exc,
            )
            return False

    # -- wake-ack expectation (dead-stream detection) -------------------------

    def expect_wake_ack(self, name: str) -> None:
        """Arm an ack expectation after a wake was pushed to `name`.

        No-op if unbound or an expectation is already pending (a burst of
        wakes shouldn't stack deadlines — one outstanding expectation at a
        time is enough evidence)."""
        with self._lock:
            if name not in self._by_name or name in self._wake_expect:
                return
            self._wake_expect[name] = time.time() + self.WAKE_ACK_TIMEOUT_SECONDS

    def wake_ack(self, name: str) -> None:
        """Record evidence the agent's session is alive and hearing us —
        called on message drain (get_messages / get_broadcasts_for_agent,
        regardless of bind flag) in addition to bind()'s implicit ack."""
        with self._lock:
            self._wake_expect.pop(name, None)
            self._wake_strikes.pop(name, None)

    def sweep_wake_acks(self) -> list[str]:
        """Expire overdue expectations; drop bindings past the strike limit.

        Returns the names dropped this sweep (already unbound; on_reap and
        on_wake_dead fired for each — outside the lock, same contract as the
        reaper). Run from the reaper loop every REAPER_INTERVAL_SECONDS."""
        now = time.time()
        dropped: list[str] = []
        with self._lock:
            for name, deadline in list(self._wake_expect.items()):
                if now < deadline:
                    continue
                del self._wake_expect[name]
                strikes = self._wake_strikes.get(name, 0) + 1
                if strikes >= self.WAKE_STRIKES_TO_DROP and name in self._by_name:
                    logger.info(
                        "wake-ack: dropping %s after %d unacked wakes "
                        "(stream presumed dead behind live binding)",
                        name, strikes,
                    )
                    self._unbind_name_locked(name)
                    dropped.append(name)
                else:
                    self._wake_strikes[name] = strikes
                    logger.info(
                        "wake-ack: %s missed ack (strike %d/%d)",
                        name, strikes, self.WAKE_STRIKES_TO_DROP,
                    )
        for name in dropped:
            for cb, label in ((self._on_reap, "on_reap"),
                              (self._on_wake_dead, "on_wake_dead")):
                if cb is None:
                    continue
                try:
                    cb(name)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "wake-ack: %s(%s) callback raised; continuing",
                        label, name,
                    )
        return dropped

    # -- background reaper ---------------------------------------------------

    def _check_one(self, name: str) -> bool:
        """Check whether `name`'s binding should survive. Drop it only if it's
        both inactive past ACTIVITY_TIMEOUT *and* no longer push-deliverable.
        Returns True if the binding survives, False otherwise.

        "Activity" = any tool call from the agent that ran through
        touch_session (server.py side) — register, send, broadcast, post,
        get_messages, ping, update_bio, get_broadcasts_for_agent. Each of
        those refreshes the timestamp via bind().

        Activity alone is not enough: a `--channels` session sitting idle for
        hours makes no tool calls, yet holds a live GET /mcp connection the
        hub can push to — it's genuinely wakeable and must NOT be reaped. So
        once activity goes stale we consult `_liveness_probe` (the server's
        `_can_deliver_push`): if a push would still land, the open connection
        IS the heartbeat — refresh the timestamp and keep the binding. Only a
        binding that is BOTH silent AND undeliverable is truly abandoned.

        (Activity is still the fast path: a recently-active agent is kept
        without probing. The probe replaces the old server-initiated ping,
        which was unreliable because Claude Code cycles streamable-http POST
        sessions ~every 30s; the channel GET stream, by contrast, persists
        across idle, so probing it is a sound liveness signal.)
        """
        with self._lock:
            last = self._last_activity.get(name)
            if last is None:
                return False  # not bound
            age = time.time() - last
            if age <= self.ACTIVITY_TIMEOUT_SECONDS:
                return True
            session = self._by_name.get(name)

        # Activity is stale. Before dropping, check whether the bound session
        # is still push-deliverable — done OUTSIDE the lock because the probe
        # introspects transport state and we don't hold the registry lock
        # across it.
        if session is not None and self._liveness_probe is not None:
            try:
                deliverable = self._liveness_probe(session)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reaper: liveness_probe(%s) raised; treating as "
                    "undeliverable", name,
                )
                deliverable = False
            if deliverable:
                # Live connection is the heartbeat — keep it, refresh activity.
                with self._lock:
                    if name in self._by_name:
                        self._last_activity[name] = time.time()
                logger.debug(
                    "reaper: %s stale (%.0fs) but still deliverable — kept",
                    name, age,
                )
                return True

        # Both silent and undeliverable (or unprobeable) — drop.
        dropped = False
        with self._lock:
            # Re-check: activity may have arrived while we probed.
            last = self._last_activity.get(name)
            if last is None:
                return False
            if time.time() - last <= self.ACTIVITY_TIMEOUT_SECONDS:
                return True
            logger.info(
                "reaper: dropping %s after %.0fs of inactivity (not deliverable)",
                name, time.time() - last,
            )
            self._unbind_name_locked(name)
            dropped = True
        # Fire the offline callback OUTSIDE the lock — it may do a DB write,
        # which we don't want to hold the registry lock across.
        if dropped and self._on_reap is not None:
            try:
                self._on_reap(name)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reaper: on_reap(%s) callback raised; continuing", name
                )
        return False

    async def run_reaper(self) -> None:
        """Background task: periodically check every bound name for recent
        activity, drop bindings that have been silent past the timeout.
        Cheap: pure in-memory timestamp comparison, no network.

        Run as a sibling task to the server's main loop. Cancellation
        cleanly exits.
        """
        logger.info(
            "reaper: started (interval=%.0fs, activity_timeout=%.0fs)",
            self.REAPER_INTERVAL_SECONDS, self.ACTIVITY_TIMEOUT_SECONDS,
        )
        try:
            while True:
                await anyio.sleep(self.REAPER_INTERVAL_SECONDS)
                try:
                    self.sweep_wake_acks()
                except Exception:  # noqa: BLE001
                    logger.exception("reaper: wake-ack sweep raised; continuing")
                for name in self.names():
                    try:
                        self._check_one(name)
                    except Exception:  # noqa: BLE001
                        # Per-name error must not kill the loop. The check
                        # is in-memory only, so this should be rare, but
                        # defensiveness is cheap.
                        logger.exception(
                            "reaper: _check_one(%s) raised; continuing",
                            name,
                        )
        finally:
            logger.info("reaper: stopped")
