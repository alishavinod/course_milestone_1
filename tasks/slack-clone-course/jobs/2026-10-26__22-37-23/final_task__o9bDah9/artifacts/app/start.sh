#!/usr/bin/env bash
set -e
mkdir -p /app/data
while true; do
  for port in 8000 8001 8002; do
    if ! ss -ltnH "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      NODE_ID=$((port-8000)) PORT=$port python3 /app/app.py >>"/app/data/node${port}.log" 2>&1 &
      echo $! >"/app/data/node${port}.pid"
    fi
  done
  sleep 1
done
