"""Lab-only check of weak-mutation pruning: operator swaps at instrumented
sites where pruning drops tests, each run against the real suite.
Usage: python wm_check.py N seed  -> out/wm_check.jsonl"""

import ast
import difflib
import gzip
import json
import random
import sys

import campaign

SWAP = {"and": "or", "or": "and", "<": "<=", "<=": "<", ">": ">=", ">=": ">",
        "is": "==", "==": "is", "is not": "!=", "!=": "is not"}
CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not",
       ast.Eq: "==", ast.NotEq: "!="}


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    wm = json.loads(gzip.decompress((campaign.SUB / "maps" / "wm.json.gz").read_bytes()))
    only_chains = len(sys.argv) > 3 and sys.argv[3] == "chain"
    only_eq = len(sys.argv) > 3 and sys.argv[3] == "eq"
    cands = [(p, k) for p, d in wm.items() for k, v in d.items() if v != "*" and (("#" in k) or not only_chains)]
    rng.shuffle(cands)
    done = 0
    for path, key in cands:
        if done >= n:
            break
        gap = None
        if "#" in key:
            key, g = key.split("#")
            gap = int(g)
        line, col = map(int, key.split(":"))
        src = (campaign.SRC / path).read_text("utf-8")
        lines = src.split("\n")
        tree = ast.parse(src)
        at_pos = [x for x in ast.walk(tree) if isinstance(x, (ast.BoolOp, ast.Compare))
                  and x.lineno == line and x.col_offset == col]
        if gap is not None:
            at_pos = [x for x in at_pos if isinstance(x, ast.BoolOp) and len(x.values) >= 3]
        else:
            at_pos = [x for x in at_pos if (isinstance(x, ast.BoolOp) and len(x.values) == 2)
                      or (isinstance(x, ast.Compare) and len(x.ops) == 1)]
            if only_eq:
                at_pos = [x for x in at_pos if isinstance(x, ast.Compare)]
        node = at_pos[0] if at_pos else None
        if node is None:
            continue
        if isinstance(node, ast.BoolOp) and gap is not None:
            a, b = node.values[gap], node.values[gap + 1]
            op = "and" if isinstance(node.op, ast.And) else "or"
        elif isinstance(node, ast.BoolOp):
            a, b = node.values
            op = "and" if isinstance(node.op, ast.And) else "or"
        else:
            a, b = node.left, node.comparators[0]
            op = CMP[type(node.ops[0])]
        if only_eq and op not in ("is", "is not", "==", "!="):
            continue
        # the operator must sit on one line, between the operands
        if a.end_lineno != b.lineno:
            continue
        ln = a.end_lineno
        text = lines[ln - 1]
        seg = text[a.end_col_offset:b.col_offset]
        if op in ("is", "is not", "==", "!=") and seg.strip() != op:
            continue
        if seg.count(op) != 1 or not text.isascii():
            continue
        at = a.end_col_offset + seg.index(op)
        new_line = text[:at] + SWAP[op] + text[at + len(op):]
        new_lines = lines[: ln - 1] + [new_line] + lines[ln:]
        body = "".join(difflib.unified_diff([x + "\n" for x in lines], [x + "\n" for x in new_lines],
                                            f"a/{path}", f"b/{path}", n=3))
        patch = f"diff --git a/{path} b/{path}\nindex 1234567..89abcde 100644\n" + body
        sel, notes = campaign.selection(patch)
        if not any("weak" in x for x in notes):
            continue
        name = f"wm{seed}_{done}"
        (campaign.MUT / f"{name}.patch").write_text(patch, "utf-8", newline="\n")
        fail, summary = campaign.run_tests(name)
        fail = {f for f in fail if "::" in f}
        missed = sorted(fail - sel)
        rec = {"name": name, "file": path, "line": ln, "op": op, "selected": len(sel), "failing": sorted(fail),
               "missed": missed, "summary": summary}
        with open(campaign.WORK / "out" / "wm_check.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{name} {path}:{ln} {op}->{SWAP[op]} sel={len(sel)} fail={len(fail)} MISSED={len(missed)} {summary}",
              flush=True)
        done += 1


if __name__ == "__main__":
    main()
