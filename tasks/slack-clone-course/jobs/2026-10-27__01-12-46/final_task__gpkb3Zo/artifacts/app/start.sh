#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; exit 0; }
trap cleanup TERM INT EXIT
for i in 0 1 2; do
  port=$((8000+i))
  NODE_ID=$i python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" --app-dir /app &
  pids[$i]=$!
done
while true; do
  for i in 0 1 2; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      port=$((8000+i))
      NODE_ID=$i python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" --app-dir /app &
      pids[$i]=$!
    fi
  done
  sleep 0.2
done
