#!/usr/bin/env bash
# Self-check for Channels.3 (mentions and events). Prints PASS/FAIL per rule in /spec/channels/channels-3.md.
# Usage: bash /spec/channels/checks/check_channels_3.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# ment <body>  -> posts as O in CH and prints the sorted mention ids
ment() {
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"$1\"}" "$O_T" >/dev/null
  pick '",".join(sorted(str(x) for x in d["message"]["mentions"]))'
}
sorted_ids() { printf '%s\n' "$@" | sort | paste -sd, -; }

rules_channels_3() {
  local m u t want
  # setup from Spec A and Channels.1: workspace WS and public channel CH; B and C join CH here
  need_base; need_ch
  for u in B C; do t="${u}_T"; call POST $N0/api/channels/${CH:-0}/join "{}" "${!t}" >/dev/null; done

  m=$(ment "hi @u${RUN}_B")
  if [ "$m" = "$B_ID" ]; then ok 4.1 "@username is mentioned"
  else bad 4.1 "'hi @user' gave mentions [$m] (want [$B_ID])"; fi

  m=$(ment "@u${RUN}_B @u${RUN}_B @u${RUN}_O")
  if [ "$m" = "$B_ID" ]; then ok 4.2 "mentions are deduped and skip yourself"
  else bad 4.2 "'@b @b @me' gave mentions [$m] (want [$B_ID])"; fi

  m=$(ment "mail me@u${RUN}_B.com or abc@u${RUN}_B")
  if [ -z "$m" ]; then ok 4.3 "email and in-word @ are not mentions"
  else bad 4.3 "email and in-word @ gave mentions [$m] (want [])"; fi

  m=$(ment "hello @u${RUN}_N")
  if [ -z "$m" ]; then ok 4.4 "users outside the workspace are not mentioned"
  else bad 4.4 "a user outside the workspace gave mentions [$m] (want [])"; fi

  m=$(ment "@channel look"); want=$(sorted_ids "$B_ID" "$C_ID")
  if [ "$m" = "$want" ]; then ok 4.5 "@channel mentions every member except the author"
  else bad 4.5 "@channel gave mentions [$m] (want [$want])"; fi

  # section 5 needs a WebSocket client, so it runs in Python (helpers in ws_lib.py)
  run_py 6 5.0 <<'PY'
import os, sys; sys.path.insert(0, os.environ["CHECKS"])
from ws_lib import *

S = {}
s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"ev-{RUN}"}, O)
EV = (b or {}).get("channel", {}).get("id")

def r51():
    S["w0"], S["w1"] = WS(8000, O), WS(8001, O)
def r52():
    for k in ("w0", "w1"): S[k].send({"type": "subscribe", "channel_id": EV})
    f0, f1 = S["w0"].first(is_sub), S["w1"].first(is_sub)
    assert f0 and f1, "no 'subscribed' frame within 3s"
    assert isinstance(f0.get("head_event_id"), int), f"head_event_id is {f0.get('head_event_id')!r}, want an int"
    assert f0["head_event_id"] == f1["head_event_id"], f"head on 8000 is {f0['head_event_id']}, on 8001 {f1['head_event_id']}"
    S["h"] = f0["head_event_id"]
def r53():
    h = S["h"]
    s, b = call("POST", f"/api/channels/{EV}/messages", {"body": "event one"}, O)
    assert s == 201, f"post message got {s}"
    S["m"] = b["message"]["id"]
    got = S["w0"].events(EV, 1)
    assert got == [(h + 1, "message.created")], f"8000 got {got}, want [({h + 1}, 'message.created')]"
def r54():
    got = S["w1"].events(EV, 1)
    assert got == [(S["h"] + 1, "message.created")], f"8001 got {got}, want [({S['h'] + 1}, 'message.created')]"
def r55():
    h = S["h"]
    steps = [(call("POST", f"/api/channels/{EV}/messages", {"body": "event reply", "parent_id": S["m"]}, O), "reply"),
             (call("PATCH", f"/api/channels/{EV}", {"topic": "events"}, O), "topic"),
             (call("POST", f"/api/channels/{EV}/join", {}, B), "join"),
             (call("POST", f"/api/channels/{EV}/leave", {}, B), "leave")]
    for (s, _), what in steps:
        assert s in (200, 201), f"{what} request got {s}"
    want = [(h + 2, "message.reply"), (h + 3, "channel.updated"), (h + 4, "member.joined"), (h + 5, "member.left")]
    got = S["w0"].events(EV, 4)
    assert got == want, f"got {got}, want {want}"
def r56():
    h = S["h"]
    w = WS(8002, O)
    w.send({"type": "resume", "channel_id": EV, "since_event_id": h})
    got, done = [], None
    for f in w.frames():
        if f.get("type") == "resumed": done = f; break
        if isinstance(f.get("event_id"), int): got.append((f["event_id"], f.get("type")))
    want = [(h + 1, "message.created"), (h + 2, "message.reply"), (h + 3, "channel.updated"),
            (h + 4, "member.joined"), (h + 5, "member.left")]
    assert got == want, f"replayed {got}, want {want}"
    assert done and done.get("head_event_id") == h + 5, f"'resumed' frame was {done}, want head_event_id {h + 5}"

rule("5.1", "a valid token connects on 8000 and 8001", r51)
rule("5.2", "subscribe answers 'subscribed' with the same head_event_id on every node", r52)
rule("5.3", "posting a message sends message.created with event_id head+1", r53)
rule("5.4", "a subscriber on 8001 gets the event for a message posted on 8000", r54)
rule("5.5", "reply, topic, join, leave each add one event, with no gaps", r55)
rule("5.6", "resume on 8002 replays every event after since_event_id, then 'resumed'", r56)
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_3; finish; }
