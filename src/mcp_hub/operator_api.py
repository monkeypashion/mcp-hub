"""The operator-token side of /api/v1 — one token rule, one honest failure per cause.

Three situations look identical to a caller and are NOT the same problem:

    no token here    this machine has never been given one
    API disabled     the hub is up and answering, MCP_HUB_API_TOKEN unset there
    unreachable      the network or the process is genuinely gone

Collapsing them is how ``hub registry unreachable (Illegal header value
b'Bearer ')`` came to be shown to an operator whose hub was perfectly healthy:
an empty token makes httpx refuse to build the header, so the request never
leaves the box and the error names the symptom furthest from the cause. The
operator goes hunting for an outage that isn't there.

So each state gets its own sentence, and each sentence names the fix. The
distinction is the product here — `ApiUnavailable` is not a generic wrapper,
it is the thing that stops the manager lying about which half is broken.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

TOKEN_FILE = pathlib.Path.home() / ".mcp-hub" / "api.token"
MACHINE_TOKEN_FILE = pathlib.Path.home() / ".mcp-hub" / "machine.token"


class ApiUnavailable(Exception):
    """Carries an operator-ready reason. Callers render it verbatim.

    The message is the whole value: it is written to be read by a person
    deciding what to fix, not by a handler deciding what to retry.
    """


def resolve_token(token_file: pathlib.Path | None = None) -> str:
    """Env first, then the per-machine file. Empty means "not configured here"."""
    token = os.environ.get("MCP_HUB_API_TOKEN", "").strip()
    if token:
        return token
    path = TOKEN_FILE if token_file is None else token_file
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def api_base(hub_url: str) -> str:
    """The API root beside the MCP endpoint (…/mcp -> …)."""
    return hub_url.rsplit("/mcp", 1)[0]


class OperatorApi:
    """Minimal operator client. `client` is injected so tests need no socket."""

    def __init__(
        self,
        base: str,
        token: str | None = None,
        timeout: float = 5.0,
        client: Any = None,
        token_file: pathlib.Path | None = None,
    ) -> None:
        self._base = base.rstrip("/")
        self._token = resolve_token(token_file) if token is None else token
        self._timeout = timeout
        self._client = client

    # -- plumbing ---------------------------------------------------------

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        return httpx.Client(timeout=self._timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        # Refuse BEFORE the transport does. httpx raises on the malformed
        # header an empty token produces, and that error describes a string,
        # not a missing credential.
        if not self._token:
            raise ApiUnavailable(
                f"no hub API token on this machine (write one to {TOKEN_FILE})"
            )
        try:
            r = self._http().request(
                method, f"{self._base}{path}", headers=self._headers(), **kw
            )
        except ApiUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — any transport failure
            raise ApiUnavailable(
                f"hub unreachable at {self._base} ({type(exc).__name__}: {exc})"
            ) from exc
        if r.status_code == 503:
            # The hub is up and talking; its management surface is switched
            # off. Naming the env var is the whole point — this is the state
            # the fleet has actually been in since the API shipped.
            raise ApiUnavailable(
                "the hub's management API is disabled"
                " (MCP_HUB_API_TOKEN is not set on the hub)"
            )
        if r.status_code in (401, 403):
            raise ApiUnavailable(
                f"the hub rejected this machine's API token ({r.status_code})"
            )
        if r.status_code >= 400:
            raise ApiUnavailable(
                f"hub API error {r.status_code} on {path}: {_body_snippet(r)}"
            )
        return r

    # -- calls ------------------------------------------------------------

    def get_registry(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/workspace-registry").json()

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/workspaces").json()["workspaces"]

    def create_workspace(
        self,
        name: str,
        machine: str = "",
        squad: str = "",
        listings: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/workspaces",
            json={
                "name": name,
                "machine": machine,
                "squad": squad,
                "listings": listings or [],
            },
        ).json()

    def rotate_machine_token(self, name: str) -> dict[str, Any]:
        """A NEW machine token, invalidating the old one. Returned once.

        This is the recovery path that did not exist: without it, a lost
        machine token was unrecoverable and the fleet had to fall back to the
        operator token for every edge pass.
        """
        return self._request(
            "POST", f"/api/v1/machines/{name}/rotate-token"
        ).json()

    def delete_workspace(self, wid: Any) -> dict[str, Any]:
        """Drop a DEFINITION. Deletes nothing on any disk.

        The two halves of a workspace are cleaned by different tools and it
        matters which you reach for: `squad teardown workspace` removes the
        file, this removes the hub's record of it. Do only the first and the
        definition survives as a ghost row; do only this and the file becomes
        feral. The manager shows both, in both directions, on purpose.
        """
        return self._request("DELETE", f"/api/v1/workspaces/{wid}").json()

    # -- seats: WHAT may run, independent of where ---------------------------

    def list_seats(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/seats").json()["seats"]

    def create_seat(
        self,
        repo: str,
        machine: str,
        folder: str,
        identity: str = "",
        launch_args: str = "",
        klass: str = "squad",
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Declare a seat. Identity is ASSIGNED by the hub when not given —
        never derived at the far end, because a container's hostname must not
        be allowed to name a seat.

        `spec` carries substrate-specific fields (image/env/ports/volumes for
        docker). Its presence is what makes a unit a container rather than a
        worktree, so a service and an agent seat are declared the same way.
        """
        body: dict[str, Any] = {
            "repo": repo, "machine": machine, "folder": folder,
            "launch_args": launch_args, "class": klass,
            "spec": spec or {},
        }
        if identity:
            body["identity"] = identity
        return self._request("POST", "/api/v1/seats", json=body).json()

    def delete_seat(self, identity: str) -> dict[str, Any]:
        """Archive a seat. Refused by the hub while it still has active
        placements — reclaim those first, or the fleet would be left with
        placements naming a seat that no longer exists."""
        return self._request("DELETE", f"/api/v1/seats/{identity}").json()

    # -- placements: WHERE a seat runs, and whether it should ----------------

    def list_placements(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/placements").json()["placements"]

    def create_placement(
        self,
        seat: str,
        machine: str,
        substrate: str = "worktree",
        desired: str = "running",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/placements",
            json={"seat": seat, "machine": machine,
                  "substrate": substrate, "desired": desired},
        ).json()

    def set_placement(self, pid: str, desired: str) -> dict[str, Any]:
        """running | stopped. Reclaim is DELETE, deliberately — it harvests
        memory before destroying, and a value in a dropdown should not be able
        to trigger that."""
        return self._request(
            "PATCH", f"/api/v1/placements/{pid}", json={"desired": desired}
        ).json()

    def reclaim_placement(self, pid: str) -> dict[str, Any]:
        """Ask for harvest-then-destroy. 202, not 200: the hub has recorded
        the intent, and the machine's edge does the work on its next pass."""
        return self._request("DELETE", f"/api/v1/placements/{pid}").json()

    # -- capsules: a whole SQUAD, frozen and placeable ------------------------

    def list_api_squads(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/squads").json()["squads"]

    def create_api_squad(self, name: str, description: str = "") -> dict[str, Any]:
        """Register a squad in the MANAGEMENT registry.

        Deliberately separate from messaging squads: `squad_members` decides
        who hears a broadcast, `api_squads` decides what the runtime can
        manage. A squad can exist for comms and be unknown here, which is
        why composing a capsule for a live squad can 404 — the members are
        there, the management row is not.
        """
        return self._request(
            "POST", "/api/v1/squads",
            json={"name": name, "description": description},
        ).json()

    def list_capsules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/capsules").json()["capsules"]

    def create_capsule(self, squad: str) -> dict[str, Any]:
        """Freeze a squad into a capsule: every seat's spec as it is NOW.

        Composition is a snapshot on purpose — placing the same capsule
        twice puts the same squad up twice, rather than whatever the roster
        happens to say at the second moment.
        """
        return self._request(
            "POST", "/api/v1/capsules", json={"squad": squad}
        ).json()

    def place_capsule(self, cid: str, machine: str) -> dict[str, Any]:
        """One docker placement PER SEAT on `machine`. Nothing runs yet —
        that machine's edge realizes them on its next pass."""
        return self._request(
            "POST", f"/api/v1/capsules/{cid}/place", json={"machine": machine}
        ).json()

    def delete_capsule(self, cid: str) -> dict[str, Any]:
        """Forget a frozen squad. The PLACEMENTS it made are untouched.

        A capsule is a snapshot, not a live link: `place` copies the manifest
        into per-seat placements and nothing refers back afterwards. So
        deleting one removes the ability to re-place THAT snapshot and changes
        nothing about anything already running — which is why this needs no
        cascade and no "placements first" gate, unlike `seats rm`.
        """
        return self._request("DELETE", f"/api/v1/capsules/{cid}").json()

    def machine_placements(self, machine: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/api/v1/machines/{machine}/placements"
        ).json()["placements"]

    def push_status(self, machine: str, payload: dict[str, Any]) -> None:
        self._request("POST", f"/api/v1/machines/{machine}/status", json=payload)

    def list_machines(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/machines").json()["machines"]

    def machine_agents(self) -> dict[str, list[dict[str, str]]]:
        """Each machine's reported roster: {machine: [{agent, worktree}]}.

        A machine ABSENT from this map has not reported one — an older edge,
        or one that has not run since it was upgraded. That is not the same
        claim as an empty roster, and callers must not collapse the two: the
        board falls back to matching by repo name for such a machine rather
        than showing it as having no agents.
        """
        got = self._request("GET", "/api/v1/machines").json().get("agents")
        return got if isinstance(got, dict) else {}

    def enrol_machine(
        self,
        name: str,
        os_name: str = "linux",
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enrol and return the machine record — INCLUDING its token.

        The token is returned exactly once, at creation: the hub stores only
        a hash and has no rotation endpoint, so a caller that drops it has
        destroyed it. Callers must persist before doing anything else.
        """
        return self._request(
            "POST",
            "/api/v1/machines",
            json={
                "name": name,
                "os": os_name,
                # A DICT, not a list: the PATCH handler does caps.update(...),
                # which raises on a list. The two machines enrolled by hand
                # during the 2026-07-30 rollout carry lists and will need
                # fixing before anyone PATCHes them.
                "capabilities": capabilities or {"worktree": True},
            },
        ).json()


def write_machine_token(token: str, dest: pathlib.Path | None = None) -> str:
    """Persist a machine token and return where it went.

    A separate function because the ORDER is the whole lesson: enrolment and
    rotation both return the token exactly once, and the hub keeps only a
    hash. Both machines in this fleet lost theirs to a shell pipeline that
    printed before saving. Persist first, print second — always.
    """
    dest = dest or MACHINE_TOKEN_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(token, encoding="utf-8")
    dest.chmod(0o600)
    return str(dest)


def _body_snippet(r: Any) -> str:
    try:
        return str(r.json())[:160]
    except Exception:  # noqa: BLE001
        return str(getattr(r, "text", ""))[:160]
