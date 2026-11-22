"""Lab-only pytest plugin: weak-mutation profile of sqlglot (load with -p brwm).

Rewrites sqlglot's function bodies at import to record, per test, where two
mutation kinds would infect the program state:

- a two-operand `A and B` / `A or B` used as a condition (if/while/assert/
  ternary test/comprehension filter/`not` operand): swapping and<->or gives
  the same branch only when A and B have the same truth value. Recorded as
  "unsafe" for the test when an evaluation is not (True, True) for `and`,
  not (False, False) for `or`.
- a single ordering comparison `a < b` (<, <=, >, >=): swapping < and <=
  (> and >=) changes the result only when a == b. Recorded as "unsafe" when
  an evaluation saw a == b (or the comparison raised).

Writes /out/wm-<worker>.json: {test nodeid (or "<import>"): [site keys]},
site key = "path:line:col" of the BoolOp / Compare node, plus
/out/wm-sites-<worker>.json listing every instrumented site.
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
_unsafe: dict[str, set] = {}
_sites: set = set()

BOOL_CTX_PARENTS = (ast.If, ast.While, ast.IfExp, ast.Assert)
PURE_CALLS = {"isinstance", "issubclass", "len", "type", "callable", "hasattr"}
PURE_METHODS = {"get"}


def _flag(key):
    bucket = _unsafe.get(_current[0])
    if bucket is None:
        bucket = _unsafe[_current[0]] = set()
    bucket.add(key)


def _br_and(key, a, thunk):
    if not a:
        _flag(key)
        return a
    b = thunk()
    if not b:
        _flag(key)
    return b


def _br_or(key, a, thunk):
    if a:
        _flag(key)
        return a
    b = thunk()
    if b:
        _flag(key)
    return b


def _and_range(truths, idx):
    """Short-circuit `and` over operand indexes with known truths (None:
    an operand the original never evaluated)."""
    for i in idx:
        t = truths[i] if i < len(truths) else None
        if t is None:
            return None
        if not t:
            return False
    return True


def _mut_truth(is_and, truths, k, p):
    """Truth of the chain with the operator after operand p swapped, from the
    original evaluation's truths; None if that needs an unevaluated operand."""
    if is_and:
        # x0 and .. and xp  or  x(p+1) and .. and x(k-1)
        left = _and_range(truths, range(0, p + 1))
        if left is None or left:
            return left
        return _and_range(truths, range(p + 1, k))
    # x0 or .. or x(p-1) or (xp and x(p+1)) or x(p+2) or ..
    groups = [range(i, i + 1) for i in range(0, p)] + [range(p, p + 2)] + [range(i, i + 1) for i in range(p + 2, k)]
    for g in groups:
        v = _and_range(truths, g)
        if v is None:
            return None
        if v:
            return True
    return False


def _br_bool(key, is_and, first, *thunks):
    """A chain of 3+ operands joined by one operator, evaluated exactly as
    Python does; flags "key#p" when swapping the operator after operand p
    would change the truth of the result."""
    t = bool(first)
    truths = [t]
    res = first
    i = 0
    stop = (not t) if is_and else t
    while not stop and i < len(thunks):
        v = thunks[i]()
        t = bool(v)
        truths.append(t)
        res = v
        i += 1
        stop = (not t) if is_and else t
    orig = truths[-1]
    k = len(thunks) + 1
    for p in range(k - 1):
        m = _mut_truth(is_and, truths, k, p)
        if m is None or m != orig:
            _flag(f"{key}#{p}")
    return res


builtins._br_bool = _br_bool

_PLAIN = (int, float, str, bytes)


def _br_cmp(key, op, a, b):
    # Only plain values compare by value: sqlglot expressions overload < and
    # <= to BUILD nodes (exp.LT vs exp.LTE), so any other type is infected.
    if type(a) not in _PLAIN or type(b) not in _PLAIN:
        _flag(key)
    else:
        try:
            if a == b:
                _flag(key)
        except Exception:  # noqa: BLE001
            _flag(key)
    if op == 0:
        return a < b
    if op == 1:
        return a <= b
    if op == 2:
        return a > b
    return a >= b


builtins._br_and = _br_and
builtins._br_or = _br_or
builtins._br_cmp = _br_cmp


def lambda_safe(node):
    """Can this expression move into a lambda unchanged?"""
    for n in ast.walk(node):
        if isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)):
            return False
        if isinstance(n, ast.Name) and n.id in ("super", "__class__"):
            return False
    return True


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.fn_depth = 0
        self.bool_ctx: set[int] = set()

    def mark_ctx(self, node):
        if node is not None:
            self.bool_ctx.add(id(node))

    def visit_FunctionDef(self, node):
        self.fn_depth += 1
        self.generic_visit(node)
        self.fn_depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        # class bodies run at class creation; only nested functions count
        saved = self.fn_depth
        self.fn_depth = 0
        self.generic_visit(node)
        self.fn_depth = saved
        return node

    def visit_Lambda(self, node):
        self.fn_depth += 1
        self.generic_visit(node)
        self.fn_depth -= 1
        return node

    def generic_visit(self, node):
        if isinstance(node, BOOL_CTX_PARENTS):
            self.mark_ctx(node.test)
        elif isinstance(node, ast.comprehension):
            for c in node.ifs:
                self.mark_ctx(c)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and id(node) in self.bool_ctx:
            self.mark_ctx(node.operand)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.mark_ctx(node.operand)
        elif isinstance(node, ast.BoolOp) and id(node) in self.bool_ctx:
            for v in node.values:
                self.mark_ctx(v)
        return super().generic_visit(node)

    def key(self, node):
        return f"{self.rel}:{node.lineno}:{node.col_offset}"

    def visit_BoolOp(self, node):
        in_ctx = id(node) in self.bool_ctx
        self.generic_visit(node)
        k = len(node.values)
        if self.fn_depth and in_ctx and 3 <= k <= 6 and all(lambda_safe(v) for v in node.values[1:]):
            key = self.key(node)
            for p in range(k - 1):
                _sites.add(f"{key}#{p}")
            empty = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
            call = ast.Call(
                func=ast.Name("_br_bool", ast.Load()),
                args=[ast.Constant(key), ast.Constant(isinstance(node.op, ast.And)), node.values[0]]
                + [ast.Lambda(empty, v) for v in node.values[1:]],
                keywords=[])
            return ast.copy_location(call, node)
        if not (self.fn_depth and in_ctx and k == 2 and lambda_safe(node.values[1])):
            return node
        key = self.key(node)
        _sites.add(key)
        fn = "_br_and" if isinstance(node.op, ast.And) else "_br_or"
        call = ast.Call(
            func=ast.Name(fn, ast.Load()),
            args=[ast.Constant(key), node.values[0],
                  ast.Lambda(ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                             node.values[1])],
            keywords=[])
        return ast.copy_location(call, node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3}
        if not (self.fn_depth and len(node.ops) == 1 and type(node.ops[0]) in ops):
            return node
        key = self.key(node)
        _sites.add(key)
        call = ast.Call(func=ast.Name("_br_cmp", ast.Load()),
                        args=[ast.Constant(key), ast.Constant(ops[type(node.ops[0])]), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # always rewrite from source (never a cached .pyc)
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
    with open(f"/out/wm-{worker}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _unsafe.items()}, f)
    with open(f"/out/wm-sites-{worker}.json", "w") as f:
        json.dump(sorted(_sites), f)
