#!/usr/bin/env bash
set -u
pids=()
cleanup(){ for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
while :; do
  for i in 0 1 2; do
    port=$((8000+i))
    if ! ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "(^|:)${port}$"; then
      NODE_ID=$i PORT=$port python3 /app/app.py >/tmp/huddle-$port.log 2>&1 &
      pid=$!
      echo "$pid" > "/tmp/huddle-$port.pid"
      pids+=("$pid")
    fi
  done
  sleep 0.25
done
