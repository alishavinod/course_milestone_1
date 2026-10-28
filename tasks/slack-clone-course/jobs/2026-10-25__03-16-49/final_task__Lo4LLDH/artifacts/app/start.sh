#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
trap 'for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait' EXIT
while true; do
  for i in 0 1 2; do
    port=$((8000+i))
    if ! ss -ltnH "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      NODE_ID=$i python -m uvicorn app:app --host 127.0.0.1 --port "$port" >/app/node$i.log 2>&1 &
      pids[$i]=$!
    fi
  done
  sleep 1
done
