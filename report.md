# Milestone 1 report

- Name: Alisha Vinod
- UNI: av3311

All results are from my own checks. 

## Spec versions

All copies are kept in `my-specs/`. Both layouts always have the same rules (same rule numbers,
word-for-word the same rule text); the final specs (v10) are in `milestone-1-specs/single-file/`
and `milestone-1-specs/staged/`.

| Version | Spec A | Spec B | What changed and why |
| --- | --- | --- | --- |
| v1 | 38 rules | 42 rules | First single-file specs and checks. |
| v2 | 38 | 42 | Rule 6.3 made stricter (the agent hard-coded read counts to pass it); README: never hard-code a value to pass a check; checks no longer hang when they start the cluster. Staged layout created from v2. |
| v3 | 42 | 44 | A: 0.3 (kill node 8001, others keep serving and it comes back), 7.8 `max_uses: 2`, 7.9 `invited_username`, 7.10 admins invite. B: 2.8 default limit 50, 5.7 archive events, 6.2 replies are not unread. README: how to restart the cluster by PID, use leftover time to test; staged README: read one spec file at a time. |
| v4 | 42 | 50 | B: 1.11 name length / `description`, 1.12 owner-only unarchive, 3.10 paging on replies/search/pins, 5.8 30 concurrent posts across 3 nodes give dense event ids, 7.1 `401` and 7.2 `404` on Spec B routes. |
| v5 | 42 | 51 | B: 6.6 read counts stay right when joins and topic changes sit between messages (the agent offset its counts to fit the old test). |
| v6 | 42 | 51 | B: 5.2 also checks that `subscribe` does not replay old events; the read-state test only uses message events, so a WebSocket bug no longer shows up as a read-state failure. |
| v7 | 44 | 54 | After the v6 finals passed every rule, I probed both final apps on 23 `COMPONENTS.md` behaviors my checks did not test. Five gaps became rules. A: 7.11 used-up invitations are not listed, 7.12 targeted invitations are single-use even with `max_uses`. B: 1.13 the workspace owner can set the topic of a channel they are not in, 4.6 a workspace member who is not in the channel is still mentioned, 5.9 `message.created` frames carry the message under `"message"`. |
| v8 | 44 | 56 | From the v7 runs. B: 2.9 REST message objects do not carry `event_id`, 3.11 a reply, topic change or pin in an archived channel returns `423` (the v7 apps returned `200` for topic and pin). A: rule 0.3 (kill a node) now runs first in `check_roles.sh` and `check_roles_1.sh`/`_all.sh`, so a slow environment can no longer skip it (Approach 3 Run 2). |
| v9 | 44 | 56 | B: 6.2 also checks that a user's own messages never count as unread for that user (the v8 app counted them). |
| v10 (final) | 51 | 66 | Each run so far had found one more gap, so instead of adding one rule per run I went through `COMPONENTS.md` line by line, wrote a second probe (17 items) for every line no rule tested, and turned each real gap into a rule. A: 0.2 bad WebSocket token on all three nodes, 1.7 `display_name` and full UserObj, unknown user login `401`, 2.6 workspace fields, unknown slug `404`, bad slug `400`, 4.6 unknown role `400` and unknown/non-member user `404`, 4.7 admin promotes and owner demotes, 6.4 owner and admin rename the workspace, 7.13 member revoke `403`, unknown code `404`, accept with no token `401`, 8.4 `owner_id` changes after a transfer and the old owner loses owner rights. B: 1.14 topic kept on create, 1.15 members list after leave, 2.10 non-member of a private channel gets `403` on its messages, 2.11 non-workspace user gets `403` on posting, 2.12 full message object and `limit=abc` `400`, 3.12 unknown `parent_id` `404`, 3.13 search cursor works on another node, 4.7 punctuation ends a mention, 5.10 private channel frames do not reach non-members, 5.11 `message.reply` frames carry the message and `parent_id`. Fixed stale counts in the staged step files. The reference server passes 117/117, and every earlier app fails only on real gaps (no false failures). |
| | | | Left out on purpose: `?offset=`/`?page=` (COMPONENTS.md only says they are not supported; apps ignore them), `resume.gap` (only needed when old events are dropped, which no app does), the optional `include_archived=true` alias, and message edit/delete (no route in Component B). |

`/app` starts empty in every run, so in the B step of Approaches 2 and 4 the agent rebuilt the
frozen Spec A and then built Spec B in the same run. That makes the B step the same setup as
Approaches 1 and 3, so each such run is listed once (under Approach 1 or 3) and referenced from
Approach 2 or 4.

## Approach 1

Single-file layout, Spec A and Spec B together.

### Run 1

- Component: both
- Spec and checks version: A v1 + B v2 (`my-specs/approach2-B-v2/`)
- Job folder: `jobs/2026-10-25__00-38-58/`
- My check results: `check_roles.sh` 38 passed, 0 failed; `check_channels.sh` 42 passed, 0 failed
- Problems:
  - "Database is locked" at startup when all nodes set up SQLite at once; the agent fixed it.
  - Spec A first 35/38 (ownership transfer missing); Spec B went 34, 37, then 42/42 (topic permissions, private channels in workspace detail, a mention regex matching a literal `\b`, read state).
  - Thread replies counted as unread, and `start.sh` did not restart a dead node. Neither was checked yet; this led to rules 6.2 (replies) and 0.3 in v3.
  - 25 steps, 3m 56s, $0.20.

### Run 2

- Component: both
- Spec and checks version: A v3 + B v4 (`my-specs/single-file-v4/`)
- Job folder: `jobs/2026-10-25__02-30-33/`
- My check results: `check_roles.sh` 42 passed, 0 failed; `check_channels.sh` 50 passed, 0 failed
- Problems:
  - The first Spec B check had 7 failures (cursor stability, archive booleans returned as `1`/`0`, read state); the agent fixed them.
  - Read state was not really fixed: the agent shifted its count by the number of non-message events before the first message, which fit my test channel only. I probed other event orders and the counts were wrong. The real cause was that `subscribe` replayed old events. This led to rule 6.6 (v5) and the sharper 5.2 (v6).
  - The agent started `start.sh` itself despite the README and lost one command to the 30-second limit.
  - 26 steps, 4m 37s, $0.21.

### Run 3

- Component: both
- Spec and checks version: A v3 + B v5 (`my-specs/single-file-v5/`)
- Job folder: `jobs/2026-10-25__02-43-34/`
- My check results: `check_roles.sh` 42 passed, 0 failed; `check_channels.sh` 51 passed, 0 failed
- Problems:
  - The first Spec B check had 26 failures; the agent got to 50/51 and then spent about 20 steps on rule 6.3. The real bug was `subscribe` replaying old events, which my read-state test picked up, so the failure pointed at the wrong place. The agent found and fixed it correctly (read state was right in my probe of other event orders).
  - To make such failures clear, v6 added the subscribe check to 5.2 and made the read-state test use only message events.
  - 52 steps, 7m 19s, $0.67.

### Run 4

- Component: both
- Spec and checks version: A v3 + B v6 (`my-specs/single-file-v6/`)
- Job folder: `jobs/2026-10-25__02-56-40/`
- My check results: `check_roles.sh` 42 passed, 0 failed; `check_channels.sh` 51 passed, 0 failed
- Problems:
  - Spec B went 34, 39, 48, then 51/51 with no long stuck loop. Fixes: cursor paging, topic permissions, a 500 on workspace detail, message events not stored reliably, the missing leave event.
  - No hard-coding: read state was correct in my probe of three event orders, every route returns `401` without a token, and nothing in the code refers to my test names.
  - My extra probe of 23 `COMPONENTS.md` behaviors found 5 gaps: used-up invitations still listed, a targeted invitation accepted twice with `max_uses: 5`, the workspace owner could not set the topic of a channel they were not in (`403`), a workspace member outside the channel was not mentioned, and `message.created` frames had the message fields at the top level. These became v7.
  - The agent submitted with 43 minutes left.
  - 42 steps, 7m 19s, $0.40.

### Run 5

- Component: both
- Spec and checks version: A v7 + B v7 (`my-specs/single-file-v7/`)
- Job folder: `jobs/2026-10-25__23-24-06/`
- My check results: `check_roles.sh` 44 passed, 0 failed; `check_channels.sh` 54 passed, 0 failed
- Problems:
  - Spec A passed 44/44 once the cluster was up. Spec B went 42, 48, 51, then 54/54: durable events were missing for topic changes, archive and leave, then mention counts in read state were off.
  - The agent briefly broke its own server with a quoting error in an SQL edit, saw the cluster fail to start, and fixed it.
  - On a cold start the final app passes all 23 extra probes, read state is right for three event orders, and every route returns `401` without a token. No hard-coding.
  - Ambiguous lines it does not follow strictly: REST message objects also include `event_id`, a topic change or pin in an archived channel returns `200` (posting returns `423` as required), and `?offset=`/`?page=` are ignored. The first two became rules 2.9 and 3.11 in v8.
  - The agent submitted with 44 minutes left.
  - 34 steps, 6m 27s, $0.32.

### Run 6

- Component: both
- Spec and checks version: A v8 + B v8 (`my-specs/single-file-v8/`)
- Job folder: `jobs/2026-10-26__22-37-23/`
- My check results: `check_roles.sh` 44 passed, 0 failed; `check_channels.sh` 56 passed, 0 failed
- Problems:
  - Spec A passed 44/44 almost at once. Spec B went 33, 40, 47, 53, then 56/56: a `500` on message listing broke all the paging rules at first, then search by `workspace_id`, pins of private channels and missing durable events.
  - The new rules worked: no `event_id` in REST messages, `423` for reply, topic and pin in an archived channel.
  - My probe found that the author's own messages counted as unread for the author. Rule 6.2 only posted messages as another user, so v9 added this case. The second probe I wrote later for v10 also shows this app missing `404` for a role change on an unknown user, `404` for revoking an unknown code, `403` for reading a private channel's messages as a non-member, and private channel frames reaching non-members.
  - 31 steps, 8m 10s, $0.29.

### Run 7

- Component: both
- Spec and checks version: A v9 + B v9 (`my-specs/single-file-v9/`)
- Job folder: `jobs/2026-10-27__00-02-24/`
- My check results: `check_roles.sh` 44 passed, 0 failed; `check_channels.sh` 56 passed, 0 failed
- Problems:
  - The first full check printed 32/44 for Spec A (invitation routes returned `404` because the agent had not built them yet), which looked like v9 was worse. It was only the first check; Spec A is the same in v8 and v9, and the run ended at 44/44 and 56/56 in fewer steps than Run 6.
  - The new 6.2 case (own messages not unread) passed.
  - My probe found that after an ownership transfer `owner_id` in the workspace still named the old owner. That was the third run in a row where my probe found a gap the rules did not test, so for v10 I checked every `COMPONENTS.md` line instead of only the last gap.
  - 22 steps, 4m 5s, $0.16.

### Run 8

- Component: both
- Spec and checks version: A v10 + B v10, final (`my-specs/single-file-v10/`, same as `milestone-1-specs/single-file/`)
- Job folder: `jobs/2026-10-27__00-35-06/`
- My check results: `check_roles.sh` 51 passed, 0 failed; `check_channels.sh` 66 passed, 0 failed
- Problems:
  - Spec A went 43, 50, then 51/51 (the new 4.6 and 7.13 error codes). Spec B went 35, 53, 58, 63, 65, then 66/66: archive and leave routes, duplicate channel names, search, pins, the new 3.12 (unknown parent) and 3.13 (search cursor on another node).
  - Four commands hit the 30-second limit (writing the whole app in one command, and killing the server and running a check while the cluster was still coming back); it recovered each time.
  - On a cold start the final app passes 51/51 and 66/66 in both layouts, all 40 items of both probes, read state is right for three event orders, every write route returns `401` without a token, and there is no hard-coding.
  - It also added message edit and delete routes, which Component B does not ask for.
  - 43 steps, 8m 6s, $0.51.

### Final `/app`

- Job folder: `jobs/2026-10-27__00-35-06/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles.sh` | 51 | 0 | Spec A v10 (also 51/51 with the staged `check_roles_all.sh`) |
  | `check_channels.sh` | 66 | 0 | Spec B v10 (also 66/66 with the staged `check_channels_all.sh`) |

## Approach 2

Single-file layout, one component at a time.

### Run 1

- Component: A
- Spec and checks version: A v1 (`my-specs/approach2-A-v1/`)
- Job folder: `jobs/2026-10-25__00-04-49/`
- My check results: 38 passed, 0 failed
- Problems:
  - Node 8000 crashed at startup with `database is locked`; the agent fixed it.
  - The agent used Python's built-in `http.server` (no packages), which cannot do the WebSockets Spec B needs. Later READMEs tell it to use FastAPI.
  - `start.sh` did not restart a dead node.
  - 9 steps, about 2 minutes, $0.04.

### Run 2

- Component: B (Spec A frozen at v1; the agent rebuilt A in the same run)
- Spec and checks version: A v1 + B v1 (`my-specs/approach2-B-v1/`)
- Job folder: `jobs/2026-10-25__00-21-21/`
- My check results: `check_roles.sh` 38 passed, 0 failed; `check_channels.sh` 42 passed, 0 failed
- Problems:
  - First Spec B check failures: missing channel fields (1.1), archive returning 404 (1.6), posting to an archived channel returning 201 instead of 423 (1.7). Fixed.
  - The agent hard-coded `unread_count = 0` and `mention_count = 0` in the mark-read response to pass rule 6.3. I made 6.3 stricter in v2 and added a README line against hard-coding.
  - Both checks could hang when they started the cluster themselves; fixed in v2.
  - 20 steps, 3m 46s, $0.14.

### Run 3

- Component: A
- Spec and checks version: A v3, 42 rules (`my-specs/v3-single-A/`)
- Job folder: `jobs/2026-10-25__02-12-37/`
- My check results: 42 passed, 0 failed
- Problems:
  - First check 41/42 (PATCH role to `owner` was allowed); fixed.
  - The agent followed the new README section and used spare time to test. Re-running the check, rule 0.3 caught that `start.sh` did not bring a killed node back; it rewrote `start.sh` as a supervisor.
  - It still submitted with about 46 minutes left.
  - 17 steps, 2m 31s, $0.10.
  - This is the last A-only run. Spec A later gained rules in v7, v8 and v10; I did not repeat the A-only run for them because the B step below rebuilds Spec A from scratch and passes all 51 A rules.

The B step on the final spec is the same run as Approach 1 Run 8 (`jobs/2026-10-27__00-35-06/`):
Spec A frozen at v10, then Spec B v10, 51/51 and 66/66. The earlier B steps on v4 to v9 are
Approach 1 Runs 2 to 7.

### Final `/app`

- Job folder: `jobs/2026-10-27__00-35-06/` (the B step, with Spec A frozen at v10)
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles.sh` | 51 | 0 | Spec A v10 (the A-only run `02-12-37` passed all 42 rules of A v3) |
  | `check_channels.sh` | 66 | 0 | Spec B v10 |

## Approach 3

Staged layout, Spec A (Roles.1 to Roles.4) and Spec B (Channels.1 to Channels.4) together.

### Run 1

- Component: both
- Spec and checks version: A v3 + B v6 (`my-specs/staged-v6/`)
- Job folder: `jobs/2026-10-25__03-16-49/`
- My check results: `check_roles_all.sh` 42 passed, 0 failed; `check_channels_all.sh` 51 passed, 0 failed
- Problems:
  - None left at the end. The agent read the eight spec files one at a time and ran each step's check until it passed: Roles 9, 8, 8, 17 and Channels 12, 18, 13, 8. Failures were small and fixed inside each step (missing fields, archive booleans, event gaps).
  - No hard-coding (same probes as Approach 1 Run 4). The extra probe found 2 of the 5 gaps that led to v7 (targeted invitations accepted twice with `max_uses`, workspace members outside the channel not mentioned).
  - The agent submitted with 45 minutes left.
  - 44 steps, 5m 7s, $0.38.

### Run 2

- Component: both
- Spec and checks version: A v7 + B v7 (`my-specs/staged-v7/`)
- Job folder: `jobs/2026-10-26__12-19-01/`
- My check results: `check_roles_all.sh` 44 passed, 0 failed; `check_channels_all.sh` 54 passed, 0 failed (on the agent's last runs the full channels check hit the 30-second limit; each of the four step checks passed, and on a cold start the full check passes 54/54 in 3 seconds)
- Problems:
  - The agent followed the steps (Roles.1 to Roles.4, then Channels.1 to Channels.4) and each step check passed.
  - Its environment became slow: orphaned server processes from a restart loop and SQLite connections that were not closed. Every check slowed down, and the agent spent many steps cleaning this up.
  - My check made it worse: in `check_roles_all.sh` rule 0.3 runs last and is skipped with "not run, the check ran out of time" after 15 seconds. In the slow environment the agent saw 43/44 again and again for a rule that was passing (it passed in `check_roles_1.sh`, which runs 0.3 early). v8 moved 0.3 to the start of the check so this cannot happen again.
  - `check_channels_all.sh` then hit the 30-second limit, so the agent submitted after each step check passed on its own.
  - On a cold start the final app is fast (3 to 5 seconds per full check), passes all 23 extra probes, read state is right for three event orders, every route returns `401` without a token, and there is no hard-coding.
  - It missed two lines my probe found (later rules 2.9 and 3.11 in v8): REST messages carried `event_id`, and a topic change in an archived channel returned `200`.
  - 89 steps, 22m 20s, $1.18.

Approaches 1 and 3 ran on v8 and v9 only in single-file form (Approach 1 Runs 6 and 7); the rule
text is the same in both layouts, and those versions were replaced by v10 before a staged run.

### Run 3

- Component: both
- Spec and checks version: A v10 + B v10, final (`my-specs/staged-v10/`, same as `milestone-1-specs/staged/`)
- Job folder: `jobs/2026-10-27__01-12-46/`
- My check results: `check_roles_all.sh` 51 passed, 0 failed; `check_channels_all.sh` 66 passed, 0 failed
- Problems:
  - The agent followed the steps in order and ran each step check until it passed: Roles 10, 9, 10, 22, then 51/51; Channels 15, 25, 18, 8, then 66/66. Failures stayed inside a step (join on an open workspace, mentions returning `[]`, a WebSocket that did not connect on 8001, the archived topic change).
  - Four commands hit the 30-second limit while it killed and restarted the server processes; it recovered each time. Unlike Run 2, the environment did not slow down and both full checks finished in about 5 seconds.
  - On a cold start the final app passes 51/51 and 66/66 in both layouts, all 40 probe items, read state is right for three event orders, every write route returns `401` without a token, and there is no hard-coding. The agent did not change any spec file.
  - 70 steps (89 in Run 2), 9m 34s, $0.87.

### Final `/app`

- Job folder: `jobs/2026-10-27__01-12-46/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles_all.sh` | 51 | 0 | Roles.1 10/10, Roles.2 9/9, Roles.3 10/10, Roles.4 22/22 (also 51/51 with the single-file check) |
  | `check_channels_all.sh` | 66 | 0 | Channels.1 15/15, Channels.2 25/25, Channels.3 18/18, Channels.4 8/8 (also 66/66 with the single-file check) |

## Approach 4

Staged layout, one component at a time.

### Run 1

- Component: A
- Spec and checks version: staged A v2, 38 rules (`my-specs/approach4-A/`)
- Job folder: `jobs/2026-10-25__01-50-12/`
- My check results: `check_roles_all.sh` 38 passed, 0 failed
- Problems:
  - The agent ignored the step order: it read all four Roles files, built everything at once and only ran `check_roles_all.sh`. It did use every function name from the Adds tables. This led to the v3 README line "read the next spec file only after the current check passes".
  - Two commands timed out because the agent started `start.sh` itself in a way that kept the output open; then its `start.sh` failed under `sh` (`wait -n`). Led to the v3 restart instructions and the bash tip.
  - Its `start.sh` took the whole cluster down when one node died. Not checked yet; led to rule 0.3.
  - 18 steps, 3m 8s, $0.09.

### Run 2

- Component: A
- Spec and checks version: staged A v3, 42 rules (`my-specs/v3-staged-A/`)
- Job folder: `jobs/2026-10-25__03-09-01/`
- My check results: `check_roles_all.sh` 42 passed, 0 failed
- Problems:
  - This time the agent followed the steps: Roles.1 9/9, Roles.2 8/8, Roles.3 8/8, Roles.4 17/17, then 42/42.
  - With spare time it found that three role routes answered `422` instead of `401` without a token and fixed them. I probed the final apps on ten write routes: all return `401`, so no spec change was needed.
  - 41 steps, 4m 47s, $0.27.
  - This is the last A-only run; as in Approach 2, Spec A later gained rules in v7, v8 and v10, which the B step below covers.

The B step on the final spec is the same run as Approach 3 Run 3 (`jobs/2026-10-27__01-12-46/`):
Spec A frozen at v10, then Spec B v10, 51/51 and 66/66. The B steps on v6 and v7 are Approach 3
Runs 1 and 2.

### Final `/app`

- Job folder: `jobs/2026-10-27__01-12-46/` (the B step, with Spec A frozen at v10)
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles_all.sh` | 51 | 0 | Spec A v10 (the A-only run `03-09-01` passed all 42 rules of A v3) |
  | `check_channels_all.sh` | 66 | 0 | Spec B v10 |

### Setup run (not counted)

- Job folder: `jobs/2026-10-24__19-15-57/`
- Before writing any spec, I ran the agent once with an empty spec folder. It failed at the first model call with `AuthenticationError ... Missing Authentication header` because my API key was not set yet. It cost $0 and built nothing.

## Final gap analysis

After the last runs I tested both final apps (Approach 1 Run 8 `00-35-06` and Approach 3 Run 3
`01-12-46`) on a fresh database, with the reference server I wrote as a control:

| Test | Approach 1 final | Approach 3 final |
| --- | --- | --- |
| Single-file checks, every version v6 to v10 | all pass (v10: 51 + 66) | all pass (v10: 51 + 66) |
| Staged v10 checks | 51 + 66 | 51 + 66 |
| Probe 1 (23 behaviors, written after v6) and probe 2 (17 lines, written for v10) | 40/40 | 40/40 |
| Lines left out of the spec: bad cursor on replies, pins and search `400`; replies and pins cursors on another node; resume from 0 on another node is dense and ends with `resumed`; search by another `workspace_id` is empty; topic over 250 `400`; duplicate name `409` | all as `COMPONENTS.md` says | all as `COMPONENTS.md` says |
| `include_archived=true` alias | accepted | accepted |
| Message edit and delete (not in Component B) | built | not built (`404`) |

Every earlier final app fails at least three of the probe items. The two finals differ only on message
edit and delete, which Component B does not ask for. I found no remaining gap, so v10 is the final
spec.

## Comparison of the approaches

On the final spec (v10) both graded approaches reached every rule: 51/51 for Spec A and 66/66 for
Spec B in both layouts, the apps passed all 40 extra behaviors I probed, and none hard-coded
anything. The difference was in how they got there. The staged layout made the agent work in order: once the
README said to read one spec file at a time, it built and checked each step before moving on, and
failures stayed small and local (the v6 staged run took 5 minutes and $0.38). Its weak point was
the full checks: they run more rules in one command, so in a slow environment they hit time
limits, and the v7 staged run spent 22 minutes and $1.18 partly chasing a rule my check skipped
for time. After v8 ran that rule first and v10 fixed the stale counts in the step files, the
final staged run took 70 steps and $0.87 with no such problem. The single-file runs needed more
spec iteration and were where the agent took shortcuts: it hard-coded read counts (B v1), fitted
its count to my test (B v4) and spent about 20 steps on a misleading failure (B v5); each of those
runs led to a better rule or a clearer failure message, and the final single-file run was clean
in 8 minutes and $0.51 (43 steps, fewer than the staged run). The biggest lesson from v7 to v10
was that adding one rule for each gap a run showed did not converge: three runs in a row passed
every rule and still had a new gap. Going through `COMPONENTS.md` line by line once (v10) closed
them all. Building one
component at a time (Approaches 2 and 4) was mainly useful for finding spec problems cheaply, since
an A-only run takes 2 to 5 minutes and $0.04 to $0.27; because `/app` starts empty, its B step is
the same as building both together. In every approach the agent submitted with about 45 minutes
left once my checks passed, so the checks, not the time limit, decided how much was built
correctly: adding rules (and probing the finished apps for behavior the rules missed) helped far
more than telling the agent to keep testing. All 16 runs together cost about $5.83 (no run over
$1.18).
