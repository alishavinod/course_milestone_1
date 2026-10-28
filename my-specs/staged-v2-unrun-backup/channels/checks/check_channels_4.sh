#!/usr/bin/env bash
# Self-check for Channels.4 (read state). Prints PASS/FAIL per rule in /spec/channels/channels-4.md.
# Usage: bash /spec/channels/checks/check_channels_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_channels_4() {
  # setup from Spec A: users O and B in workspace WS
  need_base

  # B reads channel RS, where O posts 3 messages, one mentioning B. Event ids come from a WebSocket.
  run_py 5 6.0 <<'PY'
import os, sys; sys.path.insert(0, os.environ["CHECKS"])
from ws_lib import *

S = {}
s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"rs-{RUN}"}, O)
RS = (b or {}).get("channel", {}).get("id")

def r61():
    assert call("POST", f"/api/channels/{RS}/join", {}, B)[0] == 200, "B could not join the channel"
    w = WS(8000, B); w.send({"type": "subscribe", "channel_id": RS}); w.first(is_sub)
    for body in ("one", "two", f"hi @u{RUN}_B"):
        assert call("POST", f"/api/channels/{RS}/messages", {"body": body}, O)[0] == 201, "post message failed"
    got = w.events(RS, 3)
    assert len(got) == 3, f"expected 3 message events, got {got}"
    S["h2"], S["h3"] = got[1][0], got[-1][0]
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    rs = (b or {}).get("read_state") or {}
    assert s == 200, f"GET read got {s}"
    missing = [k for k in ("channel_id", "last_read_event_id", "unread_count", "mention_count") if k not in rs]
    assert not missing, f"read_state is missing {missing}"
    assert rs["last_read_event_id"] == 0, f"last_read_event_id starts at {rs['last_read_event_id']}, want 0"
    S["rs"] = rs
def r62():
    rs = S["rs"]
    assert (rs["unread_count"], rs["mention_count"]) == (3, 1), \
        f"unread_count {rs['unread_count']}, mention_count {rs['mention_count']} (want 3 and 1)"
def r63():
    # mark the 2nd message read: only the 3rd (which mentions B) is still unread
    s, b = call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h2"]}, B)
    rs = (b or {}).get("read_state") or {}
    assert s == 200, f"POST read got {s}"
    got = (rs.get("last_read_event_id"), rs.get("unread_count"), rs.get("mention_count"))
    assert got == (S["h2"], 1, 1), f"after marking the 2nd of 3 messages read: last_read, unread, mentions = {got}, want ({S['h2']}, 1, 1)"
    # mark the latest read: both counts 0, in the POST answer and in a later GET
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"]}, B)
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    rs = (b or {}).get("read_state") or {}
    got = (rs.get("last_read_event_id"), rs.get("unread_count"), rs.get("mention_count"))
    assert got == (S["h3"], 0, 0), f"GET read after marking the latest read: last_read, unread, mentions = {got}, want ({S['h3']}, 0, 0)"
def r64():
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"]}, B)
    call("POST", f"/api/channels/{RS}/read", {"last_read_event_id": S["h3"] - 2}, B)
    s, b = call("GET", f"/api/channels/{RS}/read", token=B)
    lr = ((b or {}).get("read_state") or {}).get("last_read_event_id")
    assert lr == S["h3"], f"last_read_event_id moved back to {lr}, want {S['h3']}"
def r65():
    s, b = call("GET", f"/api/workspaces/{SLUG}", token=B)
    rs = (b or {}).get("read_state")
    items = rs if isinstance(rs, list) else list(rs.values()) if isinstance(rs, dict) else []
    mine = [r for r in items if isinstance(r, dict) and str(r.get("channel_id")) == str(RS)]
    assert mine, f"no read_state entry for the channel in workspace detail (got {json.dumps(rs)[:80]})"
    assert mine[0].get("last_read_event_id") == S["h3"], f"last_read_event_id there is {mine[0].get('last_read_event_id')}"

rule("6.1", "GET read returns read_state starting at 0", r61)
rule("6.2", "unread_count and mention_count count new messages", r62)
rule("6.3", "marking read recounts unread and mentions (partly read, then all read)", r63)
rule("6.4", "last_read_event_id never moves back", r64)
rule("6.5", "workspace detail has the same read_state", r65)
PY
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_channels_4; finish; }
