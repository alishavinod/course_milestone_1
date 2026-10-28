#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
trap 'for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait' EXIT
while true; do
 for i in 0 1 2; do
  port=$((8000+i))
  if ! curl -sf --max-time 1 "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
   NODE_ID=$i python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" >/tmp/huddle-$i.log 2>&1 &
   pids+=("$!")
  fi
 done
 sleep 1
done
