#!/bin/sh
mkdir -p /app/data
for i in 0 1 2; do
  port=$((8000+i))
  (NODE_ID=$i exec python3 -m uvicorn app:app --host 127.0.0.1 --port "$port") &
done
wait
