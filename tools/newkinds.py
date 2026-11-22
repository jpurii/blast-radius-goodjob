"""Lab-only campaign of edit kinds beyond the sample operators, against the
real suite: delete a one-line statement, duplicate a one-line statement,
change a string literal inside a function body, `return X` -> `return None`,
and two-file edits (two of the above in different sqlglot files).
Usage: python newkinds.py N seed -> out/newkinds.jsonl"""

import ast
import difflib
import json
import random
import sys

import campaign


def stmts(rel):
    src = (campaign.SRC / rel).read_text("utf-8")
    tree = ast.parse(src)
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = fn.body
        for i, st in enumerate(body):
            if st.lineno != st.end_lineno:
                continue
            if i == 0 and isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                continue  # docstring
            if isinstance(st, ast.Return) and st.value is not None and len(body) > 1:
                out.append(("return-none", st))
            if len(body) > 1 and not isinstance(st, (ast.Return, ast.Raise)):
                out.append(("delete", st))
            if isinstance(st, (ast.Expr, ast.Assign, ast.AugAssign)):
                out.append(("duplicate", st))
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str) and n.lineno == n.end_lineno
                    and 0 < len(n.value) < 40 and n is not getattr(fn.body[0], "value", None)):
                out.append(("string", n))
    return src.split("\n"), out


def edit(lines, kind, node):
    ln = node.lineno
    text = lines[ln - 1]
    if not text.isascii():
        return None
    if kind == "delete":
        indent = text[: len(text) - len(text.lstrip())]
        return lines[: ln - 1] + [indent + "pass"] + lines[ln:]
    if kind == "duplicate":
        return lines[:ln] + [text] + lines[ln:]
    if kind == "return-none":
        indent = text[: len(text) - len(text.lstrip())]
        return lines[: ln - 1] + [indent + "return None"] + lines[ln:]
    if kind == "string":
        seg = text[node.col_offset: node.end_col_offset]
        if not seg or seg[0] not in "'\"" or len(seg) < 3:
            return None
        return lines[: ln - 1] + [text[: node.col_offset] + seg[0] + "x" + seg[1:] + text[node.end_col_offset:]] + lines[ln:]
    return None


def diff(path, old, new):
    body = "".join(difflib.unified_diff([x + "\n" for x in old], [x + "\n" for x in new],
                                        f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 1234567..89abcde 100644\n" + body


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    files = [f.relative_to(campaign.SRC).as_posix() for f in sorted((campaign.SRC / "sqlglot").rglob("*.py"))]
    done = 0
    while done < n:
        parts = []
        for _ in range(2 if done % 4 == 3 else 1):
            rel = rng.choice(files)
            if any(p[0] == rel for p in parts):
                continue
            try:
                lines, cands = stmts(rel)
            except SyntaxError:
                continue
            if not cands:
                continue
            kind, node = rng.choice(cands)
            new = edit(lines, kind, node)
            if new is None or new[-1] != "" or lines[-1] != "":
                continue
            parts.append((rel, kind, node.lineno, diff(rel, lines[:-1], new[:-1])))
        if not parts:
            continue
        patch = "".join(p[3] for p in parts)
        sel, notes = campaign.selection(patch)
        name = f"nk{seed}_{done}"
        (campaign.MUT / f"{name}.patch").write_text(patch, "utf-8", newline="\n")
        fail, summary = campaign.run_tests(name)
        if not summary:
            continue  # patch did not apply / no pytest summary: not a valid run
        fail_t = {f for f in fail if "::" in f}
        file_err = sorted(f for f in fail if "::" not in f)
        missed = sorted(fail_t - sel)
        if file_err and len(sel) < 1620:
            missed.append(f"FILE-ERRORS {file_err[:3]}")
        rec = {"name": name, "parts": [(p[0], p[1], p[2]) for p in parts], "selected": len(sel),
               "failing": sorted(fail_t), "file_errors": file_err, "missed": missed, "summary": summary,
               "notes": notes}
        with open(campaign.WORK / "out" / "newkinds.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{name} {[(p[0].split('/')[-1], p[1], p[2]) for p in parts]} sel={len(sel)} fail={len(fail_t)} "
              f"fileerr={len(file_err)} MISSED={len(missed)} {notes} {summary}", flush=True)
        done += 1


if __name__ == "__main__":
    main()
