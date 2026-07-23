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
import uuid
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
        # Binding-generation tokens: agent name -> "<boot>:<n>", minted afresh
        # every time a name binds to a DIFFERENT session. A push records the
        # recipient's token of the moment; the inbox pull compares it against
        # the token that's current then. Equal means the push went to the very
        # stream the agent is STILL holding — positive evidence it surfaced, so
        # the Stop hook can say "already delivered" instead of reprinting the
        # whole message. Any inequality (rebound, unbound, or hub restarted)
        # fails safe to a full reprint, because that is exactly the case where
        # a push may have vanished into a dead stream. `_boot` makes tokens
        # unique per hub PROCESS, so a restart can never accidentally match a
        # pre-restart token whose stream died with the old process.
        self._boot = uuid.uuid4().hex[:8]
        self._gen_seq = 0
        self._generation: dict[str, str] = {}
        # Forward index: agent name -> PRIMARY session (most-recently-active).
        # The primary carries ALL the lifecycle machinery below (reaper,
        # heartbeat-drop, wake-ack, generation) exactly as it did when this
        # was strictly 1:1 — every prior incident fix is preserved untouched.
        self._by_name: dict[str, ServerSession] = {}
        # EXTRA sessions for a name — the other live conversations that share
        # one derived identity (e.g. a tmux session AND a Co-work session in
        # the same repo on the same host both derive `pm-dev-vm-1`). Before
        # this, a second register() EVICTED the first (silent clobber → the
        # loser went deaf while ⚡ still showed). Now the incumbent is DEMOTED
        # here instead of evicted, and wakes fan out to primary + all extras.
        # Extras are lighter-weight than the primary: they get delivery +
        # push-time pruning, not the full heartbeat/reaper verification. A dead
        # extra is dropped on the next wake (its send fails the deliverability
        # gate) or on session-close; between wakes it only inflates the count.
        # name -> {id(session): session}
        self._extra_sessions: dict[str, dict[int, ServerSession]] = {}
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
        """Bind `name` to `session`, making it the PRIMARY for `name`.

        Re-binding the same name to the same session is a no-op for the
        index but still refreshes the activity timestamp — that's the
        signal the reaper uses to distinguish active agents from
        truly-abandoned bindings.

        Multi-session: if `name` already has a DIFFERENT primary, that
        incumbent is DEMOTED into `_extra_sessions` (not evicted) — the two
        conversations share one derived identity and both stay wakeable.
        The most-recently-active session is always the primary, so the full
        lifecycle machinery (reaper / heartbeat / wake-ack / generation)
        tracks whichever session last acted. If `session` was already a
        known extra of this name, it is promoted out of the extras set.
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

            # `session` may already be a KNOWN extra of this name (a second
            # conversation that registered earlier and is now the one acting).
            # Promote it out of the extras set — it becomes primary below.
            extras = self._extra_sessions.get(name)
            if extras is not None:
                extras.pop(id(session), None)
                if not extras:
                    del self._extra_sessions[name]

            if old is not None:
                # DEMOTE the incumbent primary into extras instead of evicting
                # it. It stays in `_by_session_id` (so session-close still
                # cleans it up) and keeps receiving fanned-out wakes. This is
                # the whole point: a second register() no longer silently
                # blinds the first conversation.
                self._extra_sessions.setdefault(name, {})[id(old)] = old

            self._by_name[name] = session
            self._by_session_id.setdefault(id(session), set()).add(name)
            # New primary for this name -> new generation token. Only minted
            # here: the same-session refresh above returns early, so a token
            # survives as long as the underlying stream stays primary.
            self._gen_seq += 1
            self._generation[name] = f"{self._boot}:{self._gen_seq}"
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
                # Drop the stale primary. If another live conversation is
                # bound under this name, it's promoted to primary and the
                # agent stays online — the heartbeat then reports "refreshed"
                # (the new primary is trusted like a fresh bind, re-probed
                # next beat). Only a name with no survivor is truly dropped.
                fully_offline = self._drop_primary_locked(name)
                if fully_offline:
                    logger.info(
                        "heartbeat: dropping %s after %d consecutive "
                        "undeliverable beats (stale binding, no other "
                        "session)", name, beats,
                    )
                    dropped = True
                else:
                    logger.info(
                        "heartbeat: %s primary undeliverable after %d beats "
                        "— promoted an extra session to primary; agent stays "
                        "online", name, beats,
                    )
                    return "refreshed"
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
        """Take `name` FULLY offline — primary AND every extra. Callers that
        want to keep the agent alive via a survivor must use
        `_drop_primary_locked` instead; this is the terminal drop."""
        session = self._by_name.pop(name, None)
        # Drop activity timestamp — a future re-bind starts fresh.
        self._last_activity.pop(name, None)
        self._undeliverable_beats.pop(name, None)
        self._wake_expect.pop(name, None)
        self._wake_strikes.pop(name, None)
        # Unbinding invalidates the generation: anything pushed to the stream
        # we just dropped must be reprinted in full, never summarised away.
        self._generation.pop(name, None)

        def _drop_from_reverse(sess: ServerSession) -> None:
            sid = id(sess)
            names = self._by_session_id.get(sid)
            if names is not None:
                names.discard(name)
                if not names:
                    del self._by_session_id[sid]

        if session is not None:
            _drop_from_reverse(session)
        # Purge extras too, so "fully offline" leaves no orphan in
        # _extra_sessions / _by_session_id / sessions(). Without this a direct
        # unbind_name() on a multi-session agent would leave extras reachable
        # via sessions() while is_bound() reported False — an inconsistent
        # half-offline state.
        extras = self._extra_sessions.pop(name, None)
        if extras:
            for sess in extras.values():
                _drop_from_reverse(sess)

    def _drop_primary_locked(self, name: str) -> bool:
        """Drop the current PRIMARY session for `name`, promoting a live extra
        if one exists. Returns True iff `name` is now FULLY offline (no session
        of any kind left), False if an extra was promoted to primary.

        This is what the drop paths (heartbeat / wake-ack / reaper / session-
        close) call INSTEAD of `_unbind_name_locked` now that a name can carry
        more than one session. The invariant those paths relied on — "drop the
        binding == agent offline" — only holds when there are no extras; with
        extras, dropping the unhealthy primary must hand off to a survivor, not
        take the agent offline.

        The promoted extra is trusted the same way a fresh `bind()` trusts a
        new session: clean verification slate, fresh activity, new generation.
        If it too is dead, the next heartbeat / wake-ack / reaper cycle drops
        it in turn, walking down the extras until none remain — at which point
        this returns True and the caller fires `on_reap` (agent → offline).
        """
        extras = self._extra_sessions.get(name)
        if not extras:
            self._unbind_name_locked(name)
            return True
        # Promote the most-recently-demoted extra (dicts preserve insertion
        # order; the newest extra is the freshest conversation).
        promoted_id = next(reversed(extras))
        promoted = extras.pop(promoted_id)
        if not extras:
            del self._extra_sessions[name]
        # Retire the outgoing primary from the reverse index — its session is
        # being dropped. (If a caller already popped its sid, the guard is a
        # no-op.)
        old = self._by_name.get(name)
        if old is not None:
            old_id = id(old)
            names = self._by_session_id.get(old_id)
            if names is not None:
                names.discard(name)
                if not names:
                    del self._by_session_id[old_id]
        # Install the promoted extra as primary, treated as a fresh binding.
        self._by_name[name] = promoted
        self._by_session_id.setdefault(promoted_id, set()).add(name)
        self._gen_seq += 1
        self._generation[name] = f"{self._boot}:{self._gen_seq}"
        self._last_activity[name] = time.time()
        self._undeliverable_beats.pop(name, None)
        self._wake_expect.pop(name, None)
        self._wake_strikes.pop(name, None)
        return False

    def _unbind_session_locked(self, name: str, session: ServerSession) -> bool:
        """Drop ONE specific `session` from `name` (primary or extra). If it
        was the primary, promote an extra / fully unbind via
        `_drop_primary_locked`. Returns True iff `name` is now fully offline.

        Used to prune a dead EXTRA at push time (its send failed / it's no
        longer deliverable) without disturbing a healthy primary.
        """
        if self._by_name.get(name) is session:
            return self._drop_primary_locked(name)
        extras = self._extra_sessions.get(name)
        if extras is not None and id(session) in extras:
            del extras[id(session)]
            if not extras:
                del self._extra_sessions[name]
            sid = id(session)
            names = self._by_session_id.get(sid)
            if names is not None:
                names.discard(name)
                if not names:
                    del self._by_session_id[sid]
        return name not in self._by_name

    # -- query ---------------------------------------------------------------

    def get(self, name: str) -> ServerSession | None:
        with self._lock:
            return self._by_name.get(name)

    def sessions(self, name: str) -> list[ServerSession]:
        """All live sessions for `name` — primary first, then extras in
        demotion order. Wakes fan out across this list; an empty list means
        the agent has no binding at all."""
        with self._lock:
            result: list[ServerSession] = []
            primary = self._by_name.get(name)
            if primary is not None:
                result.append(primary)
            extras = self._extra_sessions.get(name)
            if extras:
                result.extend(extras.values())
            return result

    def session_count(self, name: str) -> int:
        """Number of live sessions bound to `name` (primary + extras)."""
        with self._lock:
            n = 1 if name in self._by_name else 0
            extras = self._extra_sessions.get(name)
            if extras:
                n += len(extras)
            return n

    def unbind_session(self, name: str, session: ServerSession) -> bool:
        """Public: drop one specific session from `name`. Returns True iff
        `name` is now fully offline. Fires no callbacks — the caller decides
        (push-time pruning of a dead extra must NOT mark the agent offline;
        that's owned by the reaper / wake-ack paths)."""
        with self._lock:
            return self._unbind_session_locked(name, session)

    def generation(self, name: str) -> str | None:
        """Current binding-generation token for `name`, or None if unbound.

        Compare a token recorded at push time against this: equal means the
        agent still holds the same stream the push was written to. Never
        reuse-able across hub processes (see `_boot`).
        """
        with self._lock:
            return self._generation.get(name)

    @property
    def boot_id(self) -> str:
        """This hub PROCESS's nonce — a fresh uuid per registry (per
        `create_server`, i.e. per hub start). Stable for the life of the
        process, guaranteed to differ across a restart. The heartbeat exposes
        it so the daemon can tell a genuine hub RESTART (nonce changed → every
        wake stream is dead) from a mere client-side blip or a reaper-dropped
        binding (nonce unchanged → hub sat there fine). See the disruption
        stamp: inferring "restarted" from "no binding" false-positived and
        mass-restarted the fleet on a wifi flap (2026-07-20 / reproved
        2026-07-23); the nonce is the positive evidence that replaces it."""
        return self._boot

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
        """Drop `session` from every name it's bound to. Called by the global
        hook. Multi-session aware: where `session` is a name's PRIMARY, a live
        extra is promoted so the other conversation stays online; where it's an
        EXTRA, it's simply removed. No offline callback fires here (this hook
        is opt-in and historically never marked agents offline — the reaper /
        wake-ack paths own that)."""
        sid = id(session)
        with self._lock:
            names = self._by_session_id.pop(sid, None)
            if not names:
                return
            for name in list(names):
                if self._by_name.get(name) is session:
                    # sid already popped above, so _drop_primary_locked's
                    # reverse-index cleanup for the outgoing primary no-ops.
                    self._drop_primary_locked(name)
                else:
                    extras = self._extra_sessions.get(name)
                    if extras is not None:
                        extras.pop(sid, None)
                        if not extras:
                            del self._extra_sessions[name]
        logger.info(
            "session closed; dropped bindings: %s", sorted(names)
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
        return await self.push_to_session(name, session, notification)

    async def push_to_session(
        self, name: str, session: ServerSession, notification: Any
    ) -> bool:
        """Send `notification` to one SPECIFIC session of `name`. Returns True
        on a successful send, False on failure. Does NOT unbind on failure —
        the caller decides (the fan-out prunes dead EXTRAS but leaves the
        primary for the reaper/wake-ack paths). `name` is for logging only."""
        try:
            await session.send_notification(notification)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "push %s: send to session %d failed (%s: %s); "
                "caller owns lifecycle",
                name, id(session), type(exc).__name__, exc,
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

    def has_pending_wake_ack(self, name: str) -> bool:
        """True if a wake was pushed to `name` and NOT yet acked by anything.

        This is the render-liveness gate for the compact "already delivered
        live" claim. `_can_deliver_push` proves a GET stream is PRESENT, but a
        half-dead stream (bound + ⚡ after a redeploy reconnect, before a
        process relaunch) passes presence while rendering nothing — proven
        live on Windows 2026-07-23: a delivered push was falsely stamped
        "delivered live" and truncated on a stream that showed the agent
        nothing. Push-success ≠ render.

        A still-pending expectation means the last delivered wake produced NO
        independent ack (no interactive tool call, no reply) — so we have zero
        positive evidence the stream rendered. Callers must fail SAFE: full
        reprint, never a "you already saw this" claim. The claim is only safe
        once the recipient has independently acked (an interactive bind /
        reply) BEFORE the Stop-hook drain — the drain itself is NOT render
        evidence, it's how a deaf agent DISCOVERS what it missed."""
        with self._lock:
            return name in self._wake_expect

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
                    # Drop the primary whose stream never acked. If a second
                    # conversation is bound under this name, promote it and
                    # keep the agent online (its slate is cleared by
                    # _drop_primary_locked, so it isn't charged the dead
                    # primary's strikes). Only fire the offline callbacks when
                    # no session survives.
                    if self._drop_primary_locked(name):
                        logger.info(
                            "wake-ack: dropping %s after %d unacked wakes "
                            "(stream presumed dead, no other session)",
                            name, strikes,
                        )
                        dropped.append(name)
                    else:
                        logger.info(
                            "wake-ack: %s primary unacked after %d wakes — "
                            "promoted an extra session; agent stays online",
                            name, strikes,
                        )
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
            # Drop the silent+undeliverable primary. If another live session is
            # bound under this name (e.g. an idle --channels conversation still
            # holding a GET stream), promote it and keep the agent online; only
            # fire on_reap when nothing survives.
            fully_offline = self._drop_primary_locked(name)
            if fully_offline:
                logger.info(
                    "reaper: dropping %s after %.0fs of inactivity (not "
                    "deliverable, no other session)", name, time.time() - last,
                )
                dropped = True
            else:
                logger.info(
                    "reaper: %s primary stale+undeliverable — promoted an "
                    "extra session; agent stays online", name,
                )
                return True
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
