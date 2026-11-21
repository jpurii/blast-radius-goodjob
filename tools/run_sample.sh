#!/usr/bin/env bash
# Build ../blast-radius-goodjob and run it on a data dir (default: the kit's samples)
# under the scorer's limits, then score. Usage: bash run_sample.sh [data_dir] [answers]
set -euo pipefail
cd "$(dirname "$0")"
DATA="${1:-../kit/sample}"
ANSWERS="${2:-$DATA/answers.json}"
export MSYS_NO_PATHCONV=1
docker build -q --platform linux/amd64 -t br-entry ../blast-radius-goodjob >/dev/null
ABS="$(cd "$DATA" && (pwd -W 2>/dev/null || pwd))"
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$ABS/commits:/data/commits:ro" \
  --volume "$ABS/tests.json:/data/tests.json:ro" \
  br-entry > out/entry.jsonl 2> out/entry.log
grep -E "^cpu" out/entry.log || true
python -I ../kit/score.py "$DATA" "$ANSWERS" out/entry.jsonl
