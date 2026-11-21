"""pytest plugin for the lab: records which files each test opens (fixtures,
data files) via an audit hook. Loaded with `-p brtrace`.
Writes /out/opens-<worker>.json: {nodeid_or_"<collect>": [paths...]}.
"""

import json
import os
import sys

import pytest

_current = ["<collect>"]
_opens: dict[str, set[str]] = {}
_ROOT = "/src/sqlglot/"


def _hook(event, args):
    if event == "open" and args:
        path = args[0]
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str) and path.startswith(_ROOT) and "__pycache__" not in path:
            rel = path[len(_ROOT):]
            if not rel.endswith(".py"):
                _opens.setdefault(_current[0], set()).add(rel)


sys.addaudithook(_hook)


# The optimizer and executor tests farm work out to spawned process pools,
# which coverage cannot attribute to a test. In the lab, run that work inline.
import concurrent.futures as _cf


class _InlineExecutor(_cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        # Round-trip through pickle like a real process pool does, so the
        # serialization code (sqlglot.serde via __reduce__) is exercised too.
        import pickle

        future = _cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as error:  # noqa: BLE001
            future.set_exception(error)
        return future


if os.environ.get("BR_INLINE_POOL", "1") == "1":
    _cf.ProcessPoolExecutor = _InlineExecutor


def pytest_sessionstart(session):
    # Import every sqlglot module up front (under the empty coverage context),
    # so import-time code is never credited to whichever test loaded a
    # dialect lazily first.
    if os.environ.get("BR_PREIMPORT", "1") != "1":
        return
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(info.name)
        except Exception:  # noqa: BLE001
            pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # Per-class dispatch tables are built once and cached; rebuild them for
    # every test so the builder's coverage reaches every test that needs it.
    try:
        from sqlglot import generator

        generator._DISPATCH_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"


def pytest_sessionfinish(session, exitstatus):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"/out/opens-{worker}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
    if _classes:
        with open(f"/out/classes-{worker}.json", "w") as f:
            json.dump({k: sorted(v) for k, v in _classes.items()}, f)


# Class usage: for every sqlglot function that starts running, record the
# class of its `self` (or `cls`) argument, per test. A change to a class-level
# constant can only matter to tests that used that class (or a subclass).
_classes: dict[str, set[str]] = {}

if os.environ.get("BR_CLASSES") == "1":
    _mon = sys.monitoring
    _TOOL = 4
    _PREFIX = _ROOT + "sqlglot/"
    _getframe = sys._getframe
    _seen: dict = {}

    def _on_start(code, offset):
        if not code.co_filename.startswith(_PREFIX):
            return _mon.DISABLE
        if not code.co_argcount or code.co_varnames[0] not in ("self", "cls"):
            return _mon.DISABLE
        frame = _getframe(1)
        first = frame.f_locals.get(code.co_varnames[0])
        klass = first if isinstance(first, type) else type(first)
        bucket = _classes.get(_current[0])
        if bucket is None:
            bucket = _classes[_current[0]] = set()
        seen = _seen.get(klass)
        if seen is None:
            seen = _seen[klass] = [f"{k.__module__}.{k.__qualname__}" for k in klass.__mro__ if k.__module__.startswith("sqlglot")]
        bucket.update(seen)

    _mon.use_tool_id(_TOOL, "brclasses")
    _mon.register_callback(_TOOL, _mon.events.PY_START, _on_start)
    _mon.set_events(_TOOL, _mon.events.PY_START)
