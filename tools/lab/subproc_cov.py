"""Lab-only: line coverage of Python subprocesses that tests launch.

Some tests run `python -c <code>` in a subprocess (test_lazy_load imports
sqlglot in a fresh interpreter); the per-test coverage run never sees those
lines. This finds such calls in tests/ (subprocess.run/check_output/Popen/
call with ["python" | sys.executable, "-c", <code>], <code> a literal or a
name bound to one in the same test function), runs each code under coverage
in a fresh interpreter (cwd = the sqlglot checkout, as pytest runs), and
writes /out/subproc.json: {test nodeid: {file: [lines]}}. Tests whose call
cannot be resolved are listed under "unresolved".

Run in br-lab: python /lab/subproc_cov.py
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/src/sqlglot")
CALLS = {"run", "check_output", "check_call", "call", "Popen"}

CHILD = r"""
import json, sys, coverage
cov = coverage.Coverage(data_file=None, include=["/src/sqlglot/sqlglot/*"])
cov.start()
try:
    exec(compile(sys.argv[1], "<string>", "exec"), {"__name__": "__main__"})
finally:
    cov.stop()
data = cov.get_data()
print(json.dumps({f.replace("/src/sqlglot/", ""): sorted(data.lines(f) or ()) for f in data.measured_files()}))
"""


def string_of(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def find(test_file):
    tree = ast.parse(test_file.read_text("utf-8"))
    rel = test_file.relative_to(ROOT).as_posix()
    out = []

    def walk_fn(fn, prefix):
        consts = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = string_of(n.value, {})
                if v is not None:
                    consts[n.targets[0].id] = v
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name not in CALLS or not n.args or not isinstance(n.args[0], (ast.List, ast.Tuple)):
                continue
            elts = n.args[0].elts
            exe = elts[0] if elts else None
            is_py = (isinstance(exe, ast.Constant) and str(exe.value).startswith("python")) or (
                isinstance(exe, ast.Attribute) and exe.attr == "executable")
            if not is_py:
                continue
            code = None
            for i, e in enumerate(elts[:-1]):
                if isinstance(e, ast.Constant) and e.value == "-c":
                    code = string_of(elts[i + 1], consts)
            out.append((f"{rel}::{prefix}{fn.name}", code))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test"):
                    walk_fn(fn, node.name + "::")
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            walk_fn(node, "")
    return out


def main():
    result, unresolved = {}, []
    for tf in sorted((ROOT / "tests").rglob("test_*.py")):
        for nodeid, code in find(tf):
            if code is None:
                unresolved.append(nodeid)
                continue
            p = subprocess.run([sys.executable, "-c", CHILD, code], cwd=ROOT, capture_output=True, text=True)
            if p.returncode != 0:
                print(nodeid, "child failed:", p.stderr[-500:], file=sys.stderr)
                unresolved.append(nodeid)
                continue
            lines = json.loads(p.stdout.strip().splitlines()[-1])
            result[nodeid] = lines
            print(nodeid, len(lines), "files", sum(len(v) for v in lines.values()), "lines", file=sys.stderr)
    result["unresolved"] = unresolved
    Path("/out/subproc.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
