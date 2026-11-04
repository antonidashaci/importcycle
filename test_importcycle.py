import pytest

from importcycle import DEFERRED, RUNTIME, TYPING, analyse, main, scan


def build(tmp_path, files):
    """Write a package on disk and return its root path."""
    root = tmp_path / "pkg"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def kinds(modules, source):
    return {(e.target, e.kind) for e in modules[source].edges}


# --- edges -------------------------------------------------------------------


def test_absolute_and_relative_imports_resolve(tmp_path):
    root = build(tmp_path, {
        "a.py": "import pkg.b\n",
        "b.py": "from . import c\n",
        "c.py": "from .sub.deep import thing\n",
        "sub/__init__.py": "",
        "sub/deep.py": "thing = 1\n",
    })
    modules = scan(root)
    assert ("pkg.b", RUNTIME) in kinds(modules, "pkg.a")
    assert ("pkg.c", RUNTIME) in kinds(modules, "pkg.b")
    assert ("pkg.sub.deep", RUNTIME) in kinds(modules, "pkg.c")


def test_from_package_import_submodule_points_at_the_submodule(tmp_path):
    root = build(tmp_path, {
        "sub/__init__.py": "",
        "sub/leaf.py": "x = 1\n",
        "user.py": "from pkg.sub import leaf\n",
    })
    targets = {e.target for e in scan(root)["pkg.user"].edges}
    assert "pkg.sub.leaf" in targets


def test_third_party_imports_are_ignored(tmp_path):
    root = build(tmp_path, {"a.py": "import os\nimport requests\nfrom json import loads\n"})
    assert scan(root)["pkg.a"].edges == []


def test_import_kinds(tmp_path):
    root = build(tmp_path, {
        "a.py": "x = 1\n",
        "runtime.py": "import pkg.a\n",
        "deferred.py": "def f():\n    import pkg.a\n",
        "later.py": "async def f():\n    from pkg import a\n",
        "typed.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import pkg.a\n"
        ),
        "typed2.py": (
            "import typing\n"
            "if typing.TYPE_CHECKING:\n    import pkg.a\n"
        ),
    })
    modules = scan(root)
    assert kinds(modules, "pkg.runtime") == {("pkg.a", RUNTIME)}
    assert kinds(modules, "pkg.deferred") == {("pkg.a", DEFERRED)}
    assert kinds(modules, "pkg.later") == {("pkg.a", DEFERRED)}
    assert kinds(modules, "pkg.typed") == {("pkg.a", TYPING)}
    assert kinds(modules, "pkg.typed2") == {("pkg.a", TYPING)}


def test_a_module_importing_itself_is_not_an_edge(tmp_path):
    root = build(tmp_path, {"a.py": "import pkg.a\n"})
    assert scan(root)["pkg.a"].edges == []


# --- cycles ------------------------------------------------------------------


def test_runtime_cycle_is_found_with_a_readable_path(tmp_path):
    root = build(tmp_path, {
        "a.py": "import pkg.b\n",
        "b.py": "import pkg.a\n",
    })
    report = analyse(scan(root))
    assert len(report.runtime_cycles) == 1
    cycle = report.runtime_cycles[0]
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"pkg.a", "pkg.b"}
    assert report.failed


def test_three_module_cycle(tmp_path):
    root = build(tmp_path, {
        "a.py": "import pkg.b\n",
        "b.py": "import pkg.c\n",
        "c.py": "import pkg.a\n",
    })
    report = analyse(scan(root))
    assert len(report.runtime_cycles) == 1
    assert len(report.runtime_cycles[0]) == 4  # a -> b -> c -> a


def test_a_cycle_broken_by_a_deferred_import_is_latent_not_runtime(tmp_path):
    root = build(tmp_path, {
        "a.py": "def f():\n    import pkg.b\n",
        "b.py": "import pkg.a\n",
    })
    report = analyse(scan(root))
    assert report.runtime_cycles == []
    assert len(report.latent_cycles) == 1
    assert not report.failed  # it works today, so it does not fail the build


def test_a_type_checking_only_cycle_is_latent(tmp_path):
    root = build(tmp_path, {
        "a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pkg.b\n",
        "b.py": "import pkg.a\n",
    })
    report = analyse(scan(root))
    assert report.runtime_cycles == []
    assert len(report.latent_cycles) == 1


def test_a_runtime_cycle_is_not_also_reported_as_latent(tmp_path):
    root = build(tmp_path, {
        "a.py": "import pkg.b\n",
        "b.py": "import pkg.a\ndef f():\n    import pkg.a\n",
    })
    report = analyse(scan(root))
    assert len(report.runtime_cycles) == 1
    assert report.latent_cycles == []


def test_the_classic_init_cycle(tmp_path):
    """`__init__` imports .a, and a.py reads a name back off the package."""
    root = build(tmp_path, {"a.py": "from pkg import VERSION\n"})
    (root / "__init__.py").write_text(
        "VERSION = '1'\nfrom pkg.a import thing\n", encoding="utf-8"
    )
    report = analyse(scan(root))
    assert len(report.runtime_cycles) == 1
    assert set(report.runtime_cycles[0]) == {"pkg", "pkg.a"}


def test_importing_a_submodule_is_not_an_edge_to_the_package(tmp_path):
    """Otherwise every `__init__.py` is a hub and everything looks circular."""
    root = build(tmp_path, {"a.py": "x = 1\n", "b.py": "from pkg import a\n"})
    assert {e.target for e in scan(root)["pkg.b"].edges} == {"pkg.a"}


def test_a_diamond_is_not_a_cycle(tmp_path):
    root = build(tmp_path, {
        "top.py": "import pkg.left\nimport pkg.right\n",
        "left.py": "import pkg.base\n",
        "right.py": "import pkg.base\n",
        "base.py": "x = 1\n",
    })
    report = analyse(scan(root))
    assert report.runtime_cycles == []
    assert report.latent_cycles == []


def test_deep_chain_does_not_recurse_to_death(tmp_path):
    files = {f"m{i}.py": f"import pkg.m{i + 1}\n" for i in range(2000)}
    files["m2000.py"] = "import pkg.m0\n"
    root = build(tmp_path, files)
    report = analyse(scan(root))
    assert len(report.runtime_cycles) == 1


# --- layering ----------------------------------------------------------------


def test_forbidden_direction(tmp_path):
    root = build(tmp_path, {
        "core/__init__.py": "",
        "core/model.py": "from pkg.web import view\n",
        "web/__init__.py": "",
        "web/view.py": "import pkg.core.model\n",
    })
    report = analyse(scan(root), forbid=[("pkg.core", "pkg.web")])
    assert [(e.source, e.target) for e in report.forbidden] == [
        ("pkg.core.model", "pkg.web.view")
    ]
    assert report.failed
    # The other direction is the allowed one.
    assert analyse(scan(root), forbid=[("pkg.web", "pkg.nope")]).forbidden == []


def test_prefix_match_is_on_dots_not_characters(tmp_path):
    root = build(tmp_path, {
        "corelib.py": "import pkg.web\n",
        "web.py": "x = 1\n",
    })
    assert analyse(scan(root), forbid=[("pkg.core", "pkg.web")]).forbidden == []


# --- dead modules ------------------------------------------------------------


def test_unimported_modules(tmp_path):
    root = build(tmp_path, {
        "used.py": "x = 1\n",
        "user.py": "import pkg.used\n",
        "orphan.py": "y = 2\n",
    })
    report = analyse(scan(root), find_unimported=True)
    assert "pkg.orphan" in report.unimported
    assert "pkg.used" not in report.unimported
    assert not report.failed  # a dead module is a cleanup, not a failure


# --- CLI ---------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, capsys):
    clean = build(tmp_path, {"a.py": "x = 1\n"})
    assert main([str(clean)]) == 0
    assert "no runtime cycles" in capsys.readouterr().out

    broken = build(tmp_path / "two", {
        "a.py": "import pkg.b\n",
        "b.py": "import pkg.a\n",
    })
    assert main([str(broken)]) == 1
    assert "runtime cycle" in capsys.readouterr().out


def test_cli_rejects_a_bad_rule_and_a_bad_path(tmp_path):
    root = build(tmp_path, {"a.py": "x = 1\n"})
    assert main([str(root), "--forbid", "core web"]) == 2
    assert main([str(tmp_path / "missing")]) == 2


def test_syntax_error_is_reported_with_a_location(tmp_path, capsys):
    root = build(tmp_path, {"bad.py": "def (:\n"})
    assert main([str(root)]) == 2
    assert "bad.py" in capsys.readouterr().err


def test_latent_flag_prints_latent_cycles(tmp_path, capsys):
    root = build(tmp_path, {
        "a.py": "def f():\n    import pkg.b\n",
        "b.py": "import pkg.a\n",
    })
    assert main([str(root), "--latent"]) == 0
    assert "latent cycle" in capsys.readouterr().out
