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
import sys
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
def class_mates(nodeid):
    """Every test sharing the test's class (or file, outside a class): what a
    test's setup phase does (setUpClass, class/module fixtures) runs once, in
    whichever of them comes first, and every one of them depends on it."""
    f, rest = nodeid.split("::", 1)
    key = f + "::" + rest.split("::")[0] + "::" if "::" in rest else f + "::"
    return {b for t, b in bit.items() if t.startswith(key)}


ctx_bits = {}
for cid, c in db.execute("select id, context from context"):
    if c:
        nodeid, _, phase = c.rpartition("|")
        if not nodeid:
            nodeid, phase = c, ""
        b = bit.get(nodeid)
        if b is not None:
            ctx_bits[cid] = class_mates(nodeid) | {b} if phase == "setup" else {b}
empty_ctx = {cid for cid, c in db.execute("select id, context from context") if not c}
cov = collections.defaultdict(lambda: collections.defaultdict(set))
imp = collections.defaultdict(set)  # lines run outside any test (import, collection)
for fid, cid, blob in db.execute("select file_id, context_id, numbits from line_bits"):
    if cid in empty_ctx:
        imp[files[fid]].update(numbits_to_lines(blob))
        continue
    bs = ctx_bits.get(cid)
    if bs is None:
        continue
    for ln in numbits_to_lines(blob):
        cov[files[fid]][ln].update(bs)

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
                # A docstring never runs: take the first statement after it.
                stmts = body
                if (len(stmts) > 1 and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant)
                        and isinstance(stmts[0].value.value, str)):
                    stmts = stmts[1:]
                extra = stmts[0].lineno
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
setup_opens = []  # (fixture path, nodeid whose setup phase opened it)
for p in (WORK / "out").glob("opens-*.json"):
    for key, paths in json.loads(p.read_text()).items():
        nodeid, setup = (key[: -len("|setup")], True) if key.endswith("|setup") else (key, False)
        b = bit.get(nodeid)
        for path in paths:
            opens[path].add(b if b is not None else -1)
            if setup and b is not None:
                setup_opens.append((path, nodeid))
collect_opened = sorted(p for p, s in opens.items() if -1 in s)
print("fixtures opened outside a test:", collect_opened[:10])
# opens.json is written after the name map (setup opens are refined with it)

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


# A fixture a class's setup opened reaches the class's tests that read what
# setup stored it in: `cls.X = ...("<path suffix>")` -> tests loading X.
# Unresolved (no literal path in setUpClass, as for f-string paths): every
# test of the class.
def setup_attrs(test_file, cls_name, fixture):
    try:
        tree = ast.parse((SRC / test_file).read_text("utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for fn in node.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name not in ("setUpClass", "setUpModule"):
                    continue
                for st in ast.walk(fn):
                    if not isinstance(st, ast.Assign):
                        continue
                    lits = [c.value for c in ast.walk(st.value)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str) and len(c.value) > 4]
                    if any(fixture.endswith(s) for s in lits):
                        return {t.attr for t in st.targets if isinstance(t, ast.Attribute)} or None
    return None


def setup_loader(test_file, cls_name, fixture):
    """The setUpClass statement `cls.X = <expr>` whose expr names the
    fixture, when expr only calls builtins and functions imported from a
    module under tests/: {"expr": src, "mods": {name: "tests/x.py"}}."""
    try:
        src = (SRC / test_file).read_text("utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return None
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests.") and not node.level:
            for a in node.names:
                imported[a.asname or a.name] = node.module.replace(".", "/") + ".py"
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for fn in node.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name != "setUpClass":
                    continue
                for st in ast.walk(fn):
                    lits = [c.value for c in ast.walk(st) if isinstance(c, ast.Constant)
                            and isinstance(c.value, str) and len(c.value) > 4 and fixture.endswith(c.value)]
                    if lits and isinstance(st, (ast.stmt,)):
                        hits.append(st)
    # innermost statements only (no other hit inside them)
    hits = [h for h in hits if not any(h is not o and any(x is o for x in ast.walk(h)) for o in hits)]
    if len(hits) != 1 or not isinstance(hits[0], ast.Assign):
        return None
    st = hits[0]
    if not all(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "cls"
               for t in st.targets):
        return None
    mods = {}
    for n in ast.walk(st.value):
        if isinstance(n, ast.Name):
            if n.id in imported:
                mods[n.id] = imported[n.id]
            elif n.id not in ("list", "tuple", "dict", "set", "sorted", "str"):
                return None
        elif isinstance(n, (ast.Attribute, ast.Lambda, ast.NamedExpr)):
            return None
    return {"expr": ast.get_source_segment(src, st.value), "mods": mods}


# Actual reads of setUpClass attributes (lab/brattr.py), finer than the
# line-level name map: {class "file::Class": {attr: {test bits}}}.
attr_reads = collections.defaultdict(lambda: collections.defaultdict(set))
for p in (WORK / "out").glob("attr-*.json"):
    for nodeid, names_read in json.loads(p.read_text()).items():
        b = bit.get(nodeid)
        if b is None or nodeid.count("::") < 2:
            continue
        for a in names_read:
            attr_reads[nodeid.rsplit("::", 1)[0]][a].add(b)

setup_map = {}
refined = 0
for path, nodeid in setup_opens:
    mates = class_mates(nodeid)
    f, rest = nodeid.split("::", 1)
    attrs = setup_attrs(f, rest.split("::")[0], path) if "::" in rest else None
    use = 0
    for a in attrs or ():
        use |= name_bits.get(a, 0)
    chosen = {b for b in mates if use >> b & 1} if use else set()
    if chosen:
        refined += 1
        opens[path].update(chosen)
    else:
        opens[path].update(mates)
    loader = setup_loader(f, rest.split("::")[0], path) if chosen and "::" in rest else None
    if loader:
        e = setup_map.setdefault(path, {"cls": set(), "readers": set(), "loaders": []})
        e["cls"] |= mates
        traced = attr_reads.get(f + "::" + rest.split("::")[0])
        if traced:
            exact = set().union(*(traced.get(a, set()) for a in attrs)) & mates
            e["readers"] |= exact
            e["traced"] = True
        else:
            e["readers"] |= chosen
        if loader not in e["loaders"]:
            e["loaders"].append(loader)
print("setup opens", len(setup_opens), "refined by stored attribute", refined)
# Tests that open each setup-loaded fixture themselves (outside any setup).
direct = collections.defaultdict(set)
for p in (WORK / "out").glob("opens-*.json"):
    for key, paths in json.loads(p.read_text()).items():
        if key.endswith("|setup"):
            continue
        b = bit.get(key)
        for path in paths:
            if path in setup_map:
                direct[path].add(b if b is not None else -1)
setup_json = {}
for path, e in setup_map.items():
    if -1 in direct[path]:
        continue  # also read outside any test: keep the plain opens rule
    setup_json[path] = {"cls": hexbits(e["cls"]), "readers": hexbits(e["readers"]),
                        "direct": hexbits(direct[path]), "loaders": e["loaders"]}
write_json(OUT / "setup.json", setup_json)
print("setup-loaded fixtures", {k: v["loaders"] for k, v in setup_json.items()})

# ------------------------------------------------- weak-mutation profile
# out/wm-*.json (lab/brwm.py): per test, the condition sites where an
# and<->or swap (2-operand condition) or a </<= or >/>= swap would infect
# state. Map per file: {"line:col": hexbits of infected tests, or "*" when
# the site also ran outside any test (import time: no pruning)}.
wm_sites = set()
for p in (WORK / "out").glob("wm-sites-*.json"):
    wm_sites.update(json.loads(p.read_text()))
wm_bits = collections.defaultdict(set)
wm_star = set()
for p in (WORK / "out").glob("wm-*.json"):
    if p.name.startswith("wm-sites-"):
        continue
    for key, sites in json.loads(p.read_text()).items():
        nodeid, setup = (key[: -len("|setup")], True) if key.endswith("|setup") else (key, False)
        b = bit.get(nodeid)
        for s in sites:
            if b is None:
                wm_star.add(s)  # outside a test (import, collection, between)
            else:
                wm_bits[s].update(class_mates(nodeid) | {b} if setup else {b})
wm = collections.defaultdict(dict)
for s in wm_sites:
    path, line, col = s.rsplit(":", 2)
    wm[path][f"{line}:{col}"] = "*" if s in wm_star else hexbits(wm_bits.get(s, ()))
write_json(OUT / "wm.json", wm)
print("weak-mutation sites", len(wm_sites), "files", len(wm), "import-time", len(wm_star))
write_json(OUT / "opens.json", {p: hexbits(s - {-1}) for p, s in opens.items()})

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
# ------------------------------------------- attributes read in hidden ways
# Names a class constant can be read through without a test running a line
# that loads it: loads inside function bodies that run at import (the
# metaclasses copy and derive class constants there), and identifier-like
# string constants (getattr/__dict__.get by name). The selector does not
# narrow class-constant edits for these names.
dyn = set()
for path in py_files:
    if not path.startswith("sqlglot/"):
        continue
    try:
        tree = ast.parse((SRC / path).read_text("utf-8"))
    except SyntaxError:
        continue
    imp_lines = imp.get(path, set())
    fn_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = node.body if isinstance(node.body, list) else [node.body]
            for st in body:
                fn_lines.update(range(st.lineno, (st.end_lineno or st.lineno) + 1))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            dyn.add(node.value)
        elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            if node.lineno in imp_lines and node.lineno in fn_lines:
                dyn.add(node.id if isinstance(node, ast.Name) else node.attr)
write_json(OUT / "dynattrs.json", sorted(dyn))
print("dynamic attribute names", len(dyn))
print("done ->", OUT)

# ------------------------------------------------- subprocess coverage
# Tests that run `python -c ...` in a subprocess (lab/subproc_cov.py): the
# lines that subprocess executes, as ranges per file. Unresolved subprocess
# tests would make the selector unsafe, so they stop the build.
sp_path = WORK / "out" / "subproc.json"
if sp_path.exists():
    sp = json.loads(sp_path.read_text())
    if sp.get("unresolved"):
        sys.exit(f"unresolved subprocess tests: {sp['unresolved']}")
    out_sp = {}
    for nodeid, per_file in sp.items():
        if nodeid == "unresolved" or nodeid not in bit:
            continue
        rng = {}
        for path, lines in per_file.items():
            runs, start, prev = [], None, None
            for ln in sorted(lines):
                if start is None:
                    start = prev = ln
                elif ln == prev + 1:
                    prev = ln
                else:
                    runs.append([start, prev])
                    start = prev = ln
            if start is not None:
                runs.append([start, prev])
            rng[path] = runs
        out_sp[str(bit[nodeid])] = rng
    write_json(OUT / "subproc.json", out_sp)
    print("subprocess tests", len(out_sp))

# ------------------------------------------- dict-constant key reads
# out/dict-*.json (lab/brdict.py): per context, "path:line|<key repr>" for a
# key read, "path:line|*" plus "path:line|*<op>" for a whole-dict read. Map
# per file: {"<line>": "*" (no pruning) | {"all": hex, "keys": {repr: hex | "*"}}}.
# A dict is unprunable when it was read whole outside a test, or copied
# (iterated, items(), copy, merged, repr...) inside one (a lazily built copy
# can serve later tests); only truth/len tests count as whole reads of that
# test alone. A key read outside a test makes that key unprunable.
NON_COPY = {"*__bool__", "*__len__"}
d_star, d_all, d_keys, d_keystar = set(), collections.defaultdict(set), collections.defaultdict(
    lambda: collections.defaultdict(set)), collections.defaultdict(set)
d_sites = set()
for p in (WORK / "out").glob("dict-sites-*.json"):
    d_sites.update(json.loads(p.read_text()))
for p in (WORK / "out").glob("dict-*.json"):
    if p.name.startswith("dict-sites-"):
        continue
    for key, reads in json.loads(p.read_text()).items():
        nodeid, setup = (key[: -len("|setup")], True) if key.endswith("|setup") else (key, False)
        b = bit.get(nodeid)
        tb = (class_mates(nodeid) | {b} if setup else {b}) if b is not None else None
        for r in reads:
            site, _, k = r.partition("|")
            if k.startswith("*") and k != "*":
                if tb is None or k not in NON_COPY:
                    d_star.add(site)
                continue
            if k == "*":
                if tb is None:
                    d_star.add(site)
                else:
                    d_all[site].update(tb)
                continue
            if tb is None:
                d_keystar[site].add(k)
            else:
                d_keys[site][k].update(tb)
dmap = collections.defaultdict(dict)
for s in d_sites:
    path, line = s.rsplit(":", 1)
    if s in d_star:
        dmap[path][line] = "*"
        continue
    keys = {k: hexbits(v) for k, v in d_keys[s].items()}
    for k in d_keystar[s]:
        keys[k] = "*"
    dmap[path][line] = {"all": hexbits(d_all[s]), "keys": keys}
write_json(OUT / "dicts.json", dmap)
print("dict sites", len(d_sites), "unprunable", len(d_star))
