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

    # Tight enough to catch true zombies, loose enough to ride out normal
    # network jitter or a recipient that's mid-tool-call. 2s is comfortable
    # for a same-region hub→client→hub round-trip.
    PING_TIMEOUT_SECONDS: float = 2.0

    # Cadence of the background liveness sweep. The lifecycle hook is the
    # primary cleanup mechanism, but streamable-http sessions can outlive
    # their underlying socket (StreamableHTTPSessionManager keeps sessions
    # warm by session-id, not by connection). The reaper closes that gap
    # so `list_agents` stays roughly honest. 30s feels right: short enough
    # that staleness windows are bearable, long enough to be cheap.
    REAPER_INTERVAL_SECONDS: float = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Forward index: agent name -> session
        self._by_name: dict[str, ServerSession] = {}
        # Reverse index: id(session) -> set of names bound to it. One session
        # bound to multiple names is unusual but legal (e.g., aliases).
        self._by_session_id: dict[int, set[str]] = {}

        _ensure_aexit_patched()
        with _close_handlers_lock:
            _close_handlers.append(self._on_session_close)

    # -- mutation ------------------------------------------------------------

    def bind(self, name: str, session: ServerSession) -> None:
        """Bind `name` to `session`, replacing any prior binding for `name`.

        Idempotent: re-binding the same name to the same session is a no-op.
        Re-binding to a different session drops the old reverse-index entry
        for the old session.
        """
        with self._lock:
            old = self._by_name.get(name)
            if old is session:
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

    def unbind_name(self, name: str) -> None:
        """Drop binding for `name` (if any). Idempotent."""
        with self._lock:
            self._unbind_name_locked(name)

    def _unbind_name_locked(self, name: str) -> None:
        session = self._by_name.pop(name, None)
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
        tests). Idempotent.
        """
        with _close_handlers_lock:
            try:
                _close_handlers.remove(self._on_session_close)
            except ValueError:
                pass

    # -- push ----------------------------------------------------------------

    async def push(self, name: str, notification: Any) -> bool:
        """Push `notification` to `name` with active liveness check.

        Sequence:
          1. Look up the binding. If absent, return False (recipient offline).
          2. Send an MCP ping with a tight timeout. If it fails or times out,
             drop the binding and return False (zombie connection).
          3. Send the notification. If that raises, drop the binding and
             return False.
          4. Return True only on a clean ping + send.

        Returns True only when the notification has been written to a
        verified-live connection. False covers all other cases — caller
        should treat False as "recipient unreachable; rely on persisted
        inbox / next register to deliver."
        """
        session = self.get(name)
        if session is None:
            return False

        # Liveness check — the load-bearing safety net for zombies that
        # haven't fired __aexit__ yet.
        try:
            with anyio.fail_after(self.PING_TIMEOUT_SECONDS):
                await session.send_ping()
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "liveness ping to %s failed (%s: %s); dropping binding",
                name, type(exc).__name__, exc,
            )
            self.unbind_name(name)
            return False

        # Ping returned, so the connection is live as of microseconds ago.
        # Send the actual notification.
        try:
            await session.send_notification(notification)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "send to %s failed after live ping (%s: %s); dropping binding",
                name, type(exc).__name__, exc,
            )
            self.unbind_name(name)
            return False

    # -- background reaper ---------------------------------------------------

    async def _ping_one(self, name: str) -> bool:
        """Ping the session bound to `name`. Drop on failure. Returns True
        if the session is alive after the call, False otherwise."""
        session = self.get(name)
        if session is None:
            return False
        try:
            with anyio.fail_after(self.PING_TIMEOUT_SECONDS):
                await session.send_ping()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "reaper: dropping %s (%s: %s)",
                name, type(exc).__name__, exc,
            )
            self.unbind_name(name)
            return False

    async def run_reaper(self) -> None:
        """Background task: periodically ping every bound session and drop
        dead ones. Run as a sibling task to the server's main loop;
        cancellation cleanly exits.

        Without this, streamable-http sessions can stay bound after their
        client process exits (the session manager keeps them warm by
        session-id, not by socket), making `list_agents` ⚡ a lie until
        something tries to push to them.
        """
        logger.info(
            "reaper: started (interval=%.0fs, ping_timeout=%.1fs)",
            self.REAPER_INTERVAL_SECONDS, self.PING_TIMEOUT_SECONDS,
        )
        try:
            while True:
                await anyio.sleep(self.REAPER_INTERVAL_SECONDS)
                # Snapshot under lock; ping outside lock (pings are async).
                for name in self.names():
                    await self._ping_one(name)
        finally:
            logger.info("reaper: stopped")
