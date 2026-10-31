"""Small helpers shared by the subspace-probe notebooks."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["human_bytes", "style", "timed"]


def style() -> None:
    """Consistent, readable matplotlib defaults for the notebooks."""
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.figsize": (11, 4.5),
        "figure.dpi": 100,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titleweight": "bold",
        "axes.titlesize": 12,
        "legend.frameon": False,
    })


@contextmanager
def timed(label: str) -> Iterator[None]:
    """Print how long a block took: `with timed("load"): ...`."""
    start = time.perf_counter()
    yield
    print(f"{label}: {time.perf_counter() - start:.2f} s")


def human_bytes(n: float) -> str:
    """1536 -> '1.5 KB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
