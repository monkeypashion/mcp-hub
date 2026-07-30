"""Gate for `mcp-hub machines` — enrolment, and not losing the token.

Enrolment had no verb, so the 2026-07-30 rollout used raw curl and the machine
token — returned exactly once, stored only as a hash, with no rotation
endpoint — went into a shell pipeline and was destroyed. Both machines are now
permanently tokenless, and `POST` collides on archived rows so the names
cannot even be re-enrolled.

So the load-bearing test here is not "does it enrol". It is "is the token on
disk before anything else happens to it".
"""

from __future__ import annotations

import argparse
import json

import pytest

from mcp_hub import cli
from mcp_hub.operator_api import ApiUnavailable


class _FakeApi:
    def __init__(self, machines=None, token="tok-abc123", fail=None):
        self._machines = machines or []
        self._token = token
        self._fail = fail
        self.enrolled = []

    def list_machines(self):
        if self._fail:
            raise self._fail
        return self._machines

    def enrol_machine(self, name, os_name="linux", capabilities=None):
        if self._fail:
            raise self._fail
        self.enrolled.append((name, os_name, capabilities))
        rec = {"name": name, "os": os_name, "capabilities": {"worktree": True},
               "last_seen": None}
        if self._token is not None:
            rec["token"] = self._token
        return rec


def _args(tmp_path, **kw):
    base = dict(action="enrol", name="box-1", os="linux",
                token_file=str(tmp_path / "machine.token"), force=False,
                print_token=False, hub_url="http://h/mcp", json=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestEnrol:
    def test_the_token_lands_on_disk_at_0600(self, tmp_path, capsys):
        api = _FakeApi(token="s3cret-token")
        dest = tmp_path / "machine.token"
        rc = cli.machines_command(_args(tmp_path), api=api)
        assert rc == 0
        assert dest.read_text() == "s3cret-token"
        assert oct(dest.stat().st_mode)[-3:] == "600"

    def test_the_token_is_NOT_printed_unless_asked(self, tmp_path, capsys):
        cli.machines_command(_args(tmp_path), api=_FakeApi(token="s3cret-token"))
        out = capsys.readouterr().out
        assert "s3cret-token" not in out
        assert "sha256" in out                    # a fingerprint instead
        assert "cannot be retrieved later" in out

    def test_print_token_shows_it(self, tmp_path, capsys):
        cli.machines_command(
            _args(tmp_path, print_token=True), api=_FakeApi(token="s3cret-token")
        )
        assert "s3cret-token" in capsys.readouterr().out

    def test_an_existing_token_file_is_never_clobbered_by_default(
        self, tmp_path, capsys
    ):
        """That file may be the only copy in existence."""
        dest = tmp_path / "machine.token"
        dest.write_text("the-only-copy")
        api = _FakeApi(token="new-one")
        rc = cli.machines_command(_args(tmp_path), api=api)
        assert rc == 1
        assert dest.read_text() == "the-only-copy"
        assert api.enrolled == []                 # and nothing was enrolled
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_force_overwrites(self, tmp_path):
        dest = tmp_path / "machine.token"
        dest.write_text("old")
        cli.machines_command(_args(tmp_path, force=True), api=_FakeApi(token="new"))
        assert dest.read_text() == "new"

    def test_a_tokenless_response_is_a_loud_failure_not_a_silent_success(
        self, tmp_path, capsys
    ):
        """If the hub ever stops returning it, that must not read as success —
        the token cannot be requested again."""
        rc = cli.machines_command(_args(tmp_path), api=_FakeApi(token=None))
        assert rc == 1
        assert not (tmp_path / "machine.token").exists()
        assert "cannot be requested again" in capsys.readouterr().err

    def test_already_enrolled_explains_that_the_token_is_unrecoverable(
        self, tmp_path, capsys
    ):
        api = _FakeApi(fail=ApiUnavailable("hub API error 409 on /api/v1/machines: "
                                           "machine 'box-1' already enrolled"))
        rc = cli.machines_command(_args(tmp_path), api=api)
        assert rc == 1
        err = capsys.readouterr().err
        assert "already enrolled" in err
        assert "no rotation endpoint" in err

    def test_capabilities_default_to_a_dict_the_patch_handler_can_update(
        self, tmp_path
    ):
        """PATCH does caps.update(...), which raises on a list. The rollout's
        hand-rolled curl passed a list; the verb must not repeat it."""
        from mcp_hub.operator_api import OperatorApi

        sent = {}

        class _Client:
            def request(self, method, url, **kw):
                sent.update(kw.get("json") or {})
                class R:
                    status_code = 201
                    def json(self_inner):
                        return {"name": "box-1", "token": "t"}
                return R()

        OperatorApi("http://h", token="t", client=_Client()).enrol_machine("box-1")
        assert isinstance(sent["capabilities"], dict)


class TestList:
    def test_list_names_each_machine(self, tmp_path, capsys):
        api = _FakeApi(machines=[
            {"name": "dev-vm-1", "os": "linux", "last_seen": None},
            {"name": "fireblade-wsl", "os": "linux", "last_seen": None},
        ])
        rc = cli.machines_command(_args(tmp_path, action="list"), api=api)
        assert rc == 0
        out = capsys.readouterr().out
        assert "dev-vm-1" in out and "fireblade-wsl" in out
        assert "never seen" in out

    def test_empty_says_so(self, tmp_path, capsys):
        rc = cli.machines_command(_args(tmp_path, action="list"), api=_FakeApi())
        assert rc == 0
        assert "no machines enrolled" in capsys.readouterr().out

    def test_a_disabled_api_prints_the_reason(self, tmp_path, capsys):
        api = _FakeApi(fail=ApiUnavailable("the hub's management API is disabled"))
        rc = cli.machines_command(_args(tmp_path, action="list"), api=api)
        assert rc == 1
        assert "management API is disabled" in capsys.readouterr().err

    def test_json_output(self, tmp_path, capsys):
        api = _FakeApi(machines=[{"name": "a", "os": "linux", "last_seen": None}])
        cli.machines_command(_args(tmp_path, action="list", json=True), api=api)
        assert json.loads(capsys.readouterr().out)[0]["name"] == "a"


def test_the_verb_is_reachable_through_both_registries(capsys):
    """The CLI parser and server._CLI_SUBCOMMANDS must both know it — this is
    exactly what caught `workspaces` before it shipped unreachable."""
    from mcp_hub.server import _CLI_SUBCOMMANDS

    parser_names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert "machines" in parser_names
    assert "machines" in _CLI_SUBCOMMANDS

    with pytest.raises(SystemExit) as e:
        cli.main(["machines", "--help"])
    assert e.value.code == 0
    assert "enrol" in capsys.readouterr().out
