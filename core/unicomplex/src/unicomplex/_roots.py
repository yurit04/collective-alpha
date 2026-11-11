"""Every store's data root, resolved through each package's own public settings.

`collective roots` prints them for tools/reset.sh and tools/bootstrap.sh, so the scripts follow
collective.toml and the `*_DATA_ROOT` variables exactly as the CLIs do and never hard-code
~/market_data. Only reads settings; creates nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ._config import ENV_ROOT, load_settings


@dataclass(frozen=True)
class Root:
    name: str      # massive, edgar, finra, vinculum, nanoprobe, queen, conduit, unicomplex
    kind: str      # "data" (rebuildable from the sources) or "journal" (order sheets and fills)
    env: str       # the variable that moves it
    path: Path

    def to_dict(self) -> dict[str, str | bool]:
        return {"name": self.name, "kind": self.kind, "env": self.env, "path": str(self.path),
                "exists": self.path.exists()}


def roots() -> list[Root]:
    """Every store, in the order the nightly run fills them, then the journal and the run log."""
    import borg_queen
    import edgar_cortical_node
    import finra_sensor_array
    import massive_subspace_stream
    import nanoprobe_array
    import transwarp_conduit
    import vinculum

    def env_or(var: str, default: str) -> Path:   # packages without a settings file
        return Path(os.environ.get(var) or default)

    found = [
        ("massive", "data", "SUBSPACE_DATA_ROOT", massive_subspace_stream.load_settings().data_root),
        ("edgar", "data", "EDGAR_DATA_ROOT", edgar_cortical_node.load_settings().data_root),
        ("finra", "data", "FINRA_DATA_ROOT", finra_sensor_array.load_settings().data_root),
        ("vinculum", "data", "VINCULUM_DATA_ROOT", vinculum.load_settings().data_root),
        ("nanoprobe", "data", nanoprobe_array.ENV_ROOT,
         env_or(nanoprobe_array.ENV_ROOT, "~/market_data/nanoprobe_array")),
        ("queen", "data", borg_queen.ENV_ROOT, env_or(borg_queen.ENV_ROOT, "~/market_data/borg_queen")),
        ("conduit", "journal", transwarp_conduit.ENV_ROOT, transwarp_conduit.load_settings().data_root()),
        ("unicomplex", "data", ENV_ROOT, load_settings().data_root),
    ]
    return [Root(name, kind, env, Path(path).expanduser().resolve()) for name, kind, env, path in found]
