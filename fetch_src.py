"""Build step: fetch sqlglot's Python sources at the pinned commit.

The selector applies each patch to these sources to check that the changed
files still compile and to compare their code tokens exactly. Only *.py files
under sqlglot/ and tests/, and the SQL fixtures, are kept. Usage: python fetch_src.py DEST_DIR
"""

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"


def main():
    dest = Path(sys.argv[1])
    data = urllib.request.urlopen(URL, timeout=120).read()
    kept = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            rel = parts[1]
            keep = rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/"))
            # SQL fixtures: the selector re-runs class-setup loaders on them
            keep = keep or (rel.startswith("tests/fixtures/") and rel.endswith(".sql"))
            if not keep:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tar.extractfile(member).read())
            kept += 1
    if kept < 100:
        sys.exit(f"only {kept} source files fetched")
    print(f"fetched {kept} source files", file=sys.stderr)


if __name__ == "__main__":
    main()
