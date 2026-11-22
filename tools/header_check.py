"""Lab-only check of header (default argument) edits in functions whose body
starts with a docstring: bool flip / int+1 of a default, real suite.
Usage: python header_check.py N seed -> out/header_check.jsonl"""

import ast
import difflib
import json
import random
import sys

import campaign


def candidates():
    out = []
    for f in sorted((campaign.SRC / "sqlglot").rglob("*.py")):
        rel = f.relative_to(campaign.SRC).as_posix()
        try:
            tree = ast.parse(f.read_text("utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            b = fn.body
            if not (len(b) > 1 and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                continue
            for d in fn.args.defaults + [x for x in fn.args.kw_defaults if x is not None]:
                if isinstance(d, ast.Constant) and type(d.value) in (bool, int) and d.lineno == d.end_lineno:
                    out.append((rel, d.lineno, d.col_offset, d.end_col_offset, d.value))
    return out


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    cands = candidates()
    print("candidates", len(cands), flush=True)
    rng.shuffle(cands)
    for k, (path, line, c0, c1, val) in enumerate(cands[:n]):
        lines = (campaign.SRC / path).read_text("utf-8").split("\n")
        text = lines[line - 1]
        new = repr(not val) if type(val) is bool else repr(val + 1)
        new_lines = lines[: line - 1] + [text[:c0] + new + text[c1:]] + lines[line:]
        body = "".join(difflib.unified_diff([x + "\n" for x in lines], [x + "\n" for x in new_lines],
                                            f"a/{path}", f"b/{path}", n=3))
        patch = f"diff --git a/{path} b/{path}\nindex 1234567..89abcde 100644\n" + body
        sel, notes = campaign.selection(patch)
        name = f"hd{seed}_{k}"
        (campaign.MUT / f"{name}.patch").write_text(patch, "utf-8", newline="\n")
        fail, summary = campaign.run_tests(name)
        fail_t = {f for f in fail if "::" in f}
        missed = sorted(fail_t - sel)
        rec = {"name": name, "file": path, "line": line, "selected": len(sel), "failing": sorted(fail_t),
               "missed": missed, "summary": summary, "notes": notes}
        with open(campaign.WORK / "out" / "header_check.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{name} {path}:{line} sel={len(sel)} fail={len(fail_t)} MISSED={len(missed)} {notes} {summary}",
              flush=True)


if __name__ == "__main__":
    main()
