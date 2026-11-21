# Blast Radius — team "good job"

Builders Buildathon 2026, RWX track. Given a patch to sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, pick the tests that can fail and
split them across up to 8 workers.

## How it decides

All maps are built offline from the **pinned commit only** (no patches, no
answers) and committed under `maps/` (gzipped JSON):

| Map | Built from | Used for |
| --- | --- | --- |
| `cov/` | per-test line coverage (`coverage.py` dynamic contexts) | changes inside function bodies |
| `classes` | every sqlglot method call recorded with the class of `self`, including its whole MRO | class-level constants and tables (`TRANSFORMS`, `KEYWORDS`, `arg_types`, …) |
| `names` | tests covering any line that loads a given name | module-level constants and imports |
| `opens` | files each test opened (audit hook) | fixture files |
| `cov/*/spans` | the AST of each file | widening a changed line to its whole statement header |

At run time `selector.py` (standard library only) parses each patch:

1. Comment- or whitespace-only hunks (same code tokens before and after, not
   inside a multi-line string) select nothing.
2. A changed line widens to its enclosing statement header, so a mutated
   condition in a multi-line `if` reaches tests that short-circuit past it.
3. Function body → tests covering it. Function header → tests running its
   body. Class body → tests that used that class or a subclass. Module level
   → tests loading the names it binds. Test-file setup → the whole class.
4. Anything it cannot place selects every test (a miss costs 300 s).
5. Tests are packed longest-first onto 1–8 workers, keeping the cheapest
   busiest-worker + 2 s/worker.

## Lab notes (how the maps were made)

`tools/lab/` runs sqlglot's suite on the organizers' base image with
`requirements.lock`, plus `coverage` and `pytest-cov` (lab only). Its pytest
plugin `brtrace.py`:

- runs the optimizer/executor `ProcessPoolExecutor` work inline, through a
  pickle round-trip, so coverage reaches it and `serde` is exercised;
- imports every sqlglot module before the first test, so lazily loaded
  dialect code is never credited to one test only;
- clears the generator dispatch cache before each test;
- records fixture opens and class usage.

`tools/build_maps.py` turns those traces into `maps/`.
`tools/run_sample.sh` builds this image and scores it on the kit's samples.

## Code we did not write

- Python standard library only at run time.
- Lab only: sqlglot (MIT) at the pinned commit, its test suite and pinned
  dependencies, `coverage` (Apache-2.0), `pytest-cov` (MIT).
- Written during the event with AI assistance (Claude).

## License

MIT
