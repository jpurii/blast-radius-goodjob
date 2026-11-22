"""Blast Radius selector (team: good job).

For each patch against the pinned sqlglot commit, choose the tests that can
fail, from maps built offline on that same commit:

- coverage: which tests run each line (function bodies),
- class usage: which tests use each class, through its whole hierarchy
  (class-level constants and tables are read through instances),
- name use: which tests run a line that loads a given name (module-level
  constants, imports),
- opens: which tests read each fixture file.

A changed line expands to its whole statement header, so a mutated
condition in a multi-line `if` reaches tests that short-circuit past it.
Comment-only edits select nothing. Anything we cannot place selects every
test: a miss costs 300 s, far more than over-selecting.

Then the selection is packed onto 1-8 workers (longest first onto the least
loaded), keeping the cheapest of busiest-worker seconds + 2 s per worker.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
MAPS = APP / "maps"
SRC = Path(os.environ.get("BR_SRC", APP / "src"))  # sqlglot sources at the pinned commit

IMPORT_WIDE = 50  # import-time code run by at least this many tests is trusted to coverage
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SETUP_NAMES = {"setUpClass", "tearDownClass", "setUpModule", "tearDownModule"}
HARMLESS_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg", ".html", ".css")
HARMLESS_PREFIXES = ("docs/", "pdoc/", "posts/", "benchmarks/", ".github/", "sqlglot-integration-tests/")


def log(*args):
    print(*args, file=sys.stderr)


def read_map(path: Path):
    return json.loads(gzip.decompress(path.with_name(path.name + ".gz").read_bytes()))


class _Everything:
    def __contains__(self, item):
        return True


class Maps:
    def __init__(self):
        self.ids = read_map(MAPS / "tests.json")
        self._cov = {}
        self._json = {}

    def get(self, name):
        if name not in self._json:
            self._json[name] = read_map(MAPS / name)
        return self._json[name]

    def cov(self, path):
        if path not in self._cov:
            p = MAPS / "cov" / (path.replace("/", "__") + ".json")
            if p.with_name(p.name + ".gz").exists():
                d = read_map(p)
                sets = [int(h, 16) for h in d["sets"]]
                lines = {int(k): sets[v] for k, v in d["lines"].items()}
                spans = {int(k): v for k, v in d["spans"].items()}
                mls = set()
                for a, b in d.get("mls", []):
                    mls.update(range(a, b + 1))
                self._cov[path] = (lines, spans, mls, set(d.get("imp", ())))
            else:
                self._cov[path] = None
        return self._cov[path]

    @property
    def dyn(self):
        if "_dyn" not in self.__dict__:
            p = MAPS / "dynattrs.json"
            self._dyn = set(read_map(p)) if p.with_name(p.name + ".gz").exists() else None
        # Without the list, treat every name as dynamic (no narrowing).
        return self._dyn if self._dyn is not None else _Everything()

    def bits(self, name, key):
        h = self.get(name).get(key)
        return int(h, 16) if h else 0


def parse_patch(text):
    """-> list of (old_path, new_path, hunks); hunk = (old_start, [(tag, text)])."""
    files = []
    cur = None
    old_path = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = None
            hunk = None
        elif line.startswith("--- ") and hunk is None:
            old_path = line[4:].strip()
            old_path = old_path[2:] if old_path.startswith("a/") else old_path
        elif line.startswith("+++ ") and hunk is None:
            new_path = line[4:].strip()
            new_path = new_path[2:] if new_path.startswith("b/") else new_path
            cur = (old_path, new_path, [])
            files.append(cur)
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m and cur is not None:
                hunk = (int(m.group(1)), [])
                cur[2].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append((line[0], line[1:]))
        elif hunk is not None and line.startswith("\\"):
            continue
    return files


def code_tokens(lines):
    """Token strings of some source lines, without comments or layout."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO("\n".join(lines) + "\n").readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        out.append("\0untokenizable")
        out.append("\n".join(lines))
    return out


def apply_hunks(old_lines, hunks):
    """New file text from the pinned file's lines and the patch hunks, or
    None if the hunks do not fit the file."""
    out = []
    pos = 0  # next old line index (0-based) not yet copied
    for start, lines in hunks:
        # "@@ -N,0" (no old lines) inserts after line N; otherwise N is the
        # hunk's first old line.
        has_old = any(tag != "+" for tag, _ in lines)
        i = max(start - 1, 0) if has_old else start
        if i < pos or i > len(old_lines):
            return None
        out.extend(old_lines[pos:i])
        pos = i
        for tag, text in lines:
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != text:
                    return None
                out.append(text)
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != text:
                    return None
                pos += 1
            else:
                out.append(text)
    out.extend(old_lines[pos:])
    return "\n".join(out) + "\n"


def file_tokens(text):
    """Every code token of a file (comments and blank lines left out), or None
    if it does not tokenize."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL):
                continue
            out.append((tok.type, tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


# Files whose classes compare in ways that break on None (env.py's reverse
# sort key does `other.obj == self.obj`).
NONE_EQ_UNSAFE = {"sqlglot/executor/env.py"}
_IS_NOT_NONE = re.compile(r"\bis\s+not\s+None\b")
_IS_NONE = re.compile(r"\bis\s+None\b")
_NE_NONE = re.compile(r"!=\s*None\b")
_EQ_NONE = re.compile(r"(?<![=!<>])==\s*None\b")


def none_compare_swap(removed, added):
    """True when one line changed and the only difference is an identity vs
    equality comparison with None (is None / == None, is not None / != None)."""
    if len(removed) != 1 or len(added) != 1 or removed[0] == added[0]:
        return False

    def norm(s):
        s = _IS_NOT_NONE.sub("!= None", s)
        s = _IS_NONE.sub("== None", s)
        s = _NE_NONE.sub("!= None", s)
        return _EQ_NONE.sub("== None", s)

    return norm(removed[0]) == norm(added[0])


def hunk_regions(hunk):
    """Old-side line numbers touched by the hunk: removed lines, plus the
    lines on both sides of every insertion point. Also returns the removed and
    added text blocks."""
    old = hunk[0]
    changed, around, removed, added = set(), set(), [], []
    for tag, text in hunk[1]:
        if tag == " ":
            old += 1
        elif tag == "-":
            changed.add(old)
            removed.append(text)
            old += 1
        else:
            around.add(old - 1)
            around.add(old)
            added.append(text)
    return changed, around, removed, added


class Selector:
    def __init__(self, maps: Maps, n_tests: int):
        self.m = maps
        self.all = (1 << n_tests) - 1

    def select(self, patch_text) -> tuple[int, list[str]]:
        sel = 0
        notes = []
        # File changes git writes without ---/+++ hunks (renames, copies,
        # binary files, empty new or deleted files) would otherwise be
        # invisible: they can break any test, so they select everything.
        # A mode-only change (the executable bit) cannot affect a test.
        for block in re.split(r"(?m)^(?=diff --git )", patch_text):
            if not block.startswith("diff --git "):
                continue
            has_hunks = re.search(r"(?m)^@@ ", block) is not None
            if re.search(r"(?m)^(rename from|copy from|Binary files|GIT binary patch)", block):
                return self.all, ["ALL(rename/copy/binary)"]
            if not has_hunks and re.search(r"(?m)^(new file mode|deleted file mode)", block):
                # An empty file added or deleted: same rules as any other
                # added/deleted file (an unimported new module is harmless).
                m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
                if not m:
                    return self.all, ["ALL(empty file added/deleted)"]
                added = re.search(r"(?m)^new file mode", block) is not None
                path = m.group(2) if added else m.group(1)
                old_p, new_p = ("/dev/null", path) if added else (path, "/dev/null")
                b, note = self.select_file(old_p, new_p, path, [])
                sel |= b
                notes.append(f"{path.split('/')[-1]}:{note}(empty)")
            if not has_hunks and not re.search(r"(?m)^(old mode|new mode|new file mode|deleted file mode)", block):
                return self.all, ["ALL(unparsed file change)"]
        parsed = parse_patch(patch_text)
        if not parsed and patch_text.strip() and "diff --git " not in patch_text:
            # Not a git-style diff we understand at all.
            return self.all, ["ALL(unparsed patch)"]
        for old_path, new_path, hunks in parsed:
            path = new_path if old_path == "/dev/null" else old_path
            bits, note = self.select_file(old_path, new_path, path, hunks)
            sel |= bits
            notes.append(f"{path.split('/')[-1]}:{note}")
        return sel, notes

    def select_file(self, old_path, new_path, path, hunks):
        if path.startswith(HARMLESS_PREFIXES) or (
            path.endswith(HARMLESS_SUFFIXES) and not path.startswith(("tests/", "sqlglot/"))
        ):
            return 0, "harmless"
        if old_path == "/dev/null" or new_path == "/dev/null":
            if new_path == "/dev/null" and path.startswith("tests/fixtures/"):
                return self.fixture(path, None)
            if old_path == "/dev/null" and path.startswith(("sqlglot/", "tests/")) and path.endswith(".py") \
                    and not path.endswith("__init__.py") and not path.split("/")[-1].startswith(("test_", "conftest")):
                return 0, "new-module"
            return self.all, "ALL(add/delete)"
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            return self.python(path, hunks)
        if path.startswith("tests/fixtures/"):
            return self.fixture(path, hunks)
        return self.all, "ALL(unknown file)"

    _subproc = None

    def subproc_bits(self, path, stmt_lines, spans, touched):
        """Tests that run Python in a subprocess (test_lazy_load imports
        sqlglot in a fresh interpreter): line coverage never sees what that
        subprocess runs, so maps/subproc.json lists its lines. A test is
        added when the hunk touches a statement whose lines it ran."""
        if self._subproc is None:
            self._subproc = {}
            if (MAPS / "subproc.json.gz").exists():
                for b, per_file in self.m.get("subproc.json").items():
                    for p, runs in per_file.items():
                        self._subproc.setdefault(p, []).append((int(b), runs))
        entries = self._subproc.get(path)
        if not entries:
            return 0
        lines = set(touched)
        for ln in stmt_lines:
            span = spans.get(ln)
            if span is not None:
                lines.update(range(span[0], span[1] + 1))
        out = 0
        for b, runs in entries:
            if any(a <= x <= z for a, z in runs for x in lines):
                out |= 1 << b
        return out

    _helper_mods = {}

    def run_loader(self, loader, path, text):
        """Evaluate a setUpClass loader expression (from maps/setup.json)
        with the pinned helper functions, reading `text` for the fixture."""
        import builtins
        import io

        env = {}
        for name, mod in loader["mods"].items():
            g = self._helper_mods.get(mod)
            if g is None:
                f = SRC / mod
                g = {"__name__": "br_" + mod.replace("/", "_")[:-3], "__file__": str(f)}
                exec(compile(f.read_text("utf-8"), str(f), "exec"), g)
                self._helper_mods[mod] = g

            def fake_open(file, *a, _p=path, _t=text, **k):
                if str(file).replace("\\", "/").endswith(_p):
                    return io.StringIO(_t)
                return builtins.open(file, *a, **k)

            g["open"] = fake_open
            env[name] = g[name]
        return eval(loader["expr"], {"__builtins__": builtins}, env)

    def setup_fixture(self, path, hunks):
        """A fixture that a test class's setUpClass loads into an attribute
        (maps/setup.json). If the patched file makes the loader raise, the
        class setup fails and every test of the class errors; if the loaded
        value is unchanged, no test that reads the attribute can see the
        edit; otherwise the attribute's readers. Plus tests that open the
        file themselves. None: not such a fixture."""
        if not (MAPS / "setup.json.gz").exists():
            return None
        e = self.m.get("setup.json").get(path)
        if not e:
            return None
        cls, readers, direct = (int(e[k], 16) for k in ("cls", "readers", "direct"))
        if hunks is None:
            return cls | direct, "fixture-setup(deleted)"
        try:
            old_text = (SRC / path).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return cls | direct, "fixture-setup(no source)"
        lines = old_text.split("\n")[:-1] if old_text.endswith("\n") else old_text.split("\n")
        new_text = apply_hunks(lines, hunks)
        if new_text is None:
            return cls | direct, "fixture-setup(hunks do not fit)"
        changed = False
        for loader in e["loaders"]:
            try:
                before = self.run_loader(loader, path, old_text)
            except Exception:  # noqa: BLE001
                return cls | direct, "fixture-setup(loader fails on pinned file)"
            try:
                after = self.run_loader(loader, path, new_text)
            except Exception:  # noqa: BLE001
                return cls | direct, "fixture-setup(class setup fails)"
            if before != after:
                changed = True
        return direct | (readers if changed else 0), "fixture-setup" + ("" if changed else "(same data)")

    def fixture(self, path, hunks):
        s = self.setup_fixture(path, hunks)
        if s is not None:
            return s
        h = self.m.get("opens.json").get(path)
        if h:
            return int(h, 16), "fixture"
        # Never opened in our trace: take every test that reads a fixture
        # from the same folder.
        folder = path.rsplit("/", 1)[0] + "/"
        bits = 0
        for p, hh in self.m.get("opens.json").items():
            if p.startswith(folder):
                bits |= int(hh, 16)
        return (bits or self.all), "fixture-dir" if bits else "ALL(fixture)"

    def python(self, path, hunks):
        c = self.m.cov(path)
        if c is None:
            return self.all, "ALL(no map)"
        lines_cov, spans, mls, imp = c
        whole = self.whole_file(path, hunks)
        if whole is not None:
            return whole
        if not lines_cov and not imp and spans:
            # No line of this file ran during the whole test session, not even
            # at import or collection (a script like tests/gen_fixtures.py):
            # no test can see the change.
            return 0, "never loaded"
        bits = 0
        notes = []
        for hunk in hunks:
            changed, around, removed, added = hunk_regions(hunk)
            touched = changed | around
            in_string = any(ln in mls for ln in touched)
            if not in_string and code_tokens(removed) == code_tokens(added):
                notes.append("comment")
                continue
            if not in_string and path not in NONE_EQ_UNSAFE and none_compare_swap(removed, added):
                # `x is None` <-> `x == None` (or the negations): every
                # __eq__ in sqlglot returns False for None, so the result
                # cannot change (an equivalent mutant).
                notes.append("equivalent(None)")
                continue
            # Statements the hunk touches. Removed lines name them directly;
            # an insertion point names the statements on either side.
            stmt_lines = set(changed)
            for ln in around:
                if ln in spans:
                    stmt_lines.add(ln)
            if not stmt_lines:
                # Inserted between statements on blank lines: walk outwards.
                for ln in around:
                    up = ln
                    while up > 0 and up not in spans:
                        up -= 1
                    down = ln
                    while down < ln + 200 and down not in spans:
                        down += 1
                    stmt_lines.update(x for x in (up, down) if x in spans)
            bits |= self.subproc_bits(path, stmt_lines, spans, touched)
            for ln in stmt_lines:
                span = spans.get(ln)
                if span is None:
                    b = lines_cov.get(ln, 0)
                    bits |= b
                    continue
                first, last, kind, scope, extra = span
                if kind == "f" and path.startswith("sqlglot/") and any(
                        x in imp for x in range(first, last + 1)):
                    # This function body runs while sqlglot is imported
                    # (metaclasses, table builders): what it builds is used by
                    # tests that never run the line themselves. Lines that
                    # many tests also run (the parser) are reached through
                    # coverage; narrowly covered ones are not.
                    span_bits = 0
                    for x in range(first, last + 1):
                        span_bits |= lines_cov.get(x, 0)
                    if bin(span_bits).count("1") < IMPORT_WIDE:
                        bits |= self.all
                        notes.append("ALL(runs at import)")
                        continue
                sb = 0
                for x in range(first, last + 1):
                    sb |= lines_cov.get(x, 0)
                if extra is not None:
                    sb |= lines_cov.get(extra, 0)
                if kind == "f" and path.startswith("sqlglot/"):
                    mask = self.weak_mask(path, hunk)
                    if mask is not None:
                        # Weak mutation: of the tests that ran the line, only
                        # those where the swapped operator would have changed
                        # the outcome at least once can be affected.
                        bits |= sb & mask
                        notes.append("weak-mutation")
                        continue
                bits |= sb
                if kind in ("c", "m") and self.inside_lambda_body(path, hunk):
                    # Only the lambda's body changed; it runs when called, so
                    # the line's coverage is exactly the tests that call it.
                    notes.append("lambda-body")
                    continue
                b, why = self.scope_tests(path, kind, scope, extra, first, last, hunk)
                bits |= b
                notes.append(why)
        return bits, ",".join(sorted(set(notes))) or "code"

    def whole_file(self, path, hunks):
        """Checks on the patched file as a whole, when the pinned sources ship
        with the image: a file that no longer compiles breaks every test that
        imports it; a file whose code tokens are unchanged (comments and blank
        lines only) changes no behavior. None means "decide hunk by hunk"."""
        src = SRC / path
        if not src.is_file():
            return None
        old_text = src.read_text("utf-8")
        new_text = apply_hunks(old_text.split("\n")[:-1] if old_text.endswith("\n") else old_text.split("\n"), hunks)
        if new_text is None:
            return None
        try:
            compile(new_text, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return self.all, "ALL(no longer compiles)"
        old_tok, new_tok = file_tokens(old_text), file_tokens(new_text)
        if old_tok is not None and old_tok == new_tok:
            return 0, "comment(file)"
        return None

    _IMPORT_LINE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\b)")
    _import_spans = {}

    def import_stmt(self, path, first, hunk):
        """True when the hunk adds or removes an import line, or the touched
        statement (starting at `first`) is an import outside any function."""
        import ast

        if any(tag in "+-" and self._IMPORT_LINE.match(text) for tag, text in hunk[1]):
            return True
        if path not in self._import_spans:
            spans = []
            try:
                tree = ast.parse((SRC / path).read_text("utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None

            def walk(node):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        spans.append((child.lineno, child.end_lineno))
                    walk(child)

            if tree is not None:
                walk(tree)
            self._import_spans[path] = spans
        return any(a <= first <= b for a, b in self._import_spans[path])

    def scope_tests(self, path, kind, scope, extra, first, last, hunk):
        is_test_file = path.startswith("tests/")
        if kind == "f":
            fn = scope.rsplit("|", 1)[-1]
            if is_test_file and fn in SETUP_NAMES:
                cls = scope.split("|")[0].rsplit(".", 1)[-1]
                key = f"{path}::{cls}"
                b = self.m.bits("classtests.json", key) or self.m.bits("files.json", path)
                return b, "setup"
            return 0, "f"
        if kind == "c":
            if extra is not None:
                return 0, "method-header"  # callers come from the body's coverage
            if is_test_file:
                cls = scope.rsplit(".", 1)[-1]
                b = self.m.bits("classtests.json", f"{path}::{cls}") or self.m.bits("files.json", path)
                return b, "test-class-body"
            if self.import_stmt(path, first, hunk):
                return self.all, "ALL(class-body import)"
            b = self.m.bits("classes.json", scope)
            if not b:
                # No test ran a method on this class (enums, plain constant
                # holders): its attributes are read in ways we did not trace.
                return self.all, f"ALL(class {scope.rsplit('.', 1)[-1]} unused)"
            narrow = self.narrow_constant(path, first, last, hunk, b)
            if narrow is not None:
                return narrow, f"const({scope.rsplit('.', 1)[-1]})"
            return b, f"class({scope.rsplit('.', 1)[-1]})"
        # module level
        if is_test_file and path.split("/")[-1].startswith("test_"):
            return self.m.bits("files.json", path), "test-module"
        if self.import_stmt(path, first, hunk):
            # An import that runs while the module loads: a new, removed or
            # changed one can fail every importer (circular imports) or load
            # modules early (test_lazy_load), far beyond the names it binds.
            return self.all, "ALL(module import)"
        names = set()
        for tag, text in hunk[1]:
            if tag in "+-":
                names.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text.split("#", 1)[0]))
        bound = self.bound_names(hunk)
        if not bound:
            # The edit is inside a multi-line statement (a dict or list
            # entry): the names it binds are on the statement's first line.
            bound = self.statement_binds(path, first, last)
        b = 0
        for n in bound:
            b |= self.m.bits("names.json", n)
        if extra is not None:
            return b, "def-header"
        if not bound:
            return self.all, "ALL(module stmt)"
        if not b:
            # Nothing a test ran loads these names: they are only used at
            # import time (or by name strings), which we cannot place.
            return self.all, f"ALL(module {','.join(sorted(bound))[:30]} unplaced)"
        return b, f"module({','.join(sorted(bound))[:40]})"

    _trees = {}
    SWAPS = {("and", "or"), ("or", "and"), ("<", "<="), ("<=", "<"), (">", ">="), (">=", ">"),
             ("is", "=="), ("==", "is"), ("is not", "!="), ("!=", "is not")}
    EQ_OPS = {"is": "Is", "is not": "IsNot", "==": "Eq", "!=": "NotEq"}
    PURE_CALLS = {"isinstance", "issubclass", "len", "type", "callable", "hasattr"}
    PURE_METHODS = {"get"}

    def weak_mask(self, path, hunk):
        """For a one-line swap of `and`/`or` in a two-operand condition, or of
        `<`/`<=`, `>`/`>=`, `is`/`==` or `is not`/`!=` in a single comparison, the tests in which that
        swap would have changed the outcome at least once (from the lab's
        weak-mutation profile, maps/wm.json). None: no such knowledge."""
        import ast

        changed, around, removed, added = hunk_regions(hunk)
        if len(changed) != 1 or len(removed) != 1 or len(added) != 1:
            return None
        old, new = removed[0], added[0]
        if not old.isascii() or not new.isascii():
            return None
        tok = re.compile(r"\bis\s+not\b|\w+|==|!=|<=|>=|->|\*\*|//|<<|>>|\S")
        ot = [(re.sub(r"\s+", " ", m.group()), m.start()) for m in tok.finditer(old)]
        nt = [re.sub(r"\s+", " ", m.group()) for m in tok.finditer(new)]
        if len(ot) != len(nt):
            return None
        diffs = [i for i in range(len(ot)) if ot[i][0] != nt[i]]
        if len(diffs) != 1 or (ot[diffs[0]][0], nt[diffs[0]]) not in self.SWAPS:
            return None
        col = ot[diffs[0]][1]
        ln = next(iter(changed))
        sites = self.m.get("wm.json").get(path) if (MAPS / "wm.json.gz").exists() else None
        if not sites:
            return None
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse((SRC / path).read_text("utf-8"))
            except (OSError, SyntaxError):
                self._trees[path] = None
        tree = self._trees[path]
        if tree is None:
            return None
        pos = (ln, col)
        for n in ast.walk(tree):
            if isinstance(n, ast.BoolOp) and 3 <= len(n.values) <= 6:
                # a chain: find which gap holds the swapped operator
                vals = n.values
                gap = next((p for p in range(len(vals) - 1)
                            if (vals[p].end_lineno, vals[p].end_col_offset) <= pos
                            < (vals[p + 1].lineno, vals[p + 1].col_offset)), None)
                if gap is None:
                    continue
                op = "and" if isinstance(n.op, ast.And) else "or"
                if ot[diffs[0]][0] != op or not all(self.pure(v) for v in vals[1:]):
                    return None
                v = sites.get(f"{n.lineno}:{n.col_offset}#{gap}")
                if v is None or v == "*":
                    return None
                return int(v, 16)
            if isinstance(n, ast.BoolOp) and len(n.values) == 2:
                a, b = n.values
                if not ((a.end_lineno, a.end_col_offset) <= pos < (b.lineno, b.col_offset)):
                    continue
                op = "and" if isinstance(n.op, ast.And) else "or"
                if ot[diffs[0]][0] != op or not self.pure(b):
                    return None
            elif isinstance(n, ast.Compare) and len(n.ops) == 1:
                a, b = n.left, n.comparators[0]
                if not ((a.end_lineno, a.end_col_offset) <= pos < (b.lineno, b.col_offset)):
                    continue
                o = ot[diffs[0]][0]
                if o in self.EQ_OPS:
                    # is <-> ==, is not <-> != : the node's operator must be the one replaced
                    if type(n.ops[0]).__name__ != self.EQ_OPS[o]:
                        return None
                elif o not in ("<", "<=", ">", ">="):
                    return None
            else:
                continue
            v = sites.get(f"{n.lineno}:{n.col_offset}")
            if v is None or v == "*":
                return None
            return int(v, 16)
        return None

    def pure(self, node):
        """No side effects when evaluated (names, attributes, constants,
        comparisons, and a few known-pure calls)."""
        import ast

        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name) and f.id in self.PURE_CALLS:
                    continue
                if isinstance(f, ast.Attribute) and f.attr in self.PURE_METHODS:
                    continue
                return False
            if isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda,
                              ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                return False
        return True

    _lambdas = {}

    def inside_lambda_body(self, path, hunk):
        """True when the hunk replaces exactly one line, and the old and new
        text differ only inside the body of a lambda that sits on that line
        (not its parameters, whose defaults run at definition)."""
        import ast

        changed, around, removed, added = hunk_regions(hunk)
        if len(changed) != 1 or len(removed) != 1 or len(added) != 1:
            return False
        ln = next(iter(changed))
        old, new = removed[0], added[0]
        # The differing column range in the old line.
        i = 0
        while i < min(len(old), len(new)) and old[i] == new[i]:
            i += 1
        j = 0
        while j < min(len(old), len(new)) - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
            j += 1
        lo, hi = i, len(old) - j  # old[lo:hi] was replaced
        if path not in self._lambdas:
            src = SRC / path
            spans = []
            if src.is_file():
                try:
                    tree = ast.parse(src.read_text("utf-8"))
                    for n in ast.walk(tree):
                        if isinstance(n, ast.Lambda) and n.body.lineno == n.body.end_lineno:
                            spans.append((n.body.lineno, n.body.col_offset, n.body.end_col_offset))
                except SyntaxError:
                    pass
            self._lambdas[path] = spans
        # col offsets are UTF-8 byte offsets; only trust ASCII lines
        if not old.isascii():
            return False
        return any(line == ln and c0 <= lo and hi <= c1 for line, c0, c1 in self._lambdas[path])

    @staticmethod
    def statement_binds(path, first, last):
        """Names bound by the module-level statement on lines first..last of
        the pinned source (empty if it cannot be parsed)."""
        import ast
        import textwrap

        src = SRC / path
        if not src.is_file():
            return set()
        lines = src.read_text("utf-8").split("\n")
        # A span's `last` is its header end for compound statements; for a
        # simple statement it is the whole statement.
        text = textwrap.dedent("\n".join(lines[first - 1:last]))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        out = set()
        for st in tree.body:
            targets = []
            if isinstance(st, ast.Assign):
                targets = st.targets
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                targets = [st.target]
            for tg in targets:
                for n in ast.walk(tg):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
        return out

    ASSIGN = re.compile(r"\s*([A-Z_][A-Z0-9_]*)\s*(?::[^=]*)?=(?!=)")

    def narrow_constant(self, path, first, last, hunk, class_bits):
        """An edit to an UPPER_CASE class constant `NAME = ...` reaches only
        tests that use the class AND run a line reading NAME, unless NAME is
        also read in ways no test line shows (import-time code, getattr by
        string). None means "no narrowing"."""
        src = SRC / path
        if not src.is_file():
            return None
        lines = src.read_text("utf-8").split("\n")
        if first < 1 or first > len(lines):
            return None
        m = self.ASSIGN.match(lines[first - 1])
        if not m:
            return None
        # Every changed line must belong to this one statement.
        changed, around, _, added = hunk_regions(hunk)
        if not changed or any(ln < first or ln > last for ln in changed):
            return None
        name = m.group(1)
        if name in self.m.dyn:
            return None
        users = self.m.bits("names.json", name)
        narrowed = class_bits & users
        return narrowed or None

    @staticmethod
    def bound_names(hunk):
        """Names a module-level statement binds, from the hunk's +/- lines."""
        out = set()
        for tag, text in hunk[1]:
            if tag not in "+-":
                continue
            s = text.split("#", 1)[0]
            m = re.match(r"\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", s)
            if m:
                out.add(m.group(1))
                continue
            m = re.match(r"\s*from\s+\S+\s+import\s+(.*)", s)
            if m:
                for part in m.group(1).replace("(", "").replace(")", "").split(","):
                    part = part.strip()
                    if part:
                        out.add(part.split(" as ")[-1].strip())
                continue
            m = re.match(r"\s*import\s+(.*)", s)
            if m:
                for part in m.group(1).split(","):
                    part = part.strip()
                    if part:
                        out.add(part.split(" as ")[-1].strip().split(".")[0])
                continue
            m = re.match(r"\s*([A-Za-z_][\w\s,]*?)\s*(?::[^=]*)?(?:[+\-*/|&]?=)(?!=)", s)
            if m:
                for part in m.group(1).split(","):
                    if part.strip().isidentifier():
                        out.add(part.strip())
        return out


def pack(selected, ms):
    if not selected:
        return []
    order = sorted(selected, key=lambda n: ms[n], reverse=True)
    best = None
    for w in range(1, 9):
        loads = [0] * w
        workers = [[] for _ in range(w)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            workers[i].append(n)
        cost = max(loads) + 2000 * w
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    number_by_id = {t["id"]: t["number"] for t in tests}
    maps = Maps()
    # Our bit order -> this run's test numbers. Tests we have no map for are
    # always selected.
    bit_to_num = [number_by_id.get(tid) for tid in maps.ids]
    unknown = set(number_by_id.values()) - {n for n in bit_to_num if n is not None}
    sel = Selector(maps, len(maps.ids))
    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            bits, notes = sel.select(patch.read_text("utf-8", errors="replace"))
        except Exception as error:  # noqa: BLE001
            bits, notes = sel.all, [f"ERROR {error!r}"]
        nums = set(unknown)
        i = 0
        while bits:
            if bits & 1:
                n = bit_to_num[i]
                if n is not None:
                    nums.add(n)
            bits >>= 1
            i += 1
        # PROBE (practice-board diagnostic only, never a final entry): drop
        # the two slowest tests to measure how much of the practice cost
        # they make and on how many commits they really fail.
        nums -= {number_by_id.get(x) for x in (
            "tests/test_parser.py::TestParser::test_max_nodes",
            "tests/test_executor.py::TestExecutor::test_execute_tpcds")}
        workers = pack(nums, ms)
        out.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        log(patch.stem, len(nums), " ".join(notes))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_SELF)
        log(f"cpu {r.ru_utime + r.ru_stime:.3f} s")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
