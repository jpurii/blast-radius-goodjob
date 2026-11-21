"""Build the selector's maps from the lab's traces of the pinned sqlglot commit.

Inputs (work/out): cov.sqlite (per-test line coverage), opens-*.json (files
each test opened), classes-*.json (classes each test used, with their MROs).
Sources: work/sqlglot at the pinned commit.

Output (blast-radius-goodjob/maps):
  tests.json        test ids in bit order (bit i of every bitset = tests[i])
  cov/<file>.json   {"spans": {line: [first, last, kind, scope, extra]},
                     "lines": {line: setIndex}, "sets": [hexbitset, ...]}
  opens.json        {fixture path: hexbitset}
  classes.json      {"module.Qual.Name": hexbitset}
  names.json        {name: hexbitset}   tests covering any line that loads the name
  files.json        {test file: hexbitset} all tests defined in that file
  classtests.json   {"tests/x.py::TestCls": hexbitset}
Kinds: "f" inside a function body, "c" class body, "m" module level.
"""

from __future__ import annotations

import ast
import collections
import json
import sqlite3
from pathlib import Path

WORK = Path(__file__).parent
SRC = WORK / "sqlglot"
OUT = WORK.parent / "blast-radius-goodjob" / "maps"

test_ids = [t["id"] for t in json.loads((WORK.parent / "kit/sample/tests.json").read_text())["tests"]]
bit = {tid: i for i, tid in enumerate(test_ids)}


def hexbits(nums) -> str:
    v = 0
    for n in nums:
        v |= 1 << n
    return format(v, "x")


def numbits_to_lines(blob):
    for i, byte in enumerate(blob):
        if byte:
            for b in range(8):
                if byte & (1 << b):
                    yield i * 8 + b


# ---------------------------------------------------------------- coverage
db = sqlite3.connect(WORK / "out/cov.sqlite")
files = {fid: p.replace("/src/sqlglot/", "") for fid, p in db.execute("select id, path from file")}
ctx_bit = {}
for cid, c in db.execute("select id, context from context"):
    if c:
        b = bit.get(c.rsplit("|", 1)[0])
        if b is not None:
            ctx_bit[cid] = b
empty_ctx = {cid for cid, c in db.execute("select id, context from context") if not c}
cov = collections.defaultdict(lambda: collections.defaultdict(set))
imp = collections.defaultdict(set)  # lines run outside any test (import, collection)
for fid, cid, blob in db.execute("select file_id, context_id, numbits from line_bits"):
    if cid in empty_ctx:
        imp[files[fid]].update(numbits_to_lines(blob))
        continue
    b = ctx_bit.get(cid)
    if b is None:
        continue
    for ln in numbits_to_lines(blob):
        cov[files[fid]][ln].add(b)

# --------------------------------------------------------------- AST spans


def spans_for(path: str):
    """line -> [first, last, kind, scope, extra] for the innermost statement header."""
    try:
        tree = ast.parse((SRC / path).read_text("utf-8"))
    except (OSError, SyntaxError):
        return {}
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    spans = {}

    def visit(node, depth, kind, scope, cls_scope):
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                continue
            body = getattr(child, "body", None)
            compound = isinstance(body, list) and body and isinstance(body[0], ast.stmt)
            first = child.lineno
            if getattr(child, "decorator_list", None):
                first = min(d.lineno for d in child.decorator_list)
            last = max(first, body[0].lineno - 1) if compound else child.end_lineno
            extra = None
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A header change (signature, defaults, decorators) reaches the
                # function's callers, who run its first body statement.
                extra = body[0].lineno
            entry = [first, last, kind, scope, extra]
            for ln in range(first, last + 1):
                prev = spans.get(ln)
                if prev is None or prev[5] < depth:
                    spans[ln] = entry + [depth]
            if isinstance(child, ast.ClassDef):
                qual = f"{cls_scope}.{child.name}" if cls_scope else child.name
                visit(child, depth + 1, "c", f"{module}.{qual}", qual)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, depth + 1, "f", f"{scope}|{child.name}", cls_scope)
            else:
                # if/for/try/with blocks keep the enclosing scope's kind
                visit(child, depth + 1, kind, scope, cls_scope)
            # nested defs inside a function stay "f" via the recursion above

    visit(tree, 0, "m", module, "")
    return {ln: v[:5] for ln, v in spans.items()}


def multiline_strings(path: str):
    """[first, last] line ranges of string tokens that span several lines."""
    import io
    import tokenize

    out = []
    try:
        toks = tokenize.generate_tokens(io.StringIO((SRC / path).read_text("utf-8")).readline)
        fstart = None
        for tok in toks:
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                out.append([tok.start[0], tok.end[0]])
            elif tok.type == getattr(tokenize, "FSTRING_START", -1):
                fstart = tok.start[0]
            elif tok.type == getattr(tokenize, "FSTRING_END", -1) and fstart is not None:
                if tok.end[0] > fstart:
                    out.append([fstart, tok.end[0]])
                fstart = None
    except (tokenize.TokenError, SyntaxError):
        pass
    return out


def write_json(path: Path, obj):
    import gzip

    path = path.with_name(path.name + ".gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 9, mtime=0))


OUT.mkdir(parents=True, exist_ok=True)
write_json(OUT / "tests.json", test_ids)

py_files = sorted(
    str(p.relative_to(SRC)).replace("\\", "/")
    for d in ("sqlglot", "tests")
    for p in (SRC / d).rglob("*.py")
)
total = 0
for path in py_files:
    sets, set_idx, lines = [], {}, {}
    for ln, tests in cov.get(path, {}).items():
        key = frozenset(tests)
        if key not in set_idx:
            set_idx[key] = len(sets)
            sets.append(hexbits(tests))
        lines[ln] = set_idx[key]
    data = {"spans": spans_for(path), "lines": lines, "sets": sets, "mls": multiline_strings(path),
            "imp": sorted(imp.get(path, ()))}
    out = OUT / "cov" / (path.replace("/", "__") + ".json")
    write_json(out, data)
    total += out.with_name(out.name + ".gz").stat().st_size
print("cov maps", len(py_files), "files", total // 1024, "KB")

# ------------------------------------------------------------------ opens
opens = collections.defaultdict(set)
for p in (WORK / "out").glob("opens-*.json"):
    for nodeid, paths in json.loads(p.read_text()).items():
        b = bit.get(nodeid)
        for path in paths:
            opens[path].add(b if b is not None else -1)
collect_opened = sorted(p for p, s in opens.items() if -1 in s)
print("fixtures opened outside a test:", collect_opened[:10])
write_json(OUT / "opens.json", {p: hexbits(s - {-1}) for p, s in opens.items()})

# ---------------------------------------------------------------- classes
classes = collections.defaultdict(set)
for p in (WORK / "out").glob("classes-*.json"):
    for nodeid, keys in json.loads(p.read_text()).items():
        b = bit.get(nodeid)
        if b is None:
            continue
        for k in keys:
            classes[k].add(b)
write_json(OUT / "classes.json", {k: hexbits(s) for k, s in classes.items()})
print("classes", len(classes))

# ------------------------------------------------------------------ names
name_sites = collections.defaultdict(set)  # name -> {(file, line)}
for path in py_files:
    try:
        tree = ast.parse((SRC / path).read_text("utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name_sites[node.id].add((path, node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name_sites[node.attr].add((path, node.lineno))
name_bits: dict[str, int] = {}
for name, sites in name_sites.items():
    v = 0
    for path, ln in sites:
        for b in cov.get(path, {}).get(ln, ()):
            v |= 1 << b
    name_bits[name] = v


# Import-time consumers. Code outside any function runs at import, before
# any test, so coverage credits it to no test. A name loaded there feeds what
# that code builds: the names a module-level statement binds, or the class
# whose body loads it. Propagate test sets along those edges to a fixpoint.
def bound_by(stmt):
    out = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            out.add((a.asname or a.name).split(".")[0])
    else:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        for tg in targets:
            for n in ast.walk(tg):
                if isinstance(n, ast.Name):
                    out.add(n.id)
                elif isinstance(n, ast.Attribute):
                    out.add(n.attr)
    return out


def import_time_loads(node):
    """Names loaded by a statement outside nested function bodies."""
    out = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # decorators, defaults and annotations run at import; the body not
            stack.extend(n.decorator_list)
            stack.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            out.add(n.attr)
        if isinstance(n, ast.ClassDef) and n is not node:
            continue  # nested class bodies get their own edges
        stack.extend(ast.iter_child_nodes(n))
    return out


edges = []  # (loaded name, ("name", target) | ("class", key))
for path in py_files:
    try:
        tree = ast.parse((SRC / path).read_text("utf-8"))
    except SyntaxError:
        continue
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]

    def walk_module(stmts):
        for st in stmts:
            if isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(st, field, None) or []
                    walk_module([h for h in sub if isinstance(h, ast.stmt)] +
                                [x for h in sub if isinstance(h, ast.ExceptHandler) for x in h.body])
                for n in import_time_loads(getattr(st, "test", None) or getattr(st, "iter", None) or ast.Pass()):
                    pass
                continue
            if isinstance(st, ast.ClassDef):
                walk_class(st, st.name)
            targets = bound_by(st)
            loads = import_time_loads(st) if not isinstance(st, ast.ClassDef) else (
                {x for b in st.bases + st.decorator_list + [k.value for k in st.keywords] for x in import_time_loads(b)})
            for n in loads:
                for tg in targets:
                    if tg != n:
                        edges.append((n, ("name", tg)))

    def walk_class(cls, qual):
        key = f"{module}.{qual}"
        for st in cls.body:
            if isinstance(st, ast.ClassDef):
                walk_class(st, f"{qual}.{st.name}")
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                loads = import_time_loads(st)
            else:
                loads = import_time_loads(st)
            for n in loads:
                edges.append((n, ("class", key)))
                edges.append((n, ("name", cls.name)))

    walk_module(tree.body)

class_bits = {k: int(h, 16) for k, h in ((k, hexbits(s)) for k, s in classes.items())}
for it in range(30):
    changed = 0
    for n, (kind, tgt) in edges:
        add = class_bits.get(tgt, 0) if kind == "class" else name_bits.get(tgt, 0)
        cur = name_bits.get(n, 0)
        if add & ~cur:
            name_bits[n] = cur | add
            changed += 1
    if not changed:
        break
print("import-time edges", len(edges), "fixpoint rounds", it + 1)
names = {n: format(v, "x") for n, v in name_bits.items() if v}
write_json(OUT / "names.json", names)
print("names", len(names))

# ------------------------------------------------------- tests per file/class
by_file = collections.defaultdict(set)
by_class = collections.defaultdict(set)
for tid, b in bit.items():
    f, rest = tid.split("::", 1)
    by_file[f].add(b)
    if "::" in rest:
        by_class[f + "::" + rest.split("::")[0]].add(b)
write_json(OUT / "files.json", {k: hexbits(v) for k, v in by_file.items()})
write_json(OUT / "classtests.json", {k: hexbits(v) for k, v in by_class.items()})
print("done ->", OUT)
