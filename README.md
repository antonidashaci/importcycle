# importcycle

Circular imports, layering violations and dead modules in a Python package —
found statically, without importing anything.

```bash
python importcycle.py shop/ --forbid "shop.core -> shop.web" --unimported
```

```
7 modules, 4 internal imports

runtime cycle (fails at import time):
    shop.core.billing -> shop.web.views -> shop.core.billing

forbidden import: shop.core.billing -> shop.web.views
    shop/core/billing.py:1 (module)

nothing imports these:
    shop.legacy_export
```

A circular import is two different bugs. The loop that raises at startup is the
easy one — you see it the first time you run the app. The one that *works*,
because someone moved the import inside a function to break the cycle, is the
one that fails six months later when a different entry point imports the
modules in a different order.

importcycle tells them apart.

## Install

```bash
curl -O https://raw.githubusercontent.com/antonidashaci/importcycle/main/importcycle.py
```

Python 3.9+. No dependencies. Nothing is executed — the graph is built with
`ast`, so a package that needs a database to import still gets analysed.

## Three kinds of import edge

| kind | when it runs | in a cycle |
| --- | --- | --- |
| `module` | at import time | **runtime cycle** — this is the one that breaks |
| `deferred` | inside a function, on first call | latent |
| `typing` | under `if TYPE_CHECKING` | never runs; latent |

A cycle that only closes through a deferred or `TYPE_CHECKING` import works
today, so it does not fail the build. `--latent` lists them anyway, because
that is the list you work through before the next refactor turns one of them
into the first kind.

## The `__init__.py` rule

`from pkg import thing` and `from pkg import submodule` are not the same edge:

- `from pkg.sub import leaf`, where `leaf` is a module → edge to `pkg.sub.leaf`.
- `from pkg import VERSION`, where `VERSION` is a name → edge to `pkg` itself,
  because `pkg/__init__.py` is what has to run to provide it.

That distinction is the whole point. Counting every submodule import as an edge
to the parent package would make every `__init__.py` a hub and report half the
codebase as circular. Getting it right is what catches the classic one:
`__init__.py` imports `.a`, and `a.py` does `from pkg import VERSION`.

## Layering

```bash
--forbid "shop.core -> shop.web"     # the domain must not import the web layer
--forbid "shop -> tests"             # repeatable
```

Prefixes match on dots, not characters: `shop.core` does not match
`shop.corelib`. Exit `1` on a runtime cycle or a forbidden import, `0`
otherwise — a latent cycle or a dead module is a cleanup, not a build failure.

## In CI

```yaml
- run: python importcycle.py myapp/ --forbid "myapp.domain -> myapp.api"
```

## As a library

```python
from importcycle import scan, analyse

report = analyse(scan(Path("myapp")), forbid=[("myapp.domain", "myapp.api")])
report.runtime_cycles   # [['myapp.a', 'myapp.b', 'myapp.a'], ...]
report.latent_cycles
report.forbidden        # Edge(source, target, kind, line)
```

## Details that matter

- **Cycles are found with Tarjan's algorithm, iteratively** — a 2000-module
  chain is in the tests, because a recursive version dies on it.
- Each cycle is printed as a concrete shortest path (`a -> b -> c -> a`), not as
  a set of modules you then have to trace by hand.
- Third-party and stdlib imports are ignored; only edges inside the package are
  graph edges.
- `import a.b.c` resolves to the longest prefix that is a real module in the
  package.
- A syntax error exits `2` with the file and line, instead of silently skipping
  the file.

## Tests

```bash
pytest
```

## License

MIT
