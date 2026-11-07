"""Small helpers are copied into each core package (so packages stay independent) and must
behave the same everywhere. Every `_day` in core/*/src is found and checked here, new copies
included."""

from __future__ import annotations

import ast
import importlib
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    (None, None),
    (date(2024, 1, 2), date(2024, 1, 2)),
    (datetime(2024, 1, 2, 15, 30), date(2024, 1, 2)),
    (pd.Timestamp("2024-01-02 15:30"), date(2024, 1, 2)),
    ("2024-01-02", date(2024, 1, 2)),
    ("2024-01-02T15:30:00", date(2024, 1, 2)),
]


def _copies(name: str) -> list[str]:
    """module paths (pkg.mod) of every top-level function `name` in core/*/src."""
    found = []
    for src in sorted(ROOT.glob("core/*/src")):
        for f in sorted(src.rglob("*.py")):
            tree = ast.parse(f.read_text())
            if any(isinstance(n, ast.FunctionDef) and n.name == name for n in tree.body):
                found.append(".".join(f.relative_to(src).with_suffix("").parts))
    return found


DAY = _copies("_day")


def test_day_copies_found():
    assert len(DAY) >= 10, DAY


@pytest.mark.parametrize("module", [*DAY, "massive_subspace_stream._dates:to_date"])
def test_day_copies_agree(module):
    mod, _, fn = module.partition(":")
    f = getattr(importlib.import_module(mod), fn or "_day")
    for given, want in CASES:
        got = f(given)
        assert got == want and type(got) is type(want), (module, given, got)
