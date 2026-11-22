"""Lab-only check of the dict-key rule: random one-line value edits inside
module/class-level dict displays in sqlglot (int +1, bool flip, string
changed) where the selector applies the rule, each run against the real
suite. Usage: python dict_check.py N seed -> out/dict_check.jsonl"""

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
        src = f.read_text("utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        def walk(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(child, (ast.Assign, ast.AnnAssign)) and isinstance(child.value, ast.Dict):
                    for k, v in zip(child.value.keys, child.value.values):
                        if not isinstance(k, ast.Constant):
                            continue
                        for c in ast.walk(v):
                            if (isinstance(c, ast.Constant) and c.lineno == c.end_lineno
                                    and type(c.value) in (int, bool, str) and k.lineno <= c.lineno <= v.end_lineno):
                                out.append((rel, c.lineno, c.col_offset, c.end_col_offset, c.value))
                walk(child)

        walk(tree)
    return out


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    cands = candidates()
    rng.shuffle(cands)
    done = 0
    for path, line, c0, c1, val in cands:
        if done >= n:
            break
        lines = (campaign.SRC / path).read_text("utf-8").split("\n")
        text = lines[line - 1]
        if not text.isascii():
            continue
        if type(val) is bool:
            new = repr(not val)
        elif type(val) is int:
            new = repr(val + 1)
        else:
            new = repr(val + "x") if val else repr("x")
        new_line = text[:c0] + new + text[c1:]
        new_lines = lines[: line - 1] + [new_line] + lines[line:]
        body = "".join(difflib.unified_diff([x + "\n" for x in lines], [x + "\n" for x in new_lines],
                                            f"a/{path}", f"b/{path}", n=3))
        patch = f"diff --git a/{path} b/{path}\nindex 1234567..89abcde 100644\n" + body
        sel, notes = campaign.selection(patch)
        if not any("dict-key" in x for x in notes):
            continue
        name = f"dk{seed}_{done}"
        (campaign.MUT / f"{name}.patch").write_text(patch, "utf-8", newline="\n")
        fail, summary = campaign.run_tests(name)
        fail_t = {f for f in fail if "::" in f}
        missed = sorted(fail_t - sel)
        file_err = sorted(f for f in fail if "::" not in f)
        rec = {"name": name, "file": path, "line": line, "selected": len(sel), "failing": sorted(fail_t),
               "file_errors": file_err, "missed": missed, "summary": summary, "notes": notes}
        with open(campaign.WORK / "out" / "dict_check.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{name} {path}:{line} sel={len(sel)} fail={len(fail_t)} fileerr={len(file_err)} "
              f"MISSED={len(missed)} {notes} {summary}", flush=True)
        done += 1


if __name__ == "__main__":
    main()
