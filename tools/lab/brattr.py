"""Lab-only pytest plugin: which tests read the attributes a unittest
class's setUpClass stores (load with -p brattr).

Line coverage credits every name on a covered line to the test, so a line
like `self.a[i] if x else self.b[i]` credits both `a` and `b` to every test
running it. This records the actual reads instead: each TestCase class that
defines setUpClass gets a __getattribute__ that notes reads of the names
setUpClass assigns (`cls.X = ...`).

Writes /out/attr-<worker>.json: {test nodeid: [attribute names]}.
"""

import ast
import inspect
import json
import os
import textwrap
import unittest

import pytest

_current = [None]
_reads: dict[str, set] = {}
_patched: set = set()


def _assigned(cls):
    fn = cls.__dict__.get("setUpClass")
    if fn is None:
        return set()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn.__func__ if hasattr(fn, "__func__") else fn)))
    except (OSError, TypeError, SyntaxError):
        return set()
    out = set()
    for n in ast.walk(tree):
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else []
        for t in targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "cls":
                out.add(t.attr)
    return out


def _patch(cls):
    if cls in _patched:
        return
    _patched.add(cls)
    names = set()
    for k in cls.__mro__:
        if issubclass(k, unittest.TestCase):
            names |= _assigned(k)
    if not names:
        return
    orig = cls.__getattribute__

    def __getattribute__(self, name, _orig=orig, _names=frozenset(names)):
        if name in _names and _current[0] is not None:
            _reads.setdefault(_current[0], set()).add(name)
        return _orig(self, name)

    cls.__getattribute__ = __getattribute__


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    cls = getattr(item, "cls", None)
    if cls is not None and issubclass(cls, unittest.TestCase):
        _patch(cls)
    _current[0] = item.nodeid
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _current[0] = item.nodeid
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _current[0] = item.nodeid
    yield
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"/out/attr-{worker}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _reads.items()}, f)
