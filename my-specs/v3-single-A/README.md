# Read this first: work order and rules for this run

This run is about one component: **roles and invitations**. Before you run
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have run the check below and seen
**all 42** rules pass. Do not submit unless every one of them passes.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to work on now and how to prove it works.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: work order, how to verify, when you may submit |
| `roles/roles.md` | The 42 rules for the baseline, auth, workspaces, roles and invitations |
| `roles/checks/check_roles.sh` | Prints PASS or FAIL for each rule in `roles.md` |

## Scope

Work only on the routes in `roles.md`. Do NOT start on messages, files, IRC, or the UI in
this run. `/app` starts empty.

Use **FastAPI and `uvicorn[standard]`**, not Python's built-in `http.server`: later
components need WebSockets.

## Order of work

1. Read `/spec/roles/roles.md`.
2. Build one section at a time (0, then 1, then 2, ...).
3. Run the check after every section. Fix failing rules, lowest number first.
4. If a rule that passed starts failing, fix it before doing anything else.

Fix the real behavior described in the spec. Never hard-code a value or special-case a
request just to make a check line pass: the hidden tests check the same rules with different
requests.

Run `bash /app/timer.sh` to see how much time is left.

## How to verify

Run this as a single command:

```bash
bash /spec/roles/checks/check_roles.sh
```

It prints one line per rule and a final summary:

```text
PASS 1.1 register returns 201 with user and token
FAIL 4.4 PATCH role to owner: expected 400, got 200
RESULT: 41 passed, 1 failed
```

The check finishes in under 30 seconds.

## Starting and restarting the cluster

- Do not start `/app/start.sh` yourself. The check starts it when the cluster is not
  answering.
- After you change code, stop the running nodes (and the `start.sh` that started them) by PID,
  then run the check again. This one command does it:

  ```bash
  for p in $(ss -ltnpH | grep -E ':800[0-2] ' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do pp=$(ps -o ppid= -p $p | tr -d ' '); [ "${pp:-1}" -gt 1 ] && kill $pp; kill $p; done; sleep 1
  ```

- Never stop processes by name (`pkill`, `killall`, `pgrep -f`).
- Rule 0.3 kills the node on port `8001` on purpose, at the end of the check. `start.sh` must
  start it again.

## When every check passes

Do not submit right away. Use the time left to test other behavior from the spec file and the
grading contract for these routes with your own `curl` requests (other status codes, other
nodes, edge cases), and fix what is wrong. Run the check again after every change; if a rule
that passed starts failing, undo the change. Spend at least 10 more minutes this way, and
submit when `bash /app/timer.sh` shows about 5 minutes left.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of the check is `RESULT: 42 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001`, and `8002`;
- `/app/requirements.txt` lists every Python package you use.

These 42 rules are necessary, not sufficient. The full task in the grading contract still
applies.
