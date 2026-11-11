"""tools/reset.sh and tools/bootstrap.sh: syntax, option handling and dry runs against stores in
a temporary folder. Dry runs change nothing (no jobs, no stores, no downloads), so this runs
offline on macOS and on the Linux CI runner alike."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESET = ROOT / "tools" / "reset.sh"
BOOTSTRAP = ROOT / "tools" / "bootstrap.sh"
UNITS = sorted((ROOT / "tools" / "systemd").iterdir())
BASH = shutil.which("bash")
VARS = {"massive": "SUBSPACE_DATA_ROOT", "edgar": "EDGAR_DATA_ROOT", "finra": "FINRA_DATA_ROOT",
        "vinculum": "VINCULUM_DATA_ROOT", "nanoprobe": "NANOPROBE_DATA_ROOT", "queen": "BORG_QUEEN_DATA_ROOT",
        "conduit": "TRANSWARP_DATA_ROOT", "unicomplex": "UNICOMPLEX_DATA_ROOT"}

pytestmark = pytest.mark.skipif(not BASH or not shutil.which("uv"), reason="needs bash and uv")


def run(script: Path, *args: str, tmp: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, **{var: str(tmp / name) for name, var in VARS.items()}}
    return subprocess.run([BASH, str(script), *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)


@pytest.mark.parametrize("script", [RESET, BOOTSTRAP, ROOT / "tools" / "nightly_sync.sh"])
def test_syntax(script):
    assert subprocess.run([BASH, "-n", str(script)]).returncode == 0
    assert os.access(script, os.X_OK)


def test_reset_dry_run_lists_the_stores_and_deletes_nothing(tmp_path):
    (tmp_path / "massive" / "bars").mkdir(parents=True)
    (tmp_path / "massive" / "bars" / "x.parquet").write_bytes(b"0" * 4096)
    (tmp_path / "conduit").mkdir()
    res = run(RESET, "--dry-run", tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert re.search(r"massive .*" + re.escape(str((tmp_path / "massive").resolve())), out)
    assert "(absent)" in out and "kept: trading journal" in out
    assert "Dry run: nothing was stopped or deleted." in out
    assert (tmp_path / "massive" / "bars" / "x.parquet").exists() and (tmp_path / "conduit").exists()


def test_reset_with_nothing_there_says_clean(tmp_path):
    res = run(RESET, "--dry-run", "--include-journal", tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "already clean" in res.stdout


def test_reset_rejects_unknown_options(tmp_path):
    assert run(RESET, "--everything", tmp=tmp_path).returncode == 2


def test_bootstrap_dry_run_shows_every_step_in_order(tmp_path):
    res = run(BOOTSTRAP, "--dry-run", tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    order = ["subspace init", "cortical init", "subspace backfill initial", "subspace identity build",
             "cortical backfill", "cortical profiles", "sensor backfill short-interest",
             "sensor backfill short-volume", "vinculum build", "vinculum check", "nanoprobe build",
             "queen build", "collective status"]
    positions = [out.index(cmd) for cmd in order]
    assert positions == sorted(positions)
    assert not any((tmp_path / name).exists() for name in VARS)      # a dry run creates no store


def test_bootstrap_from_and_options(tmp_path):
    res = run(BOOTSTRAP, "--dry-run", "--from", "vinculum", "--no-minute", "--no-services", tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    steps = re.search(r"steps: (.*?)\s{3}", res.stdout).group(1).split()
    assert steps == ["setup", "vinculum", "queen", "status"]      # no nanoprobe without minute bars
    res = run(BOOTSTRAP, "--dry-run", "--only", "massive", "--since", "2015-01-01", "--no-minute", tmp=tmp_path)
    assert "subspace backfill initial --since 2015-01-01 --no-minute" in res.stdout
    assert "cortical backfill" not in res.stdout


def test_bootstrap_rejects_unknown_steps(tmp_path):
    res = run(BOOTSTRAP, "--dry-run", "--from", "borg", tmp=tmp_path)
    assert res.returncode == 1 and "unknown step" in res.stderr


def test_systemd_units_use_only_the_known_placeholders():
    assert [u.name for u in UNITS] == ["collective-stream.service", "collective-sync.service",
                                       "collective-sync.timer"]
    for unit in UNITS:
        assert set(re.findall(r"@[A-Z_]+@", unit.read_text())) <= {"@REPO@", "@RUN_TIME@"}, unit
