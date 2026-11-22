"""Lab-only pytest plugin: key-level reads of sqlglot's dict constants
(load with -p brdict).

Rewrites sqlglot's modules at import so that every module- or class-level
`NAME = {...}` dict display becomes `NAME = _br_dict("path:line", {...})`, a
dict subclass that records, per test, which keys are read. Reading the
whole dict (iteration, keys/values/items, copy, equality, repr, len, merge,
pickling) records "*" (every key). Reads outside a test (import,
collection) are recorded under "<import>".

Writes /out/dict-<worker>.json: {context: ["path:line|key-repr" | "path:line|*"]}.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys

import pytest

ROOT = "/src/sqlglot/"
_current = ["<import>"]
_reads: dict[str, set] = {}
_sites: set = set()


def _rec(site, key):
    b = _reads.get(_current[0])
    if b is None:
        b = _reads[_current[0]] = set()
    try:
        b.add(f"{site}|{key!r}")
    except Exception:  # noqa: BLE001
        b.add(f"{site}|*")


def _all(site, op="*"):
    b = _reads.get(_current[0])
    if b is None:
        b = _reads[_current[0]] = set()
    b.add(f"{site}|*")
    b.add(f"{site}|*{op}")


class TrackDict(dict):
    __slots__ = ("_br_site",)

    def __getitem__(self, k):
        _rec(self._br_site, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _rec(self._br_site, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _rec(self._br_site, k)
        return dict.__contains__(self, k)

    def setdefault(self, k, default=None):
        _rec(self._br_site, k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *a):
        _rec(self._br_site, k)
        return dict.pop(self, k, *a)

    def __setitem__(self, k, v):
        _rec(self._br_site, k)
        return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        _rec(self._br_site, k)
        return dict.__delitem__(self, k)

    def _whole(name):  # noqa: N805
        base = getattr(dict, name)

        def f(self, *a, **k):
            _all(self._br_site, name)
            return base(self, *a, **k)

        f.__name__ = name
        return f

    for _n in ("__iter__", "keys", "values", "items", "copy", "__eq__", "__ne__", "__repr__", "__len__",
               "__or__", "__ror__", "__ior__", "update", "popitem", "clear", "__reversed__", "__reduce_ex__",
               "__reduce__", "__str__"):
        if hasattr(dict, _n):
            locals()[_n] = _whole(_n)
    del _n, _whole

    def __bool__(self):  # dict has no __bool__; truth uses __len__
        _all(self._br_site, "__bool__")
        return dict.__len__(self) > 0


def _br_dict(site, d):
    _sites.add(site)
    t = TrackDict(d)
    t._br_site = site
    return t


builtins._br_dict = _br_dict


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_FunctionDef(self, node):
        return node  # only module and class bodies

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Assign(self, node):
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Dict)):
            site = f"{self.rel}:{node.value.lineno}"
            node.value = ast.copy_location(
                ast.Call(func=ast.Name("_br_dict", ast.Load()), args=[ast.Constant(site), node.value], keywords=[]),
                node.value)
        return node

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Dict):
            site = f"{self.rel}:{node.value.lineno}"
            node.value = ast.copy_location(
                ast.Call(func=ast.Name("_br_dict", ast.Load()), args=[ast.Constant(site), node.value], keywords=[]),
                node.value)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        rel = path[len(ROOT):] if path.startswith(ROOT) else path
        tree = ast.parse(data, filename=path)
        tree = Rewriter(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not spec.origin.startswith(ROOT + "sqlglot/"):
            return spec
        spec.loader = Loader(name, spec.origin)
        return spec


sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _current[0] = item.nodeid + "|setup"
    yield
    _current[0] = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"/out/dict-{worker}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _reads.items()}, f)
    with open(f"/out/dict-sites-{worker}.json", "w") as f:
        json.dump(sorted(_sites), f)
