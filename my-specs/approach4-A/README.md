# Read this first: work order and rules for this run

This run is about one component: **roles and invitations**. It is written as four specs that
build on each other. Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST
have run the full check below and seen **all 38** rules pass. Do not submit unless every one
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

- Each check starts `/app/start.sh` itself if the cluster is not answering, and finishes in
  under 30 seconds.
- After you change code, restart the cluster before checking again: find the process that
  owns each port with `ss -ltnp` and stop it by PID. Never stop processes by name.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_roles_all.sh` is `RESULT: 38 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001`, and `8002`;
- `/app/requirements.txt` lists every Python package you use.

These 38 rules are necessary, not sufficient. The full task in the grading contract still
applies.
