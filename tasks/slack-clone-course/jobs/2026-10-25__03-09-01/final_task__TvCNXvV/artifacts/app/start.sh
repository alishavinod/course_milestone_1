#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
cleanup() {
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM
while true; do
  for i in 0 1 2; do
    port=$((8000+i))
    alive=0
    if [ -n "${pids[$i]:-}" ] && kill -0 "${pids[$i]}" 2>/dev/null; then alive=1; fi
    if [ "$alive" -eq 0 ]; then
      NODE_ID=$i PORT=$port python /app/app.py >>"/app/node-$i.log" 2>&1 &
      pids[$i]=$!
    fi
  done
  sleep 1
done
