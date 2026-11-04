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


def make_nb(root: Path, project: str, folder: str, name: str, code: str) -> None:
    import json

    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {},
          "cells": [{"cell_type": "markdown", "metadata": {}, "source": ["# demo"]},
                    {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                     "source": code.splitlines(keepends=True)}]}
    path = root / "projects" / project / "notebooks" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb))


def test_notebooks_follow_the_dag(ws):
    make_pkg(ws, "core", "model-core", deps=["feature-core"], code="import feature_core\n")
    make_pkg(ws, "projects", "probe", deps=["data-core", "feature-core", "model-core"])
    # feature-core's notebooks may use feature-core, data-core (its dependency) and the project's helpers.
    make_nb(ws, "probe", "feature-core", "00_tour.ipynb",
            "%load_ext autoreload\nimport feature_core\nfrom data_core import thing\nimport probe\n!ls\n")
    assert arch.check(ws) == []
    # ... but not model-core, which depends on it.
    make_nb(ws, "probe", "feature-core", "01_bad.ipynb", "from model_core import Model\n")
    errors = arch.check(ws)
    assert any("01_bad.ipynb" in e and "may use only it and its dependencies" in e for e in errors)
    # Private parts are off limits in notebooks too, and folders must be named after a core package.
    make_nb(ws, "probe", "feature-core", "02_private.ipynb", "from data_core._internal import x\n")
    make_nb(ws, "probe", "misc", "00.ipynb", "import data_core\n")
    errors = arch.check(ws)
    assert any("02_private.ipynb" in e and "private part" in e for e in errors)
    assert any("notebooks/misc" in e and "named after a core package" in e for e in errors)
