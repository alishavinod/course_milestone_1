#!/usr/bin/env bash
set -u
cd /app
while true; do
  for n in 0 1 2; do
    port=$((8000+n))
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      NODE_ID=$n uvicorn app:app --host 127.0.0.1 --port "$port" >/tmp/huddle-$n.log 2>&1 &
    fi
  done
  sleep 1
done
