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

    def __init__(self) -> None:
        self._lock = threading.Lock()
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
                # Same session — refresh activity, don't touch indexes
                self._last_activity[name] = now
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

    def _unbind_name_locked(self, name: str) -> None:
        session = self._by_name.pop(name, None)
        # Drop activity timestamp — a future re-bind starts fresh.
        self._last_activity.pop(name, None)
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
        """Push `notification` to `name`. Try ping-then-send; on any failure,
        return False but DO NOT unbind.

        Why we don't unbind on failure: Claude Code's MCP client cycles
        streamable-http session_ids per ~30s of inactivity (DELETE /mcp,
        new POST on next call). The session_id we have bound is therefore
        often dead by the time anyone tries to push to it — the bound
        ServerSession's underlying connection has been DELETEd, so
        send_ping/send_notification raise. The PREVIOUS behaviour was to
        unbind on those failures, which produced the same false-positive
        symptom as the old ping-based reaper: a passing peer's send to an
        idle agent dropped the agent's binding.

        New contract:
          1. Look up the binding. If absent, return False.
          2. Try ping; on failure, return False — leave binding intact.
          3. Try send; on failure, return False — leave binding intact.
          4. Return True only on a clean ping + send.

        The activity-based reaper is the only authoritative source of drop
        ("no tool call from this agent in N seconds"). Push failures are
        treated as transient — caller has already persisted the message in
        SQLite, so the recipient picks it up via Stop-hook surfacing on
        their next turn end. Misleading-⚡ for an unreachable session lasts
        at most ACTIVITY_TIMEOUT_SECONDS, which is the same window we
        already tolerate for "agent went away without unregistering."
        """
        session = self.get(name)
        if session is None:
            return False

        # Liveness check — short-circuits send when the connection is
        # already known to be dead, saving the second round-trip. We
        # treat the result as advisory: failure means we won't even try
        # send, but we don't drop the binding (the activity reaper owns
        # the lifecycle).
        try:
            with anyio.fail_after(self.PING_TIMEOUT_SECONDS):
                await session.send_ping()
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "push %s: liveness ping failed (%s: %s); skipping send, "
                "binding kept (activity reaper owns lifecycle)",
                name, type(exc).__name__, exc,
            )
            return False

        try:
            await session.send_notification(notification)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push %s: send failed after live ping (%s: %s); "
                "binding kept (activity reaper owns lifecycle)",
                name, type(exc).__name__, exc,
            )
            return False

    # -- background reaper ---------------------------------------------------

    def _check_one(self, name: str) -> bool:
        """Check whether `name`'s binding has had recent activity. Drop the
        binding if not. Returns True if the binding survives, False otherwise.

        "Activity" = any tool call from the agent that ran through
        touch_session (server.py side) — register, send, broadcast, post,
        get_messages, ping, update_bio, get_broadcasts_for_agent. Each of
        those refreshes the timestamp via bind().

        This replaces the previous server-initiated-ping liveness check,
        which was unreliable in production: Claude Code's MCP client cycles
        streamable-http sessions every ~30s (DELETE+new POST), so the bound
        session_id was usually dead by the time the reaper pinged it. Pings
        timed out, the reaper dropped the binding, and live agents looked
        offline. Activity is the reliable signal.
        """
        with self._lock:
            last = self._last_activity.get(name)
            if last is None:
                return False  # not bound
            age = time.time() - last
            if age <= self.ACTIVITY_TIMEOUT_SECONDS:
                return True
            # Stale — drop
            logger.info(
                "reaper: dropping %s after %.0fs of inactivity",
                name, age,
            )
            self._unbind_name_locked(name)
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
