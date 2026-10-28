#!/usr/bin/env bash
set -u
PIDS=()
trap 'for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; exit 0' TERM INT
while true; do
 for i in 0 1 2; do
  port=$((8000+i))
  if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
   uvicorn app:app --host 127.0.0.1 --port "$port" >>/tmp/uvicorn-$port.log 2>&1 &
   PIDS[$i]=$!
  fi
 done
 sleep 1
done
