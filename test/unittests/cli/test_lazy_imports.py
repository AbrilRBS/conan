import ast
import os
import subprocess
import sys
import textwrap

import pytest

import conan
from conans.conan import _EAGER_IMPORTS

_repo_folder = os.path.dirname(os.path.dirname(os.path.abspath(conan.__file__)))


def _bound_names(tree, package):
    """ Names bound in the module scope of the ``__init__.py`` of "package", except those that
    are the submodule itself, as in ``from package import submodule``
    """
    names, pending = set(), list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            module = ".".join(parts[:len(parts) - (node.level - 1)] if node.level else [])
            module = ".".join(filter(None, [module, node.module]))
            for alias in node.names:
                if alias.name == "*":
                    raise ValueError(f"'import *' in {package} cannot be analyzed")
                bound = alias.asname or alias.name
                if not (module == package and bound == alias.name):
                    names.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name == f"{package}.{alias.asname}":
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                raise ValueError(f"Module '__getattr__' in {package} cannot be analyzed")
            names.add(node.name)
        elif not isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                                   ast.GeneratorExp)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            pending.extend(ast.iter_child_nodes(node))
    return names


def _name_clashes(root, packages=("conan", "conans")):
    """ Submodules whose parent package binds the same name to something else. Importing them
    lazily can make "from package import name" return the submodule instead of the object.
    Only parses the source, without importing anything
    """
    clashes = set()
    for package in packages:
        for folder, dirs, files in os.walk(os.path.join(root, package)):
            if "__init__.py" not in files:
                continue
            submodules = {f[:-3] for f in files if f.endswith(".py")} | set(dirs)
            submodules -= {"__init__", "__pycache__"}
            package_name = os.path.relpath(folder, root).replace(os.sep, ".")
            with open(os.path.join(folder, "__init__.py"), encoding="utf-8") as f:
                names = _bound_names(ast.parse(f.read()), package_name)
            clashes.update(f"{package_name}.{name}" for name in submodules & names)
    return clashes


def test_eager_imports_are_up_to_date():
    # If this fails after adding or renaming code, the lazy imports could break "from X import Y"
    clashes = _name_clashes(_repo_folder)
    assert clashes == set(_EAGER_IMPORTS), \
        f"Add to _EAGER_IMPORTS in conans/conan.py: {sorted(clashes - set(_EAGER_IMPORTS))}\n" \
        f"Remove from _EAGER_IMPORTS in conans/conan.py: {sorted(set(_EAGER_IMPORTS) - clashes)}"


@pytest.mark.parametrize("init, expected", [
    ("from pkg.sub import sub", {"pkg.sub"}),
    ("from .sub import sub", {"pkg.sub"}),
    ("from .sub import sub as renamed", set()),
    ("from .sub import other as sub", {"pkg.sub"}),
    ("from .sub import other", set()),
    ("from pkg import sub", set()),
    ("from . import sub", set()),
    ("from pkg import other as sub", {"pkg.sub"}),
    ("import pkg.sub as sub", set()),
    ("import other as sub", {"pkg.sub"}),
    ("def sub(): pass", {"pkg.sub"}),
    ("class sub: pass", {"pkg.sub"}),
    ("sub = 1", {"pkg.sub"}),
    ("sub: int = 1", {"pkg.sub"}),
    ("a, (b, sub) = 1, (2, 3)", {"pkg.sub"}),
    ("try:\n    from other import sub\nexcept ImportError:\n    pass", {"pkg.sub"}),
    ("if True:\n    from other import sub", {"pkg.sub"}),
    ("with open('f') as sub: pass", {"pkg.sub"}),
    ("def f():\n    sub = 1", set()),
    ("class C:\n    sub = 1\n    def __init__(self): sub = 2", set()),
    ("x = [sub for sub in ()]", set()),
    ("x = lambda sub: sub", set()),
    ("from other import something", set()),
])
def test_name_clashes_detection(tmp_path, init, expected):
    folder = tmp_path / "pkg"
    (folder / "subpackage").mkdir(parents=True)
    (folder / "__init__.py").write_text(init)
    (folder / "sub.py").write_text("")
    (folder / "subpackage" / "__init__.py").write_text("")
    assert _name_clashes(str(tmp_path), packages=("pkg",)) == expected


def test_name_clashes_detection_subpackage(tmp_path):
    folder = tmp_path / "pkg" / "sub"
    folder.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("from pkg.sub import sub")
    (folder / "__init__.py").write_text("")
    (folder / "sub.py").write_text("")
    assert _name_clashes(str(tmp_path), packages=("pkg",)) == {"pkg.sub"}


@pytest.mark.parametrize("init", ["from .sub import *", "def __getattr__(name): pass"])
def test_name_clashes_unsupported(tmp_path, init):
    # These cannot be analyzed statically, so they must fail instead of being ignored
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(init)
    with pytest.raises(ValueError, match="cannot be analyzed"):
        _name_clashes(str(tmp_path), packages=("pkg",))


@pytest.mark.skipif(not hasattr(sys, "set_lazy_imports_filter"), reason="Needs Python>=3.15")
def test_lazy_imports_name_clash():
    # Resolving the lazy import in the module that imports the submodule must not replace the
    # function exported by the package with the submodule
    script = textwrap.dedent("""\
        from conans.conan import _enable_lazy_imports
        _enable_lazy_imports()
        from conan.tools.build import cross_building
        import conan.tools.cmake.toolchain.blocks as blocks
        blocks.cross_building
        assert callable(cross_building), type(cross_building)
        """)
    env = dict(os.environ, PYTHONPATH=_repo_folder)
    subprocess.run([sys.executable, "-c", script], env=env, check=True)


def test_conan_entry_point():
    env = dict(os.environ, PYTHONPATH=_repo_folder)
    result = subprocess.run([sys.executable, "-m", "conans.conan", "--version"], env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout.startswith("Conan version")
