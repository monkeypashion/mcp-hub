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

    def push_status(self, machine: str, payload: dict[str, Any]) -> None:
        self._request("POST", f"/api/v1/machines/{machine}/status", json=payload)

    def list_machines(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/machines").json()["machines"]

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


def _body_snippet(r: Any) -> str:
    try:
        return str(r.json())[:160]
    except Exception:  # noqa: BLE001
        return str(getattr(r, "text", ""))[:160]
