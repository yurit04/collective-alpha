#!/usr/bin/env python3
"""Enforce the collective-alpha dependency rules.

Rules
-----
1. Core packages (core/*) may depend on other core packages, but the graph
   must be acyclic (a DAG).
2. Projects (projects/*) may depend on core packages only.
3. No project depends on another project.
4. No core package depends on a project.
5. Code may only import another workspace package that its pyproject.toml
   declares, and only that package's public names (no `pkg._private`).
6. Demo notebooks: a project's `notebooks/<core-package>/` folder shows that core
   package, so its notebooks may import that package and the core packages it
   declares as dependencies (plus the project's own helpers), nothing else. Folders
   under `notebooks/` must be named after a core package.

Usage
-----
    python tools/check_architecture.py           # check, exit 1 on violations
    python tools/check_architecture.py --graph   # print the dependency graph
    python tools/check_architecture.py --hook    # Claude Code hook mode (exit 2)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIND_DIRS = {"core": "core", "project": "projects"}
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", "build", "dist", ".ipynb_checkpoints"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(req: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return normalize(match.group(1)) if match else None


@dataclass
class Member:
    name: str
    kind: str
    path: Path
    import_names: set[str] = field(default_factory=set)
    deps: set[str] = field(default_factory=set)


def _declared_deps(pyproject: dict) -> set[str]:
    reqs: list[str] = list(pyproject.get("project", {}).get("dependencies", []))
    for group in pyproject.get("project", {}).get("optional-dependencies", {}).values():
        reqs.extend(group)
    for group in pyproject.get("dependency-groups", {}).values():
        reqs.extend(r for r in group if isinstance(r, str))
    return {n for n in map(requirement_name, reqs) if n}


def _import_names(pkg_dir: Path) -> set[str]:
    src = pkg_dir / "src"
    base = src if src.is_dir() else pkg_dir
    names = set()
    for child in base.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            names.add(child.name)
        elif child.suffix == ".py" and base is src:
            names.add(child.stem)
    return names


def load_members(root: Path) -> dict[str, Member]:
    members: dict[str, Member] = {}
    for kind, dirname in KIND_DIRS.items():
        base = root / dirname
        if not base.is_dir():
            continue
        for pkg_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            pyproject_path = pkg_dir / "pyproject.toml"
            if not pyproject_path.exists():
                continue
            data = tomllib.loads(pyproject_path.read_text())
            name = normalize(data.get("project", {}).get("name", pkg_dir.name))
            members[name] = Member(
                name=name,
                kind=kind,
                path=pkg_dir,
                import_names=_import_names(pkg_dir),
                deps=_declared_deps(data),
            )
    return members


def _python_files(member: Member):
    for path in member.path.rglob("*.py"):
        if not SKIP_DIRS.intersection(path.relative_to(member.path).parts):
            yield path


def _is_private(part: str) -> bool:
    return part.startswith("_") and not (part.startswith("__") and part.endswith("__"))


def _imports(tree: ast.AST) -> list[tuple[int, str, list[str]]]:
    """(line, module, imported names) for every import statement in a parsed module."""
    out: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module, [a.name for a in node.names]))
    return out


def _notebook_code(path: Path) -> str:
    """The code cells of a notebook as one module; IPython magics and shell lines are dropped."""
    nb = json.loads(path.read_text())
    lines: list[str] = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        lines += [line for line in src.splitlines() if not line.lstrip().startswith(("%", "!"))]
        lines.append("")
    return "\n".join(lines)


def _notebook_errors(project: Member, members: dict[str, Member], owners: dict[str, Member], root: Path) -> list[str]:
    """Rule 6 for one project's notebooks/ folder."""
    errors: list[str] = []
    base = project.path / "notebooks"
    if not base.is_dir():
        return errors
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        shown = members.get(normalize(folder.name))
        rel_folder = folder.relative_to(root)
        if shown is None or shown.kind != "core":
            errors.append(f"{rel_folder}: notebook folders are named after a core package; "
                          f"'{folder.name}' is not one (packages: {', '.join(sorted(m for m in members if members[m].kind == 'core'))})")
            continue
        allowed = {shown.name} | {d for d in shown.deps if d in members} | {project.name}
        for nb in sorted(folder.rglob("*.ipynb")):
            if SKIP_DIRS.intersection(nb.relative_to(folder).parts):
                continue
            rel = nb.relative_to(root)
            try:
                tree = ast.parse(_notebook_code(nb), filename=str(nb))
            except (SyntaxError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{rel}: cannot parse its code cells ({exc})")
                continue
            for _, module, names in _imports(tree):
                parts = module.split(".")
                target = owners.get(parts[0])
                if target is None:
                    continue
                if target.name not in allowed:
                    errors.append(f"{rel}: imports '{parts[0]}', but notebooks under '{folder.name}' show "
                                  f"{shown.name} and may use only it and its dependencies "
                                  f"({', '.join(sorted(allowed - {project.name})) or 'none'})")
                if any(_is_private(p) for p in parts[1:]) or any(_is_private(n) for n in names):
                    errors.append(f"{rel}: imports a private part of '{target.name}' ({module}); use its public API")
    return errors


def find_cycles(members: dict[str, Member]) -> list[list[str]]:
    graph = {
        m.name: sorted(d for d in m.deps if d in members and members[d].kind == "core")
        for m in members.values()
        if m.kind == "core"
    }
    cycles, state, stack = [], {}, []

    def visit(node: str) -> None:
        state[node] = "active"
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == "active":
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = "done"

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return cycles


def check(root: Path = REPO_ROOT) -> list[str]:
    members = load_members(root)
    errors: list[str] = []

    # Declared dependencies.
    for m in members.values():
        for dep in sorted(m.deps & members.keys()):
            target = members[dep]
            if dep == m.name:
                continue
            if m.kind == "core" and target.kind == "project":
                errors.append(f"core package '{m.name}' depends on project '{dep}' (core must never depend on projects)")
            if m.kind == "project" and target.kind == "project":
                errors.append(f"project '{m.name}' depends on project '{dep}' (projects must be independent)")

    for cycle in find_cycles(members):
        errors.append("dependency cycle among core packages: " + " -> ".join(cycle))

    # Actual imports.
    owners = {imp: m for m in members.values() for imp in m.import_names}
    for m in members.values():
        for path in _python_files(m):
            rel = path.relative_to(root)
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError as exc:
                errors.append(f"{rel}: cannot parse ({exc.msg})")
                continue
            for lineno, module, names in _imports(tree):
                parts = module.split(".")
                target = owners.get(parts[0])
                if target is None or target is m:
                    continue
                where = f"{rel}:{lineno}"
                if target.name not in m.deps:
                    errors.append(f"{where}: imports '{parts[0]}' but '{m.name}' does not declare a dependency on '{target.name}'")
                if any(_is_private(p) for p in parts[1:]) or any(_is_private(n) for n in names):
                    errors.append(f"{where}: imports a private part of '{target.name}' ({module}); use its public API")

    # Demo notebooks (rule 6).
    for m in members.values():
        if m.kind == "project":
            errors += _notebook_errors(m, members, owners, root)
    return errors


def render_graph(root: Path = REPO_ROOT) -> str:
    members = load_members(root)
    if not members:
        return "(no workspace packages yet)"
    lines = []
    for kind in ("core", "project"):
        for m in sorted((x for x in members.values() if x.kind == kind), key=lambda x: x.name):
            deps = sorted(d for d in m.deps if d in members and d != m.name)
            lines.append(f"[{kind}] {m.name} -> {', '.join(deps) if deps else '(nothing in repo)'}")
    return "\n".join(lines)


def _hook_should_run() -> bool:
    """In hook mode, only run when the edited file can affect the rules."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return True
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    return not file_path or file_path.endswith((".py", ".ipynb", "pyproject.toml"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", action="store_true", help="print the dependency graph and exit")
    parser.add_argument("--hook", action="store_true", help="Claude Code hook mode: exit 2 so the agent sees violations")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.graph:
        print(render_graph(args.root))
        return 0
    if args.hook and not _hook_should_run():
        return 0

    errors = check(args.root)
    if not errors:
        if not args.hook:
            print("architecture OK: core packages form a DAG, projects depend only on core")
        return 0
    out = sys.stderr if args.hook else sys.stdout
    print("Architecture rule violations (see CLAUDE.md, 'Dependency rules'):", file=out)
    for err in errors:
        print(f"  - {err}", file=out)
    return 2 if args.hook else 1


if __name__ == "__main__":
    sys.exit(main())
