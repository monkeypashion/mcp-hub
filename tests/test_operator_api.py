"""Gate for operator_api — one honest failure per cause.

The bug this exists to prevent shipped once and was shown to the operator:
with no token configured, httpx refused to build the `Bearer ` header, the
request never left the box, and the manager printed "hub registry unreachable
(Illegal header value b'Bearer ')". Every word of that is wrong except
"registry" — the hub was up, and the fix was on the local disk.

So each test here pins a DIFFERENT cause to a DIFFERENT sentence, and one
pins the behaviour underneath: with no token, nothing is sent at all.
"""

from __future__ import annotations

import pathlib

import pytest

from mcp_hub.operator_api import (
    ApiUnavailable,
    OperatorApi,
    api_base,
    resolve_token,
)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _Client:
    """Records every request so 'nothing was sent' is provable, not assumed."""

    def __init__(self, resp=None, raises=None):
        self.calls = []
        self._resp = resp or _Resp()
        self._raises = raises

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self._raises is not None:
            raise self._raises
        return self._resp


def _api(**kw):
    kw.setdefault("token", "t0ken")
    return OperatorApi("http://hub.example", **kw)


class TestTokenResolution:
    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        f = tmp_path / "api.token"
        f.write_text("from-file")
        monkeypatch.setenv("MCP_HUB_API_TOKEN", "from-env")
        assert resolve_token(f) == "from-env"

    def test_falls_back_to_the_file(self, tmp_path, monkeypatch):
        f = tmp_path / "api.token"
        f.write_text("  from-file\n")
        monkeypatch.delenv("MCP_HUB_API_TOKEN", raising=False)
        assert resolve_token(f) == "from-file"

    def test_missing_file_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_HUB_API_TOKEN", raising=False)
        assert resolve_token(tmp_path / "nope") == ""

    def test_api_base_strips_the_mcp_endpoint(self):
        assert api_base("http://h:8090/mcp") == "http://h:8090"


class TestOneFailurePerCause:
    def test_no_token_names_the_file_and_sends_NOTHING(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_HUB_API_TOKEN", raising=False)
        client = _Client()
        api = OperatorApi(
            "http://hub.example", client=client, token_file=tmp_path / "absent"
        )
        with pytest.raises(ApiUnavailable) as e:
            api.get_registry()
        assert "no hub API token on this machine" in str(e.value)
        # The load-bearing half: the old code got as far as the transport,
        # which is why the message described a header instead of a credential.
        assert client.calls == []

    def test_hub_with_the_api_switched_off_says_so_and_names_the_var(self):
        api = _api(client=_Client(_Resp(503, {"detail": "management API disabled"})))
        with pytest.raises(ApiUnavailable) as e:
            api.get_registry()
        msg = str(e.value)
        assert "management API is disabled" in msg
        assert "MCP_HUB_API_TOKEN is not set on the hub" in msg
        # It must NOT read as an outage — the hub answered.
        assert "unreachable" not in msg

    def test_rejected_token_is_not_reported_as_unreachable(self):
        api = _api(client=_Client(_Resp(401, {"detail": "nope"})))
        with pytest.raises(ApiUnavailable) as e:
            api.get_registry()
        assert "rejected this machine's API token (401)" in str(e.value)
        assert "unreachable" not in str(e.value)

    def test_a_real_transport_failure_IS_unreachable(self):
        api = _api(client=_Client(raises=OSError("connection refused")))
        with pytest.raises(ApiUnavailable) as e:
            api.get_registry()
        msg = str(e.value)
        assert "unreachable" in msg
        assert "http://hub.example" in msg      # names WHERE it tried
        assert "connection refused" in msg

    def test_other_4xx_carries_the_status_and_body(self):
        api = _api(client=_Client(_Resp(422, {"detail": "name required"})))
        with pytest.raises(ApiUnavailable) as e:
            api.create_workspace("")
        assert "422" in str(e.value)
        assert "name required" in str(e.value)


class TestCalls:
    def test_registry_get_targets_the_right_path_with_bearer(self):
        client = _Client(_Resp(200, {"definitions": [], "discovered": []}))
        _api(client=client).get_registry()
        method, url, kw = client.calls[0]
        assert method == "GET"
        assert url == "http://hub.example/api/v1/workspace-registry"
        assert kw["headers"]["Authorization"] == "Bearer t0ken"

    def test_create_workspace_posts_the_whole_definition(self):
        client = _Client(_Resp(201, {"id": 1, "name": "runtime"}))
        _api(client=client).create_workspace(
            "runtime", machine="dev-vm-1", squad="runtime", listings=["/a", "/b"]
        )
        method, url, kw = client.calls[0]
        assert (method, url) == ("POST", "http://hub.example/api/v1/workspaces")
        assert kw["json"] == {
            "name": "runtime", "machine": "dev-vm-1",
            "squad": "runtime", "listings": ["/a", "/b"],
        }

    def test_push_status_targets_the_named_machine(self):
        client = _Client()
        _api(client=client).push_status("fireblade-wsl", {"workspace_open": "/w.x"})
        method, url, kw = client.calls[0]
        assert method == "POST"
        assert url == "http://hub.example/api/v1/machines/fireblade-wsl/status"
        assert kw["json"] == {"workspace_open": "/w.x"}

    def test_base_url_trailing_slash_does_not_double(self):
        client = _Client(_Resp(200, {"workspaces": []}))
        OperatorApi("http://hub.example/", token="t", client=client).list_workspaces()
        assert client.calls[0][1] == "http://hub.example/api/v1/workspaces"


def test_token_file_constant_points_at_the_per_machine_location():
    from mcp_hub.operator_api import TOKEN_FILE

    assert TOKEN_FILE == pathlib.Path.home() / ".mcp-hub" / "api.token"
