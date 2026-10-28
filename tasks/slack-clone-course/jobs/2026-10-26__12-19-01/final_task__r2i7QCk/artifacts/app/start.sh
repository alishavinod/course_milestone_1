#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
cleanup(){ for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
for i in 0 1 2; do
  (while true; do NODE_ID=$i python -m uvicorn app:app --host 127.0.0.1 --port $((8000+i)) || true; sleep 0.3; done) &
  pids+=("$!")
done
wait
