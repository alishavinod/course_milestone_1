# Read this first: work order and rules for this run

This run is about two components: **roles and invitations** (Spec A) and **channels and
message history** (Spec B). Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you
MUST have run both checks below and seen every rule pass: **51** in Spec A and **66** in
Spec B. Do not submit unless every one of them passes.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to work on now and how to prove it works.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: work order, how to verify, when you may submit |
| `roles/roles.md` | Spec A: 51 rules for the baseline, auth, workspaces, roles and invitations |
| `roles/checks/check_roles.sh` | Prints PASS or FAIL for each rule in `roles.md` |
| `channels/channels.md` | Spec B: 66 rules for channels, messages, paging, mentions, events and read state |
| `channels/checks/check_channels.sh` | Prints PASS or FAIL for each rule in `channels.md` |

## Scope

Work only on the routes in `roles.md` and `channels.md`. Do NOT start on reactions, files,
DMs, slash commands, IRC, or the UI in this run. `/app` starts empty.

Use **FastAPI and `uvicorn[standard]`** from the start, not Python's built-in `http.server`:
Spec B needs WebSockets.

## Order of work

1. Read `/spec/roles/roles.md` and `/spec/channels/channels.md`.
2. Build Spec A first, one section at a time. Run `check_roles.sh` until all 51 pass.
3. Then build Spec B, one section at a time (1, then 2, ...). Run `check_channels.sh` after
   every section and fix failing rules, lowest number first.
4. Run `check_roles.sh` again after Spec B changes. If a rule that passed starts failing, fix
   it before doing anything else.

Fix the real behavior described in the spec. Never hard-code a value or special-case a
request just to make a check line pass: the hidden tests check the same rules with different
requests.

Run `bash /app/timer.sh` to see how much time is left.

## How to verify

Run each of these as a single command:

```bash
bash /spec/roles/checks/check_roles.sh
bash /spec/channels/checks/check_channels.sh
```

Each prints one line per rule and a final summary:

```text
PASS 2.1 post message returns 201 with every message field
FAIL 2.7 bad cursor and bad limit return 400: made-up cursor: 500, limit=0: 400, limit=201: 400 (want 400 each), limit=200: 200 (want 200)
RESULT: 43 passed, 1 failed
```

Each check finishes in under 30 seconds.

## Starting and restarting the cluster

- Do not start `/app/start.sh` yourself. Each check starts it when the cluster is not
  answering.
- After you change code, stop the running nodes (and the `start.sh` that started them) by PID,
  then run a check again. This one command does it:

  ```bash
  for p in $(ss -ltnpH | grep -E ':800[0-2] ' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do pp=$(ps -o ppid= -p $p | tr -d ' '); [ "${pp:-1}" -gt 1 ] && kill $pp; kill $p; done; sleep 1
  ```

- Never stop processes by name (`pkill`, `killall`, `pgrep -f`).
- Rule 0.3 kills the node on port `8001` on purpose, at the start of `check_roles.sh`.
  `start.sh` must start it again.

## When every check passes

Do not submit right away. Use the time left to test other behavior from the spec files and
the grading contract for these routes with your own `curl` requests (other status codes,
other nodes, edge cases), and fix what is wrong. Run both checks again after every change; if
a rule that passed starts failing, undo the change. Spend at least 10 more minutes this way,
and submit when `bash /app/timer.sh` shows about 5 minutes left.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_roles.sh` is `RESULT: 51 passed, 0 failed`;
- the last line of `check_channels.sh` is `RESULT: 66 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001`, and `8002`;
- `/app/requirements.txt` lists every Python package you use.

These rules are necessary, not sufficient. The full task in the grading contract still
applies.
