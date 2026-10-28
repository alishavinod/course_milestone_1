# Spec B: channels and message history

Check: `bash /spec/channels/checks/check_channels.sh`. The rule numbers below match its output.

Spec B builds on Spec A (register, workspaces, joining). `check_roles.sh` must keep passing.

## Tips

- Use **FastAPI + `uvicorn[standard]`** (put both in `/app/requirements.txt`). Section 5 needs
  real WebSockets, which Python's built-in `http.server` cannot do.
- **Events.** Store every event in one `events` table: `(channel_id, event_id, type, payload)`.
  `event_id` is `1 + the highest event_id for that channel`, written in the same transaction
  as the change (`BEGIN IMMEDIATE`). Each node checks the table every ~100 ms and sends new
  rows to its WebSocket subscribers. Then all three nodes see the same numbered events.
- **Cursors.** A cursor can be a base64 string holding the last message id of the page. The
  next page is "messages with a smaller id, newest first". A cursor that does not decode is
  `400`.
- Timestamps look like `2026-05-16T08:07:34.123Z` (UTC).

## Objects

- `ChannelObj`: `{id, workspace_id, name, is_private, is_dm, topic, is_archived}`. `topic`
  starts as `""`.
- `UserObj`: `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}`.
- `MessageObj`: `{id, channel_id, author_id, author, body, parent_id, reply_count, created_at,
  edited_at, files, reactions, mentions}`. `author` is a `UserObj`. `files` and `reactions`
  are `[]` for now. `mentions` is a list of user ids.

## 1. Channels

1.1 **Create.** `POST /api/workspaces/{slug}/channels` with `{name}` returns `201 {channel}`
    with every `ChannelObj` field. `topic` is `""`, `is_archived` and `is_dm` are `false`.

1.2 **Bad or duplicate name.** The name must match `^[a-z0-9-]{1,32}$`: `Bad Name` returns
    `400`. A name already used in the workspace returns `409`.

1.3 **Topic.** `PATCH /api/channels/{id}` with `{topic}` returns `200 {channel}` with the new
    topic. A topic over 250 characters returns `400` (on PATCH and on create).

1.4 **Who can set the topic.** Only a channel member or the workspace owner. Anyone else gets
    `403`.

1.5 **Workspace detail.** `GET /api/workspaces/{slug}` returns `200 {workspace, channels,
    read_state}`. `channels` leaves out private channels the caller is not in.

1.6 **Archive.** `POST /api/channels/{id}/archive` is workspace-owner only: the owner gets
    `200 {channel}` with `is_archived: true`, anyone else gets `403`.

1.7 **Archived channels.** Posting a message to an archived channel returns `423`. Workspace
    detail leaves archived channels out, unless called with `?include_archived=1`.

1.8 **Unarchive.** `POST /api/channels/{id}/unarchive` (owner only) returns `200 {channel}`
    with `is_archived: false`, and posting works again (`201`).

1.9 **Leave.** `POST /api/channels/{id}/leave` returns `200 {channel}`. Leaving again also
    returns `200`.

1.10 **Channel members.** `GET /api/channels/{id}/members` returns `200 {members: [UserObj,
     ...]}`.

## 2. Messages and paging

2.1 **Post.** `POST /api/channels/{id}/messages` with `{body}` returns `201 {message}` with
    every `MessageObj` field. `parent_id` is `null`, `reply_count` is `0`, `created_at` is a
    UTC timestamp.

2.2 **Empty body.** `{body: ""}` returns `400`.

2.3 **Newest first.** `GET /api/channels/{id}/messages` returns `200 {messages, next_cursor}`
    with the newest message first.

2.4 **Paging.** With `?limit=2`, each page has 2 messages and a `next_cursor` string. Passing
    `&cursor=<next_cursor>` returns the next older messages. The last page has
    `next_cursor: null`. No message is skipped or repeated.

2.5 **Cursors work on every node.** A cursor from port `8000` gives the same page on `8001`.

2.6 **Cursors do not move.** New messages posted after a cursor was issued do not change the
    page that cursor returns.

2.7 **Bad paging input.** A made-up cursor returns `400`. `limit=0` and `limit=201` return
    `400`. `limit=200` is allowed. The default limit is `50`.

## 3. Replies, search, pins

3.1 **Reply.** Posting `{body, parent_id}` returns `201` with that `parent_id`. The parent's
    `reply_count` goes up by 1.

3.2 **Replies list.** `GET /api/messages/{parent_id}/replies` returns `200 {replies,
    next_cursor}`, oldest first.

3.3 **Search.** `GET /api/search?q=<text>` returns `200 {results, next_cursor}`: messages
    containing the text, newest first.

3.4 **Search only what you can see.** Messages in private channels the caller is not in are
    not returned.

3.5 **Search one workspace.** `&workspace_id=<id>` only returns messages from that workspace.

3.6 **Pin.** `POST /api/messages/{id}/pin` returns `200 {pin: {message_id, pinned_by,
    pinned_at}}`. Pinning again also returns `200`.

3.7 **Pin list.** `GET /api/channels/{id}/pins` returns `200 {pins: [{message, pinned_by,
    pinned_at}], next_cursor}`, newest first.

3.8 **Unpin.** `DELETE /api/messages/{id}/pin` returns `200 {"unpinned": true}`, and the pin
    list is empty again.

3.9 **Private pins.** A user who is not in a private channel gets `403` on its pin list.

## 4. Mentions

`mentions` lists the user ids of `@username` in the body.

4.1 **Basic.** `"hi @bob"` gives `mentions: [<bob's id>]`.

4.2 **No repeats, no self.** `"@bob @bob @me"` (written by me) gives just `[<bob's id>]`.

4.3 **Not inside words.** `"me@bob.com"` and `"abc@bob"` mention nobody.

4.4 **Workspace only.** `@username` of a user who is not in the workspace is left out.

4.5 **@channel.** `@channel` mentions every channel member except the author.

## 5. Events (WebSocket)

Connect to `ws://127.0.0.1:<port>/api/ws?token=<token>`. Frames are JSON, one per message.

5.1 **Connect.** A valid token connects on `8000` and `8001`. (A bad token is still rejected.)

5.2 **Subscribe.** Sending `{"type": "subscribe", "channel_id": <id>}` gets back
    `{"type": "subscribed", "channel_id": <id>, "head_event_id": <int>}`, where
    `head_event_id` is the channel's latest `event_id` (`0` if none). Every node gives the same
    number.

5.3 **New message event.** Posting a message sends subscribers
    `{"type": "message.created", "event_id": <head + 1>, "channel_id": <id>, "message": ...}`.

5.4 **Every node.** A subscriber on `8001` gets the same event, with the same `event_id`, for a
    message posted on `8000`.

5.5 **Dense event ids.** These actions each add exactly one event, numbered one after another
    with no gaps: a message (`message.created`), a reply (`message.reply`), a topic change
    (`channel.updated`), a user joining (`member.joined`), a user leaving (`member.left`).
    Archive and unarchive add `channel.archived` and `channel.unarchived`.

5.6 **Resume.** Sending `{"type": "resume", "channel_id": <id>, "since_event_id": <n>}` (on any
    node) sends every event after `n` in order, then
    `{"type": "resumed", "channel_id": <id>, "head_event_id": <latest>}`.

## 6. Read state

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
