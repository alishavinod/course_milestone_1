#!/usr/bin/env bash
set -u
mkdir -p /app/data
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true; wait' EXIT
while true; do
 for i in 0 1 2; do
  port=$((8000+i))
  if ! (ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN); then
   NODE_ID=$i PORT=$port python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" >>/tmp/node$i.log 2>&1 &
   pids+=($!)
  fi
 done
 sleep 1
done
