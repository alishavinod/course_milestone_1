#!/usr/bin/env bash
set -eu
mkdir -p /app/data
python3 -c 'import sys;sys.path.insert(0,"/app");import app'
pids=""
cleanup(){ for p in $pids; do kill "$p" 2>/dev/null || true; done; }
trap cleanup TERM INT EXIT
for n in 0 1 2; do
  (NODE_ID=$n exec uvicorn app:app --app-dir /app --host 127.0.0.1 --port $((8000+n))) &
  pids="$pids $!"
done
wait -n $pids
exit 1
