# Read this first: work order and rules for this run

This run is about one component: **roles and invitations**. It is written as four specs that
build on each other. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST
have run the full check below and seen **all 42** rules pass. Do not submit unless every one
of them passes.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to work on now and how to prove it works.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `README.md` | This file: work order, how to verify, when you may submit | |
| `roles/roles-1.md` | Roles.1: baseline and auth (rules 0.1 to 1.6) | nothing |
| `roles/roles-2.md` | Roles.2: workspaces and joining channels (rules 2.1 to 3.3) | Roles.1 |
| `roles/roles-3.md` | Roles.3: roles and channel permissions (rules 4.1 to 5.3) | Roles.1, Roles.2 |
| `roles/roles-4.md` | Roles.4: join mode, invitations, ownership transfer (rules 6.1 to 9.1) | Roles.1 to Roles.3 |
| `roles/checks/check_roles_1.sh` to `_4.sh` | Print PASS or FAIL for each rule of one spec | |
| `roles/checks/check_roles_all.sh` | Runs the four checks in order and prints one total | |
| `roles/checks/lib.sh` | Shared helpers for the checks. You do not need to read it | |

Each spec has the same three parts: **Uses** (functions from earlier specs that you must
call, not rewrite), **Adds** (functions this spec introduces, with the names to use), and
**Rules** (what must be true over HTTP).

## Scope

Work only on the routes in the four spec files. Do NOT start on messages, files, IRC, or the
UI in this run. `/app` starts empty.

Use **FastAPI and `uvicorn[standard]`**, not Python's built-in `http.server`: later
components need WebSockets.

## Order of work

Work one spec at a time. Read the next spec file only after the check for the current one
passes.

1. Read `/spec/roles/roles-1.md`. Build it. Run `check_roles_1.sh` until all rules pass.
2. Read `/spec/roles/roles-2.md`. Build it using the functions from Roles.1. Run
   `check_roles_2.sh` until all rules pass.
3. Do the same for `roles-3.md` and `roles-4.md`, in order.
4. After each step, run `check_roles_all.sh`. If a rule from an earlier spec starts failing,
   fix it before doing anything else.

Do not start a spec while a rule from an earlier spec is failing.

Fix the real behavior described in the spec. Never hard-code a value or special-case a
request just to make a check line pass: the hidden tests check the same rules with different
requests.

Run `bash /app/timer.sh` to see how much time is left.

## How to verify

Run one of these as a single command:

```bash
bash /spec/roles/checks/check_roles_1.sh
bash /spec/roles/checks/check_roles_2.sh
bash /spec/roles/checks/check_roles_3.sh
bash /spec/roles/checks/check_roles_4.sh
bash /spec/roles/checks/check_roles_all.sh
```

Each prints one line per rule and a final summary:

```text
PASS 1.1 register returns 201 with user and token
FAIL 4.4 PATCH role to owner: expected 400, got 200
RESULT: 7 passed, 1 failed
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
- Rule 0.3 kills the node on port `8001` on purpose, at the end of `check_roles_1.sh` and
  `check_roles_all.sh`. `start.sh` must start it again.

## When every check passes

Do not submit right away. Use the time left to test other behavior from the spec files and
the grading contract for these routes with your own `curl` requests (other status codes,
other nodes, edge cases), and fix what is wrong. Run `check_roles_all.sh` again after every
change; if a rule that passed starts failing, undo the change. Spend at least 10 more minutes
this way, and submit when `bash /app/timer.sh` shows about 5 minutes left.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_roles_all.sh` is `RESULT: 42 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001`, and `8002`;
- `/app/requirements.txt` lists every Python package you use.

These 42 rules are necessary, not sufficient. The full task in the grading contract still
applies.
