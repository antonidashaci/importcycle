#!/usr/bin/env python3
"""Find circular imports, layering violations and dead modules in a package.

A circular import is not one bug, it is two. The loop that fails at startup is
the easy one -- you see it immediately. The one that *works*, because somebody
moved the import inside a function to break the cycle, is the one that fails six
months later when a different module imports things in a different order.

This builds the import graph with `ast` -- no imports are executed -- and tells
the two apart: which cycles run at import time, and which only exist through a
deferred or `TYPE_CHECKING` import and are waiting for someone to tidy them up.
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# An edge that runs when the module is imported is what breaks a build.
RUNTIME = "module"
DEFERRED = "deferred"   # inside a function or method: runs on first call
TYPING = "typing"       # under `if TYPE_CHECKING`: never runs


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    line: int


@dataclass
class Module:
    name: str
    path: Path
    edges: list[Edge] = field(default_factory=list)


class _Visitor(ast.NodeVisitor):
    """Collect import edges, tracking whether each one actually runs."""

    def __init__(self, module: str, package: str, is_package: bool,
                 known: set[str]) -> None:
        self.module = module
        self.package = package
        self.is_package = is_package
        self.known = known
        self.edges: list[Edge] = []
        self.depth = 0          # > 0 means inside a function body
        self.type_only = False  # inside `if TYPE_CHECKING`

    # -- where does `from . import x` point? ---------------------------------

    def _base(self, level: int) -> str | None:
        """The package `level` dots up from this module."""
        parts = self.module.split(".")
        if not self.is_package:
            parts = parts[:-1]
        if level > 1:
            parts = parts[: len(parts) - (level - 1)]
        if len(parts) < 0:
            return None
        return ".".join(parts)

    def _resolve(self, dotted: str) -> str | None:
        """Longest prefix of `dotted` that is a module we actually have.

        `import pkg.a.b` reaches `pkg.a.b`, but `from pkg.a import thing`
        reaches `pkg.a` -- the name may be an attribute, not a module.
        """
        parts = dotted.split(".")
        for stop in range(len(parts), 0, -1):
            candidate = ".".join(parts[:stop])
            if candidate in self.known:
                return candidate
        return None

    def _kind(self) -> str:
        if self.type_only:
            return TYPING
        return DEFERRED if self.depth else RUNTIME

    def _add(self, dotted: str | None, line: int) -> None:
        if not dotted:
            return
        target = self._resolve(dotted)
        if target and target != self.module:
            self.edges.append(Edge(self.module, target, self._kind(), line))

    # -- nodes ---------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self._base(node.level)
            if base is None:
                return
            dotted = f"{base}.{node.module}" if node.module else base
        else:
            dotted = node.module or ""
        # `from pkg import submodule` is an edge to the submodule. The parent
        # package is only an edge when a name is *not* a submodule, because then
        # `pkg/__init__.py` is what has to run to provide it -- which is exactly
        # how the classic `__init__` imports .a, a.py does `from pkg import x`
        # cycle happens.
        submodules = [
            f"{dotted}.{a.name}" for a in node.names
            if dotted and f"{dotted}.{a.name}" in self.known
        ]
        for target in submodules:
            self._add(target, node.lineno)
        if len(submodules) != len(node.names):
            self._add(dotted, node.lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            was, self.type_only = self.type_only, True
            for child in node.body:
                self.visit(child)
            self.type_only = was
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def module_name(root: Path, path: Path) -> str:
    """Dotted name of `path` inside the package rooted at `root`."""
    rel = path.relative_to(root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def scan(root: Path) -> dict[str, Module]:
    """Parse every module under `root` and build the import graph."""
    root = root.resolve()
    files = sorted(p for p in root.rglob("*.py") if p.is_file())
    modules: dict[str, Module] = {}
    for path in files:
        try:
            modules[module_name(root, path)] = Module(module_name(root, path), path)
        except ValueError:
            continue
    known = set(modules)
    for name, module in modules.items():
        source = module.path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(module.path))
        except SyntaxError as exc:
            raise SyntaxError(f"{module.path}:{exc.lineno}: {exc.msg}") from None
        visitor = _Visitor(name, root.name, module.path.name == "__init__.py", known)
        visitor.visit(tree)
        module.edges = visitor.edges
    return modules


# --- graph -------------------------------------------------------------------


def _adjacency(modules: dict[str, Module], kinds: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {name: [] for name in modules}
    for module in modules.values():
        for edge in module.edges:
            if edge.kind in kinds and edge.target not in out[module.name]:
                out[module.name].append(edge.target)
    return out


def strongly_connected(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCC, iterative so a deep package cannot blow the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            neighbours = graph.get(node, ())
            while child < len(neighbours):
                nxt = neighbours[child]
                child += 1
                work[-1] = (node, child)
                if nxt not in index:
                    work.append((nxt, 0))
                    recursed = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if recursed:
                continue
            if child >= len(neighbours):
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        popped = stack.pop()
                        on_stack.discard(popped)
                        component.append(popped)
                        if popped == node:
                            break
                    if len(component) > 1:
                        result.append(sorted(component))
    return result


def cycle_path(graph: dict[str, list[str]], component: list[str]) -> list[str]:
    """A concrete loop through `component`, as a readable path a -> b -> a."""
    inside = set(component)
    start = component[0]
    previous: dict[str, str] = {}
    queue = [start]
    seen = {start}
    while queue:
        node = queue.pop(0)
        for nxt in graph.get(node, ()):
            if nxt not in inside:
                continue
            if nxt == start:
                path = [node]
                while path[-1] != start:
                    path.append(previous[path[-1]])
                return list(reversed(path)) + [start]
            if nxt not in seen:
                seen.add(nxt)
                previous[nxt] = node
                queue.append(nxt)
    return component + [component[0]]


@dataclass
class Report:
    runtime_cycles: list[list[str]] = field(default_factory=list)
    latent_cycles: list[list[str]] = field(default_factory=list)
    forbidden: list[Edge] = field(default_factory=list)
    unimported: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.runtime_cycles or self.forbidden)


def analyse(modules: dict[str, Module], forbid: list[tuple[str, str]] | None = None,
            find_unimported: bool = False) -> Report:
    report = Report()

    runtime = _adjacency(modules, {RUNTIME})
    for component in strongly_connected(runtime):
        report.runtime_cycles.append(cycle_path(runtime, component))

    # A cycle that only closes through a deferred or TYPE_CHECKING import works
    # today. It is still a cycle, and the next refactor turns it into the above.
    everything = _adjacency(modules, {RUNTIME, DEFERRED, TYPING})
    runtime_members = {m for cycle in report.runtime_cycles for m in cycle}
    for component in strongly_connected(everything):
        if not set(component) & runtime_members:
            report.latent_cycles.append(cycle_path(everything, component))

    for source_prefix, target_prefix in forbid or []:
        for module in modules.values():
            if not _under(module.name, source_prefix):
                continue
            for edge in module.edges:
                if _under(edge.target, target_prefix):
                    report.forbidden.append(edge)

    if find_unimported:
        imported = {e.target for m in modules.values() for e in m.edges}
        report.unimported = sorted(
            name for name in modules
            if name not in imported
            and "." in name                          # the package root is the entry
            and not name.rsplit(".", 1)[1].startswith("__")
            and not any(k.startswith(name + ".") for k in modules)  # not a package
        )
    return report


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _parse_forbid(values: list[str]) -> list[tuple[str, str]]:
    rules = []
    for value in values:
        if "->" not in value:
            raise ValueError(f"--forbid wants 'a -> b', got {value!r}")
        source, _, target = value.partition("->")
        rules.append((source.strip(), target.strip()))
    return rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="importcycle",
        description="Circular imports, layering violations and dead modules, "
                    "found statically.",
    )
    parser.add_argument("package", help="path to the package directory")
    parser.add_argument("--forbid", action="append", default=[], metavar="'a -> b'",
                        help="fail if anything under a imports anything under b; "
                             "repeatable")
    parser.add_argument("--unimported", action="store_true",
                        help="also list modules nothing imports")
    parser.add_argument("--latent", action="store_true",
                        help="also list cycles that only close through a "
                             "deferred or TYPE_CHECKING import")
    args = parser.parse_args(argv)

    root = Path(args.package)
    if not root.is_dir():
        print(f"importcycle: {root}: not a directory", file=sys.stderr)
        return 2
    try:
        forbid = _parse_forbid(args.forbid)
    except ValueError as exc:
        print(f"importcycle: {exc}", file=sys.stderr)
        return 2
    try:
        modules = scan(root)
    except SyntaxError as exc:
        print(f"importcycle: {exc}", file=sys.stderr)
        return 2
    if not modules:
        print(f"importcycle: no .py files under {root}", file=sys.stderr)
        return 2

    report = analyse(modules, forbid, args.unimported)
    edges = sum(len(m.edges) for m in modules.values())
    print(f"{len(modules)} modules, {edges} internal imports\n")

    for cycle in report.runtime_cycles:
        print("runtime cycle (fails at import time):")
        print("    " + " -> ".join(cycle))
    if report.runtime_cycles:
        print()

    if args.latent and report.latent_cycles:
        for cycle in report.latent_cycles:
            print("latent cycle (only closes through a deferred/typing import):")
            print("    " + " -> ".join(cycle))
        print()

    for edge in report.forbidden:
        location = modules[edge.source].path
        print(f"forbidden import: {edge.source} -> {edge.target}")
        print(f"    {location}:{edge.line} ({edge.kind})")
    if report.forbidden:
        print()

    if report.unimported:
        print("nothing imports these:")
        for name in report.unimported:
            print(f"    {name}")
        print()

    if not report.failed:
        print("importcycle: no runtime cycles, no forbidden imports.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
