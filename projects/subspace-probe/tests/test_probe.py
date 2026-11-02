import json
from pathlib import Path

import pytest
from subspace_probe import SECTORS, human_bytes, sic_sector, timed

NOTEBOOKS = sorted((Path(__file__).parents[1] / "notebooks").rglob("*.ipynb"))


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


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_valid_and_uses_public_api(path):
    nb = json.loads(path.read_text())
    assert nb["nbformat"] == 4
    code = "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
    # Notebooks read data through the public API only, never private modules.
    assert "massive_subspace_stream._" not in code
    assert "_store import" not in code
