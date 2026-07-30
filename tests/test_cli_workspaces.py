"""Gate for `mcp-hub workspaces` — the registry's missing write half.

Registration is what makes ABSENCE mean something: until a workspace is
defined on the hub, "not registered" is the state of every workspace in the
fleet, so the manager's drift column can only ever say the same thing about
everything. These tests pin that registering is idempotent (re-running is
not a duplicate), that --dry-run writes nothing, and that a hub which cannot
answer produces the honest sentence rather than a traceback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from mcp_hub import cli
from mcp_hub.operator_api import ApiUnavailable


class _FakeApi:
    def __init__(self, existing=None, fail_on_create=None, fail_on_list=None):
        self.existing = list(existing or [])
        self.created = []
        self._fail_on_create = fail_on_create
        self._fail_on_list = fail_on_list

    def list_workspaces(self):
        if self._fail_on_list:
            raise self._fail_on_list
        return self.existing

    def create_workspace(self, name, machine="", squad="", listings=None):
        if self._fail_on_create and name in self._fail_on_create:
            raise ApiUnavailable(self._fail_on_create[name])
        rec = {"name": name, "machine": machine, "squad": squad,
               "listings": listings or []}
        self.created.append(rec)
        return rec

    def get_registry(self):
        return {"definitions": [], "discovered": []}


def _ws(tmp_path: Path, name: str, folders: int = 1) -> Path:
    f = tmp_path / f"{name}.code-workspace"
    f.write_text(json.dumps(
        {"folders": [{"path": f"/repo/{name}{i}"} for i in range(folders)]}
    ))
    return f


def _args(**kw):
    base = dict(action="register", paths=[], all=False, squad="",
                machine="here", hub_url="http://h/mcp", dry_run=False,
                json=False, scan_dir=None)
    base.update(kw)
    return argparse.Namespace(**base)


class TestRegister:
    def test_all_registers_every_discovered_workspace_with_its_folders(
        self, tmp_path, capsys
    ):
        _ws(tmp_path, "squad", 3)
        _ws(tmp_path, "runtime", 2)
        api = _FakeApi()
        rc = cli.workspaces_command(
            _args(all=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 0
        by_name = {c["name"]: c for c in api.created}
        assert set(by_name) == {"squad", "runtime"}
        assert by_name["squad"]["listings"] == [
            "/repo/squad0", "/repo/squad1", "/repo/squad2"
        ]
        assert by_name["squad"]["machine"] == "here"
        assert "2 registered" in capsys.readouterr().out

    def test_rerunning_registers_nothing_and_says_so(self, tmp_path, capsys):
        _ws(tmp_path, "squad")
        api = _FakeApi(existing=[{"name": "squad", "machine": "here"}])
        rc = cli.workspaces_command(
            _args(all=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 0
        assert api.created == []
        out = capsys.readouterr().out
        assert "already registered, left alone: squad" in out

    def test_a_machineless_definition_already_covers_this_machine(
        self, tmp_path, capsys
    ):
        """A fleet-wide definition satisfies every machine's row, so
        registering again would silently duplicate what already applies."""
        _ws(tmp_path, "squad")
        api = _FakeApi(existing=[{"name": "squad", "machine": ""}])
        cli.workspaces_command(_args(all=True, scan_dir=[str(tmp_path)]), api=api)
        assert api.created == []

    def test_dry_run_writes_NOTHING_but_reports_what_it_would_do(
        self, tmp_path, capsys
    ):
        _ws(tmp_path, "squad", 2)
        api = _FakeApi()
        rc = cli.workspaces_command(
            _args(all=True, dry_run=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 0
        assert api.created == []
        out = capsys.readouterr().out
        assert "would register: squad (2 folder(s))" in out
        assert "1 would register" in out

    def test_named_paths_register_just_those(self, tmp_path):
        _ws(tmp_path, "squad")
        keep = _ws(tmp_path, "runtime")
        api = _FakeApi()
        cli.workspaces_command(
            _args(paths=[str(keep)], scan_dir=[str(tmp_path)]), api=api
        )
        assert [c["name"] for c in api.created] == ["runtime"]

    def test_a_named_path_that_does_not_exist_refuses(self, tmp_path, capsys):
        api = _FakeApi()
        rc = cli.workspaces_command(
            _args(paths=[str(tmp_path / "ghost.code-workspace")]), api=api
        )
        assert rc == 1
        assert api.created == []
        assert "no such workspace file" in capsys.readouterr().err

    def test_neither_paths_nor_all_refuses_rather_than_guessing(self, capsys):
        api = _FakeApi()
        rc = cli.workspaces_command(_args(), api=api)
        assert rc == 1
        assert api.created == []
        assert "or pass --all" in capsys.readouterr().err

    def test_squad_typing_rides_along(self, tmp_path):
        _ws(tmp_path, "runtime")
        api = _FakeApi()
        cli.workspaces_command(
            _args(all=True, squad="runtime", scan_dir=[str(tmp_path)]), api=api
        )
        assert api.created[0]["squad"] == "runtime"

    def test_an_unparseable_workspace_still_registers_with_no_listings(
        self, tmp_path
    ):
        """The registry's whole point is never losing track of a file. A
        workspace with a broken JSONC body is exactly the one worth having
        a definition for."""
        bad = tmp_path / "broken.code-workspace"
        bad.write_text("{ this is not json")
        api = _FakeApi()
        cli.workspaces_command(_args(all=True, scan_dir=[str(tmp_path)]), api=api)
        assert api.created[0]["name"] == "broken"
        assert api.created[0]["listings"] == []

    def test_jsonc_comments_do_not_break_listing_extraction(self, tmp_path):
        f = tmp_path / "commented.code-workspace"
        f.write_text('{\n  // the squad\n  "folders": [{"path": "/a"}]\n}')
        api = _FakeApi()
        cli.workspaces_command(_args(all=True, scan_dir=[str(tmp_path)]), api=api)
        assert api.created[0]["listings"] == ["/a"]

    def test_an_empty_machine_reports_nothing_to_do_and_succeeds(
        self, tmp_path, capsys
    ):
        api = _FakeApi()
        rc = cli.workspaces_command(
            _args(all=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 0
        assert "nothing to register" in capsys.readouterr().out


class TestHonestFailure:
    def test_an_unconfigured_hub_prints_the_reason_not_a_traceback(
        self, tmp_path, capsys
    ):
        _ws(tmp_path, "squad")
        api = _FakeApi(fail_on_list=ApiUnavailable(
            "the hub's management API is disabled"
            " (MCP_HUB_API_TOKEN is not set on the hub)"
        ))
        rc = cli.workspaces_command(
            _args(all=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 1
        assert "management API is disabled" in capsys.readouterr().err

    def test_one_failed_create_does_not_hide_the_others_or_the_exit_code(
        self, tmp_path, capsys
    ):
        _ws(tmp_path, "aaa")
        _ws(tmp_path, "bbb")
        api = _FakeApi(fail_on_create={"bbb": "hub rejected it"})
        rc = cli.workspaces_command(
            _args(all=True, scan_dir=[str(tmp_path)]), api=api
        )
        assert rc == 1                       # a partial success is not success
        assert [c["name"] for c in api.created] == ["aaa"]
        cap = capsys.readouterr()
        assert "registered: aaa" in cap.out
        assert "FAILED bbb" in cap.err
        assert "1 registered" in cap.out and "1 failed" in cap.out


class TestList:
    def test_list_renders_the_three_columns_and_the_note(self, tmp_path, capsys):
        _ws(tmp_path, "squad")

        class _Off(_FakeApi):
            def get_registry(self):
                raise ApiUnavailable("no hub API token on this machine")

        rc = cli.workspaces_command(
            _args(action="list", scan_dir=[str(tmp_path)]), api=_Off()
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "no hub API token on this machine — local scan only" in out
        assert "squad" in out
        assert "? hub" in out          # unknown, NOT "unregistered"
        assert "✔ disk" in out

    def test_list_json_is_the_whole_model(self, tmp_path, capsys):
        _ws(tmp_path, "squad")
        rc = cli.workspaces_command(
            _args(action="list", json=True, scan_dir=[str(tmp_path)]),
            api=_FakeApi(),
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["rows"][0]["name"] == "squad"
        assert data["hub_reachable"] is True


def test_the_verb_is_wired_into_the_parser_and_dispatches(monkeypatch, capsys):
    """A command nothing can reach is not shipped.

    No `if` here on purpose: an earlier draft skipped its own assertions
    when a helper happened to exist, which is how a test passes while
    proving nothing.
    """
    with pytest.raises(SystemExit) as e:
        cli.main(["workspaces", "--help"])
    assert e.value.code == 0
    assert "register" in capsys.readouterr().out

    seen = {}
    monkeypatch.setattr(
        cli, "workspaces_command", lambda args: seen.setdefault("action", args.action)
    )
    cli.main(["workspaces", "list"])
    assert seen["action"] == "list"
