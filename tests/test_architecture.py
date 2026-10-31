"""The repo's dependency rules hold, and the checker catches each kind of violation."""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_architecture", ROOT / "tools" / "check_architecture.py")
arch = importlib.util.module_from_spec(_spec)
sys.modules["check_architecture"] = arch  # dataclasses look their module up here
_spec.loader.exec_module(arch)


def test_repo_follows_the_rules():
    assert arch.check(ROOT) == []


def make_pkg(root: Path, kind_dir: str, name: str, deps=(), code: str = "") -> None:
    pkg = root / kind_dir / name
    module = name.replace("-", "_")
    (pkg / "src" / module).mkdir(parents=True)
    dep_list = ", ".join(f'"{d}"' for d in deps)
    (pkg / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0"\ndependencies = [{dep_list}]\n')
    (pkg / "src" / module / "__init__.py").write_text(textwrap.dedent(code))


@pytest.fixture
def ws(tmp_path):
    make_pkg(tmp_path, "core", "data-core")
    make_pkg(tmp_path, "core", "feature-core", deps=["data-core", "polars>=1"], code="import data_core\n")
    return tmp_path


def test_valid_workspace(ws):
    make_pkg(ws, "projects", "alpha-one", deps=["feature-core"], code="from feature_core import thing\n")
    assert arch.check(ws) == []
    assert "[core] feature-core -> data-core" in arch.render_graph(ws)


def test_core_cycle(ws):
    (ws / "core" / "data-core" / "pyproject.toml").write_text(
        '[project]\nname = "data-core"\nversion = "0"\ndependencies = ["feature-core"]\n')
    assert any("cycle" in e for e in arch.check(ws))


def test_project_depends_on_project(ws):
    make_pkg(ws, "projects", "alpha-one")
    make_pkg(ws, "projects", "alpha-two", deps=["alpha-one"])
    assert any("projects must be independent" in e for e in arch.check(ws))


def test_core_depends_on_project(ws):
    make_pkg(ws, "projects", "alpha-one")
    make_pkg(ws, "core", "bad-core", deps=["alpha-one"])
    assert any("core must never depend on projects" in e for e in arch.check(ws))


def test_undeclared_import(ws):
    make_pkg(ws, "projects", "alpha-one", deps=["feature-core"], code="import data_core\n")
    errors = arch.check(ws)
    assert any("does not declare a dependency on 'data-core'" in e for e in errors)


def test_private_import(ws):
    make_pkg(ws, "projects", "alpha-one", deps=["data-core"],
             code="from data_core._internal import x\nfrom data_core import _hidden\n")
    assert sum("private part" in e for e in arch.check(ws)) == 2


def test_dunder_import_is_fine(ws):
    make_pkg(ws, "projects", "alpha-one", deps=["data-core"], code="from data_core import __version__\n")
    assert arch.check(ws) == []
