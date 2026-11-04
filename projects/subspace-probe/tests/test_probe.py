import json
from pathlib import Path

import pytest
from subspace_probe import SECTORS, human_bytes, sic_sector, timed

NOTEBOOKS = sorted(p for p in (Path(__file__).parents[1] / "notebooks").rglob("*.ipynb")
                   if ".ipynb_checkpoints" not in p.parts)


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(3 * 1024**3) == "3.0 GB"


def test_sic_sector():
    assert sic_sector("3571") == "Business equipment"  # Apple
    assert sic_sector(6021) == "Finance"  # JPMorgan
    assert sic_sector("2834") == "Healthcare"  # pharma
    assert sic_sector("1311") == "Energy"
    assert sic_sector("4911") == "Utilities"
    assert sic_sector("1000") == "Other"  # metal mining
    assert sic_sector(None) is None and sic_sector("") is None
    assert len(SECTORS) == 12


def test_timed(capsys):
    with timed("block"):
        pass
    assert capsys.readouterr().out.startswith("block: ")


def test_there_are_notebooks():
    assert NOTEBOOKS


CORE = sorted(p.name for p in (Path(__file__).parents[3] / "core").iterdir() if (p / "pyproject.toml").exists())


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_notebook_is_valid_executed_and_in_a_module_folder(path):
    nb = json.loads(path.read_text())
    assert nb["nbformat"] == 4
    # One folder per core module, named after the package (the import rules themselves are
    # checked by tools/check_architecture.py, rule 6).
    assert path.parent.name in CORE, f"{path.parent.name} is not a core package"
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    # Committed with outputs: every code cell has run, and none ended in an error.
    assert all(c.get("execution_count") for c in code), "a code cell was never executed"
    assert not any(o.get("output_type") == "error" for c in code for o in c.get("outputs", [])), "a cell errored"
    assert any(c.get("outputs") for c in code), "no outputs at all"
