# Read this first: work order and rules for this run

This run is about one component: **roles and invitations**. Before you run
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have run the check below and seen
**all 38** rules pass. Do not submit unless every one of them passes.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to work on now and how to prove it works.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: work order, how to verify, when you may submit |
| `roles/roles.md` | The 38 rules for roles and invitations |
| `roles/checks/check_roles.sh` | Prints PASS or FAIL for each rule in `roles.md` |

## Scope

Work only on the routes in `roles.md`. Do NOT start on messages, files, IRC, or the UI in
this run. If `/app` is empty, first build the smallest cluster that answers `GET /api/health`
on ports `8000`, `8001`, and `8002`, then add the rest in the order of `roles.md`.

## Order of work

1. Read `/spec/roles/roles.md`.
2. Build one section at a time (0, then 1, then 2, ...).
3. Run the check after every section. Fix failing rules, lowest number first.
4. If a rule that passed starts failing, fix it before doing anything else.

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
RESULT: 37 passed, 1 failed
```

- The check starts `/app/start.sh` itself if the cluster is not answering, and finishes in
  under 30 seconds.
- After you change code, restart the cluster before checking again: find the process that
  owns each port with `ss -ltnp` and stop it by PID. Never stop processes by name.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of the check is `RESULT: 38 passed, 0 failed`;
- `GET /api/health` returns `200` on ports `8000`, `8001`, and `8002`;
- `/app/requirements.txt` lists every Python package you use.

These 38 rules are necessary, not sufficient. The full task in the grading contract still
applies.
