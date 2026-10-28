#!/usr/bin/env bash
set -u
cd /app
pids=()
trap 'for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; exit 0' TERM INT
while true; do
 for i in 0 1 2; do
  port=$((8000+i)); if ! ss -ltn "sport = :$port" | grep -q LISTEN; then
   PORT=$port python3 -m uvicorn app:app --host 127.0.0.1 --port "$port" >>/app/node$i.log 2>&1 & pids[$i]=$!
  fi
 done
 sleep 1
done
