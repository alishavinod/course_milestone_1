# Channels.4: read state

Check: `bash /spec/channels/checks/check_channels_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_channels_1.sh`, `check_channels_2.sh` and
`check_channels_3.sh` all pass.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From | Used here for |
|---|---|---|
| `require_user(request)` | Roles.1 | The caller |
| `get_channel(channel_id)` | Channels.1 | `404` for unknown channels |
| `workspace_detail(...)` | Channels.1 | Now fills `read_state` |
| `emit_event(...)`, the `events` table | Channels.3 | Which messages come after `last_read_event_id` |
| `resolve_mentions(...)` | Channels.3 | Which of those mention the reader |

## Adds

| Function | What it does |
|---|---|
| `get_read_state(user_id, channel_id)` | Works out `{channel_id, last_read_event_id, unread_count, mention_count}` |
| `mark_read(user_id, channel_id, event_id)` | Stores the larger of the old and new `last_read_event_id` |

## Rules

### 6. Read state

6.1 **Get.** `GET /api/channels/{id}/read` returns `200 {read_state: {channel_id,
    last_read_event_id, unread_count, mention_count}}`. `last_read_event_id` starts at `0`.

6.2 **Counts.** `unread_count` is the number of top-level messages by other users with an
    `event_id` above `last_read_event_id`. `mention_count` is how many of those mention you.

6.3 **Mark read.** `POST /api/channels/{id}/read` with `{last_read_event_id}` returns the
    updated `{read_state}`, with counts worked out from the new `last_read_event_id`. Marking
    an older message read leaves the newer ones unread. Marking the latest event read makes
    both counts `0`, and `GET` shows the same.

6.4 **Never backwards.** Posting a smaller `last_read_event_id` does not move it back.

6.5 **In workspace detail.** `read_state` in `GET /api/workspaces/{slug}` is a list with one
    `read_state` object per channel the caller is in.

Every request must answer within 5 seconds.
