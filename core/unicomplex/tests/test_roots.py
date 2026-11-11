"""`collective roots`: every store's root, as each package resolves it."""

from __future__ import annotations

import json

from typer.testing import CliRunner
from unicomplex import roots
from unicomplex.cli import app

VARS = ("SUBSPACE_DATA_ROOT", "EDGAR_DATA_ROOT", "FINRA_DATA_ROOT", "VINCULUM_DATA_ROOT",
        "NANOPROBE_DATA_ROOT", "BORG_QUEEN_DATA_ROOT", "TRANSWARP_DATA_ROOT", "UNICOMPLEX_DATA_ROOT")


def _point_everything_at(tmp_path, monkeypatch):
    for var in VARS:
        monkeypatch.setenv(var, str(tmp_path / var.lower()))


def test_every_store_follows_its_own_variable(tmp_path, monkeypatch):
    _point_everything_at(tmp_path, monkeypatch)
    found = roots()
    assert [r.name for r in found] == ["massive", "edgar", "finra", "vinculum", "nanoprobe", "queen",
                                       "conduit", "unicomplex"]
    assert {r.env for r in found} == set(VARS)
    for r in found:
        assert r.path == (tmp_path / r.env.lower()).resolve()
        assert not r.path.exists()                      # resolving creates nothing
    assert {r.name: r.kind for r in found}["conduit"] == "journal"
    assert {r.kind for r in found if r.name != "conduit"} == {"data"}


def test_defaults_live_under_market_data(monkeypatch, tmp_path):
    for var in VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLLECTIVE_CONFIG", str(tmp_path / "collective.toml"))
    (tmp_path / "collective.toml").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    for r in roots():
        assert r.path.parent == (tmp_path / "market_data").resolve(), r


def test_cli_prints_tab_separated_lines_and_json(tmp_path, monkeypatch):
    _point_everything_at(tmp_path, monkeypatch)
    (tmp_path / "subspace_data_root").mkdir()
    res = CliRunner().invoke(app, ["roots"])
    assert res.exit_code == 0, res.output
    lines = [line.split("\t") for line in res.output.splitlines()]
    assert lines[0] == ["massive", "data", "SUBSPACE_DATA_ROOT", str((tmp_path / "subspace_data_root").resolve())]
    assert len(lines) == len(VARS)
    res = CliRunner().invoke(app, ["roots", "--json"])
    rows = json.loads(res.output)
    assert rows[0]["exists"] is True and rows[1]["exists"] is False
