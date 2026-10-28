#!/bin/sh
mkdir -p /app
for i in 0 1 2; do
  port=$((8000+i))
  NODE_ID=$i nohup uvicorn app:app --app-dir /app --host 127.0.0.1 --port $port >/tmp/huddle-$i.log 2>&1 &
done
wait
