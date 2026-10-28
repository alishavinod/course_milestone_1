#!/usr/bin/env bash
# Self-check for Spec B. Prints PASS/FAIL per rule in /spec/channels/channels.md.
# Usage: bash /spec/channels/checks/check_channels.sh      Exit 0 only if every rule passes.
set -u
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_channels_body.json
RUN="$(date +%s | tail -c 6)$RANDOM"
PW="correct-horse-1"
pass=0; fail=0

ok()  { echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { echo "FAIL $1 $2"; fail=$((fail+1)); }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 5 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
    print(v)
except Exception: pass' "$BODY" "$1"
}
# pick <python expression using d>  -> evaluates it on the last response body d, prints the result
pick() {
  python3 -c 'import json,sys,re
try:
    d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))
except Exception: pass' "$BODY" "$1"
}
# bodies <list-key>  -> the "body" of each item in that list, comma separated
bodies() { pick "\",\".join(m[\"body\"] for m in d[\"$1\"])"; }
# enc <text>  -> URL-encodes text (for cursors)
enc() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
# new_user <NAME>  -> registers a user, sets NAME_T (token) and NAME_ID
new_user() {
  call POST $N0/api/auth/register "{\"username\":\"u${RUN}_$1\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_T" '%s' "$(field token)"; printf -v "$1_ID" '%s' "$(field user.id)"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# Start the cluster if it is not answering. Wait at most 15s so the check fits the 30s limit.
if ! healthy; then
  # only start.sh goes to the background (with its own output), so this check still exits cleanly
  (cd /app; nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &)
  for _ in $(seq 1 15); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

# Setup (Spec A routes): O owns workspace WS; B and C are members; N is not in the workspace.
for u in O B C N; do new_user $u; done
WS="ws-$RUN-c"
call POST $N0/api/workspaces "{\"slug\":\"$WS\",\"name\":\"Check B\"}" "$O_T" >/dev/null
WS_ID=$(field workspace.id); GEN=$(field general_channel.id)
for u in B C; do t="${u}_T"; call POST $N0/api/channels/${GEN:-0}/join "{}" "${!t}" >/dev/null; done
[ -n "$O_T" ] && [ -n "$WS_ID" ] || echo "NOTE setup failed (register or create workspace). Run check_roles.sh and fix Spec A first."

# two_pages <url> <list-key> <python expression for one item x>  -> "<page 1 items>|<page 2 items>|<page 2 next_cursor is null>" with limit=1
two_pages() {
  local sep='?' i1 i2 c1 n2
  [[ "$1" == *\?* ]] && sep='&'
  call GET "$1${sep}limit=1" "" "$O_T" >/dev/null; i1=$(pick "[$3 for x in d[\"$2\"]]"); c1=$(field next_cursor)
  call GET "$1${sep}limit=1&cursor=$(enc "$c1")" "" "$O_T" >/dev/null; i2=$(pick "[$3 for x in d[\"$2\"]]"); n2=$(pick 'd["next_cursor"] is None')
  echo "$i1|$i2|$n2"
}

# ---------- 1. channels ----------
s=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"chan-$RUN\"}" "$O_T"); CH=$(field channel.id)
f=$(pick 'all(k in d["channel"] for k in ["id","workspace_id","name","is_private","is_dm","topic","is_archived"]) and d["channel"]["topic"]=="" and d["channel"]["is_archived"] is False and d["channel"]["is_dm"] is False')
if [ "$s" = 201 ] && [ "$f" = True ]; then ok 1.1 "create channel returns 201 with every channel field"
else bad 1.1 "create channel: expected 201 with id, workspace_id, name, is_private, is_dm, topic=\"\", is_archived=false; got $s"; fi

a=$(call POST $N0/api/workspaces/$WS/channels '{"name":"Bad Name"}' "$O_T")
b=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"chan-$RUN\"}" "$O_T")
if [ "$a" = 400 ] && [ "$b" = 409 ]; then ok 1.2 "bad name 400, duplicate name 409"
else bad 1.2 "bad name got $a (want 400), duplicate name got $b (want 409)"; fi

LONG=$(printf 'x%.0s' $(seq 1 251))
a=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"hello"}' "$O_T"); t=$(field channel.topic)
b=$(call PATCH $N0/api/channels/${CH:-0} "{\"topic\":\"$LONG\"}" "$O_T")
c=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"long-$RUN\",\"topic\":\"$LONG\"}" "$O_T")
if [ "$a" = 200 ] && [ "$t" = hello ] && [ "$b" = 400 ] && [ "$c" = 400 ]; then ok 1.3 "topic update 200, topic over 250 chars 400"
else bad 1.3 "PATCH topic: $a with topic '$t' (want 200, hello), 251 chars on PATCH: $b and on create: $c (want 400)"; fi

s=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"nope"}' "$C_T")
expect 1.4 "non-member sets the topic" 403 "$s"

call POST $N0/api/workspaces/$WS/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$O_T" >/dev/null; PRIV=$(field channel.id)
s=$(call GET $N0/api/workspaces/$WS "" "$B_T")
f=$(pick '"workspace" in d and "read_state" in d and isinstance(d["channels"],list)')
names=$(pick '",".join(c["name"] for c in d["channels"])')
if [ "$s" = 200 ] && [ "$f" = True ] && [[ ",$names," == *",chan-$RUN,"* ]] && [[ ",$names," != *",priv-$RUN,"* ]]; then
  ok 1.5 "workspace detail lists channels and hides private ones"
else bad 1.5 "GET /api/workspaces/{slug}: status $s, has workspace/channels/read_state: '$f', channel names: '$names'"; fi

call POST $N0/api/channels/${CH:-0}/join "{}" "$B_T" >/dev/null
a=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$B_T")
b=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$O_T"); ar=$(field channel.is_archived)
if [ "$a" = 403 ] && [ "$b" = 200 ] && [ "$ar" = True ]; then ok 1.6 "only the owner can archive"
else bad 1.6 "member archive: $a (want 403), owner archive: $b with is_archived '$ar' (want 200, true)"; fi

a=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"while archived"}' "$O_T")
call GET $N0/api/workspaces/$WS "" "$O_T" >/dev/null; n1=$(pick '",".join(c["name"] for c in d["channels"])')
call GET "$N0/api/workspaces/$WS?include_archived=1" "" "$O_T" >/dev/null; n2=$(pick '",".join(c["name"] for c in d["channels"])')
if [ "$a" = 423 ] && [[ ",$n1," != *",chan-$RUN,"* ]] && [[ ",$n2," == *",chan-$RUN,"* ]]; then ok 1.7 "archived: post 423, hidden unless include_archived=1"
else bad 1.7 "post to archived: $a (want 423), listed without flag: $([[ ",$n1," == *",chan-$RUN,"* ]] && echo yes || echo no) (want no), with include_archived=1: $([[ ",$n2," == *",chan-$RUN,"* ]] && echo yes || echo no) (want yes)"; fi

a=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$O_T"); ar=$(field channel.is_archived)
b=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"after unarchive"}' "$O_T")
if [ "$a" = 200 ] && [ "$ar" = False ] && [ "$b" = 201 ]; then ok 1.8 "unarchive restores posting"
else bad 1.8 "unarchive: $a with is_archived '$ar' (want 200, false), post after: $b (want 201)"; fi

a=$(call POST $N0/api/channels/${CH:-0}/leave "{}" "$B_T"); cid=$(field channel.id)
b=$(call POST $N0/api/channels/${CH:-0}/leave "{}" "$B_T")
if [ "$a" = 200 ] && [ "$cid" = "$CH" ] && [ "$b" = 200 ]; then ok 1.9 "leave returns 200 {channel}, twice"
else bad 1.9 "leave: $a (want 200 with channel), leave again: $b (want 200)"; fi

s=$(call GET $N0/api/channels/${CH:-0}/members "" "$O_T")
f=$(pick 'all(all(k in u for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]) for u in d["members"]) and any(str(u["id"])=="'"$O_ID"'" for u in d["members"])')
if [ "$s" = 200 ] && [ "$f" = True ]; then ok 1.10 "channel members are full user objects"
else bad 1.10 "GET /api/channels/{id}/members: $s; want {members:[UserObj]} including the owner with all 7 user fields"; fi

a=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"$(printf 'n%.0s' $(seq 1 33))\"}" "$O_T")
b=$(call POST $N0/api/workspaces/$WS/channels "{\"name\":\"desc-$RUN\",\"description\":\"from description\"}" "$O_T"); t=$(pick 'd["channel"]["topic"]==""')
if [ "$a" = 400 ] && [ "$b" = 201 ] && [ "$t" = True ]; then ok 1.11 "33-char name 400, description does not fill topic"
else bad 1.11 "33-char name: $a (want 400), create with description: $b, topic still empty: '$t' (want 201, True)"; fi

a=$(call POST $N0/api/channels/${CH:-0}/archive "{}" "$O_T")
b=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$C_T")
c=$(call POST $N0/api/channels/${CH:-0}/unarchive "{}" "$O_T")
if [ "$a$b$c" = 200403200 ]; then ok 1.12 "only the owner can unarchive"
else bad 1.12 "owner archive: $a (want 200), another user unarchive: $b (want 403), owner unarchive: $c (want 200)"; fi

# ---------- 2. messages and paging ----------
s=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"hello world"}' "$O_T"); M1=$(field message.id)
f=$(pick 'all(k in d["message"] for k in ["id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"]) and d["message"]["parent_id"] is None and d["message"]["reply_count"]==0 and bool(re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$", d["message"]["created_at"]))')
if [ "$s" = 201 ] && [ "$f" = True ]; then ok 2.1 "post message returns 201 with every message field"
else bad 2.1 "post message: $s; want 201 with all 12 message fields, parent_id null, reply_count 0, UTC created_at"; fi

s=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":""}' "$O_T")
expect 2.2 "empty body" 400 "$s"

call POST $N0/api/workspaces/$WS/channels "{\"name\":\"page-$RUN\"}" "$O_T" >/dev/null; PG=$(field channel.id)
for i in 1 2 3 4 5; do call POST $N0/api/channels/${PG:-0}/messages "{\"body\":\"p$i\"}" "$O_T" >/dev/null; done
s=$(call GET $N0/api/channels/${PG:-0}/messages "" "$O_T"); all=$(bodies messages); nc=$(pick '"next_cursor" in d and d["next_cursor"] is None')
if [ "$s" = 200 ] && [ "$all" = "p5,p4,p3,p2,p1" ] && [ "$nc" = True ]; then ok 2.3 "messages are newest first with next_cursor"
else bad 2.3 "GET messages: $s, order '$all' (want p5,p4,p3,p2,p1), next_cursor null: '$nc'"; fi

call GET "$N0/api/channels/${PG:-0}/messages?limit=2" "" "$O_T" >/dev/null; p1=$(bodies messages); C1=$(field next_cursor)
call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; p2=$(bodies messages); C2=$(field next_cursor)
call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C2")" "" "$O_T" >/dev/null; p3=$(bodies messages); n3=$(pick 'd["next_cursor"] is None')
if [ "$p1|$p2|$p3" = "p5,p4|p3,p2|p1" ] && [ "$n3" = True ]; then ok 2.4 "limit=2 pages through every message once"
else bad 2.4 "pages were '$p1' | '$p2' | '$p3' (want p5,p4 | p3,p2 | p1), last next_cursor null: '$n3'"; fi

call GET "$N1/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; q=$(bodies messages)
if [ "$q" = "p3,p2" ]; then ok 2.5 "a cursor from 8000 works on 8001"
else bad 2.5 "cursor from 8000 used on 8001 gave '$q' (want p3,p2)"; fi

call POST $N0/api/channels/${PG:-0}/messages '{"body":"p6"}' "$O_T" >/dev/null
call GET "$N0/api/channels/${PG:-0}/messages?limit=2&cursor=$(enc "$C1")" "" "$O_T" >/dev/null; q=$(bodies messages)
if [ "$q" = "p3,p2" ]; then ok 2.6 "a cursor still gives the same page after new messages"
else bad 2.6 "after posting p6, the old cursor gave '$q' (want p3,p2)"; fi

a=$(call GET "$N0/api/channels/${PG:-0}/messages?cursor=garbage-$RUN" "" "$O_T")
b=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=0" "" "$O_T")
c=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=201" "" "$O_T")
d=$(call GET "$N0/api/channels/${PG:-0}/messages?limit=200" "" "$O_T")
if [ "$a$b$c$d" = 400400400200 ]; then ok 2.7 "bad cursor and bad limit return 400"
else bad 2.7 "made-up cursor: $a, limit=0: $b, limit=201: $c (want 400 each), limit=200: $d (want 200)"; fi

call POST $N0/api/workspaces/$WS/channels "{\"name\":\"lim-$RUN\"}" "$O_T" >/dev/null; LM=$(field channel.id)
for i in $(seq 1 51); do call POST $N0/api/channels/${LM:-0}/messages "{\"body\":\"m$i\"}" "$O_T" >/dev/null; done
s=$(call GET $N0/api/channels/${LM:-0}/messages "" "$O_T"); n=$(pick 'len(d["messages"])'); nc=$(pick 'isinstance(d["next_cursor"],str)')
if [ "$s" = 200 ] && [ "$n" = 50 ] && [ "$nc" = True ]; then ok 2.8 "limit defaults to 50"
else bad 2.8 "no limit, 51 messages in the channel: status $s, $n messages, next_cursor is a string: '$nc' (want 200, 50, True)"; fi

# ---------- 3. replies, search, pins ----------
s=$(call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"r1\",\"parent_id\":${M1:-0}}" "$O_T"); pid=$(field message.parent_id)
call GET "$N0/api/channels/${CH:-0}/messages?limit=200" "" "$O_T" >/dev/null
rc=$(pick '[m["reply_count"] for m in d["messages"] if str(m["id"])=="'"$M1"'"][0]')
if [ "$s" = 201 ] && [ "$pid" = "$M1" ] && [ "$rc" = 1 ]; then ok 3.1 "reply has parent_id and bumps reply_count"
else bad 3.1 "reply: $s with parent_id '$pid' (want 201, $M1), parent reply_count '$rc' (want 1)"; fi

call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"r2\",\"parent_id\":${M1:-0}}" "$O_T" >/dev/null
s=$(call GET $N0/api/messages/${M1:-0}/replies "" "$O_T"); r=$(bodies replies); nc=$(pick '"next_cursor" in d')
if [ "$s" = 200 ] && [ "$r" = "r1,r2" ] && [ "$nc" = True ]; then ok 3.2 "replies list is oldest first"
else bad 3.2 "GET replies: $s, order '$r' (want r1,r2), has next_cursor: '$nc'"; fi

# search results may be messages, or objects holding a "message"
RB='",".join((x.get("message") or x)["body"] for x in d["results"])'
call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"zebra$RUN one\"}" "$O_T" >/dev/null
call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"zebra$RUN two\"}" "$O_T" >/dev/null
s=$(call GET "$N0/api/search?q=zebra$RUN" "" "$O_T"); r=$(pick "$RB"); nc=$(pick '"next_cursor" in d')
if [ "$s" = 200 ] && [ "$r" = "zebra$RUN two,zebra$RUN one" ] && [ "$nc" = True ]; then ok 3.3 "search finds messages, newest first"
else bad 3.3 "search: $s, results '$r' (want the 'two' message then 'one'), has next_cursor: '$nc'"; fi

call POST $N0/api/channels/${PRIV:-0}/messages "{\"body\":\"secret$RUN\"}" "$O_T" >/dev/null
call GET "$N0/api/search?q=secret$RUN" "" "$B_T" >/dev/null; nb=$(pick 'len(d["results"])')
call GET "$N0/api/search?q=secret$RUN" "" "$O_T" >/dev/null; no=$(pick 'len(d["results"])')
if [ "$nb" = 0 ] && [ "$no" = 1 ]; then ok 3.4 "search hides private channels you are not in"
else bad 3.4 "private message found by a non-member: '$nb' (want 0), by the owner: '$no' (want 1)"; fi

call POST $N0/api/workspaces "{\"slug\":\"ws-$RUN-d\",\"name\":\"Other\"}" "$O_T" >/dev/null; WS2_ID=$(field workspace.id); GEN2=$(field general_channel.id)
call POST $N0/api/channels/${GEN2:-0}/messages "{\"body\":\"both$RUN\"}" "$O_T" >/dev/null
call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"both$RUN\"}" "$O_T" >/dev/null
s=$(call GET "$N0/api/search?q=both$RUN&workspace_id=${WS2_ID:-0}" "" "$O_T"); r=$(pick '[str((x.get("message") or x)["channel_id"]) for x in d["results"]]')
if [ "$s" = 200 ] && [ "$r" = "['$GEN2']" ]; then ok 3.5 "workspace_id limits search to one workspace"
else bad 3.5 "search with workspace_id: $s, result channel ids $r (want only ['$GEN2'])"; fi

a=$(call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T"); f=$(pick 'str(d["pin"]["message_id"])=="'"$M1"'" and "pinned_by" in d["pin"] and "pinned_at" in d["pin"]')
b=$(call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T")
if [ "$a" = 200 ] && [ "$f" = True ] && [ "$b" = 200 ]; then ok 3.6 "pin returns 200 {pin}, pinning again is 200"
else bad 3.6 "pin: $a with pin object ok '$f' (want 200 and {pin:{message_id,pinned_by,pinned_at}}), pin again: $b (want 200)"; fi

s=$(call GET $N0/api/channels/${CH:-0}/pins "" "$O_T")
f=$(pick 'len(d["pins"])==1 and str(d["pins"][0]["message"]["id"])=="'"$M1"'" and "pinned_by" in d["pins"][0] and "next_cursor" in d')
if [ "$s" = 200 ] && [ "$f" = True ]; then ok 3.7 "pin list shows the pinned message"
else bad 3.7 "GET pins: $s; want {pins:[{message, pinned_by, pinned_at}], next_cursor} with one pin"; fi

a=$(call DELETE $N0/api/messages/${M1:-0}/pin "" "$O_T"); u=$(field unpinned)
call GET $N0/api/channels/${CH:-0}/pins "" "$O_T" >/dev/null; n=$(pick 'len(d["pins"])')
if [ "$a" = 200 ] && [ "$u" = True ] && [ "$n" = 0 ]; then ok 3.8 "unpin returns unpinned:true and empties the list"
else bad 3.8 "unpin: $a with unpinned '$u' (want 200, true), pins left: '$n' (want 0)"; fi

s=$(call GET $N0/api/channels/${PRIV:-0}/pins "" "$B_T")
expect 3.9 "non-member reads pins of a private channel" 403 "$s"

r=$(two_pages "$N0/api/messages/${M1:-0}/replies" replies 'x["body"]')
q=$(two_pages "$N0/api/search?q=zebra$RUN" results '(x.get("message") or x)["body"]')
call POST $N0/api/channels/${CH:-0}/messages '{"body":"second pin"}' "$O_T" >/dev/null; M2=$(field message.id)
call POST $N0/api/messages/${M1:-0}/pin "{}" "$O_T" >/dev/null; sleep 0.1; call POST $N0/api/messages/${M2:-0}/pin "{}" "$O_T" >/dev/null
p=$(two_pages "$N0/api/channels/${CH:-0}/pins" pins 'str(x["message"]["id"])')
if [ "$r" = "['r1']|['r2']|True" ] && [ "$q" = "['zebra$RUN two']|['zebra$RUN one']|True" ] && [ "$p" = "['$M2']|['$M1']|True" ]; then
  ok 3.10 "replies, search and pins page with limit and cursor"
else bad 3.10 "limit=1 pages (page 1 | page 2 | last next_cursor null): replies $r, search $q, pins $p; want ['r1']|['r2']|True, two then one, ['$M2']|['$M1']|True"; fi

# ---------- 4. mentions ----------
for u in B C; do t="${u}_T"; call POST $N0/api/channels/${CH:-0}/join "{}" "${!t}" >/dev/null; done
# ment <body>  -> posts as O in CH and prints the sorted mention ids
ment() {
  call POST $N0/api/channels/${CH:-0}/messages "{\"body\":\"$1\"}" "$O_T" >/dev/null
  pick '",".join(sorted(str(x) for x in d["message"]["mentions"]))'
}
sorted_ids() { printf '%s\n' "$@" | sort | paste -sd, -; }

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

# ---------- 5. events and 6. read state (WebSocket, in Python) ----------
out=$(RUN="$RUN" WS="$WS" O_T="$O_T" B_T="$B_T" timeout 15 python3 - <<'PY' 2>&1
import base64, json, os, socket, struct, time, urllib.request, urllib.error
E = os.environ; RUN = E["RUN"]; SLUG = E["WS"]; O = E["O_T"]; B = E["B_T"]

def call(method, path, body=None, token=None, port=8000):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None: req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e: return e.code, None
    except Exception: return 0, None

class WS:
    """Tiny WebSocket client: connect, send JSON, receive JSON frames."""
    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/ws?token={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk: raise AssertionError(f"port {port}: connection closed during the handshake")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b" ")[1].decode()
        assert status == "101", f"port {port}: handshake answered {status}, want 101"
    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        hdr = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise AssertionError("server closed the WebSocket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    def frames(self, timeout=3):
        """Yields JSON frames until `timeout` seconds pass with nothing new."""
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                b1, b2 = self._read(2)
            except socket.timeout:
                return
            n = b2 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if b1 & 0x0F == 1: yield json.loads(data)
            elif b1 & 0x0F == 8: raise AssertionError("server closed the WebSocket")
    def first(self, pred, timeout=3):
        return next((f for f in self.frames(timeout) if pred(f)), None)
    def events(self, cid, count, timeout=3):
        got = []
        for f in self.frames(timeout):
            if str(f.get("channel_id")) == str(cid) and isinstance(f.get("event_id"), int):
                got.append((f["event_id"], f.get("type")))
                if len(got) == count: break
        return got

S = {}
def rule(rid, desc, fn):
    try:
        fn(); print(f"PASS {rid} {desc}")
    except AssertionError as e:
        print(f"FAIL {rid} {desc}: {e}")
    except KeyError:
        print(f"FAIL {rid} {desc}: skipped, an earlier rule in this section failed")
    except Exception as e:
        print(f"FAIL {rid} {desc}: {type(e).__name__}: {e}")

s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"ev-{RUN}"}, O)
EV = (b or {}).get("channel", {}).get("id")
is_sub = lambda f: f.get("type") == "subscribed"

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
def r57():
    h = S["h"]
    for act in ("archive", "unarchive"):
        s, _ = call("POST", f"/api/channels/{EV}/{act}", {}, O)
        assert s == 200, f"{act} got {s}"
    want = [(h + 6, "channel.archived"), (h + 7, "channel.unarchived")]
    got = S["w0"].events(EV, 2)
    assert got == want, f"got {got}, want {want}"

rule("5.1", "a valid token connects on 8000 and 8001", r51)
rule("5.2", "subscribe answers 'subscribed' with the same head_event_id on every node", r52)
rule("5.3", "posting a message sends message.created with event_id head+1", r53)
rule("5.4", "a subscriber on 8001 gets the event for a message posted on 8000", r54)
rule("5.5", "reply, topic, join, leave each add one event, with no gaps", r55)
rule("5.6", "resume on 8002 replays every event after since_event_id, then 'resumed'", r56)
rule("5.7", "archive and unarchive add channel.archived and channel.unarchived", r57)
def r58():
    from concurrent.futures import ThreadPoolExecutor
    s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"cc-{RUN}"}, O)
    assert s == 201, f"create channel got {s}"
    cc = b["channel"]["id"]
    w = WS(8000, O); w.send({"type": "subscribe", "channel_id": cc}); f = w.first(is_sub)
    assert f, "no 'subscribed' frame within 3s"
    h = f["head_event_id"]
    post = lambda i: call("POST", f"/api/channels/{cc}/messages", {"body": f"c{i}"}, O, port=8000 + i % 3)[0]
    with ThreadPoolExecutor(30) as ex:
        codes = list(ex.map(post, range(30)))
    assert codes.count(201) == 30, f"{codes.count(201)} of 30 concurrent posts returned 201 (statuses {sorted(set(codes))})"
    r = WS(8001, O); r.send({"type": "resume", "channel_id": cc, "since_event_id": h})
    ids = []
    for fr in r.frames():
        if fr.get("type") == "resumed": break
        if isinstance(fr.get("event_id"), int): ids.append(fr["event_id"])
    assert ids == list(range(h + 1, h + 31)), \
        f"after 30 concurrent posts the event ids were {ids[:6]}... ({len(ids)} ids, {len(set(ids))} distinct), want {h + 1} to {h + 30} in order"
    s, b = call("GET", f"/api/channels/{cc}/messages?limit=50", token=O)
    n = len((b or {}).get("messages") or [])
    assert n == 30, f"the channel lists {n} messages, want 30"
rule("5.8", "30 concurrent posts across the three nodes get 30 dense, unique event ids", r58)

# Read state: B reads channel RS, where O posts 3 messages, one mentioning B.
s, b = call("POST", f"/api/workspaces/{SLUG}/channels", {"name": f"rs-{RUN}"}, O)
RS = (b or {}).get("channel", {}).get("id")

def r61():
    assert call("POST", f"/api/channels/{RS}/join", {}, B)[0] == 200, "B could not join the channel"
    w = WS(8000, B); w.send({"type": "subscribe", "channel_id": RS}); w.first(is_sub)
    ids = []
    for body in ("one", "two", f"hi @u{RUN}_B"):
        s, b = call("POST", f"/api/channels/{RS}/messages", {"body": body}, O)
        assert s == 201, "post message failed"
        ids.append(b["message"]["id"])
    # a thread reply: it adds an event but must not count as unread
    assert call("POST", f"/api/channels/{RS}/messages", {"body": "a reply", "parent_id": ids[0]}, O)[0] == 201, "post reply failed"
    got = w.events(RS, 4)
    assert len(got) == 4, f"expected 3 message events and 1 reply event, got {got}"
    S["h2"], S["h3"] = got[1][0], got[2][0]
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
rule("6.2", "unread_count and mention_count count new top-level messages, not replies", r62)
rule("6.3", "marking read recounts unread and mentions (partly read, then all read)", r63)
rule("6.4", "last_read_event_id never moves back", r64)
rule("6.5", "workspace detail has the same read_state", r65)
PY
)
echo "$out" | grep -E '^(PASS|FAIL) ' || true
echo "$out" | grep -vE '^(PASS|FAIL) ' | tail -3
pass=$((pass + $(echo "$out" | grep -c '^PASS '))); fail=$((fail + $(echo "$out" | grep -c '^FAIL ')))
n=$(echo "$out" | grep -cE '^(PASS|FAIL) ')
[ "$n" -lt 13 ] && { bad 5.0 "the WebSocket part stopped early ($n of 13 rules ran)"; }

# ---------- 7. auth and unknown ids ----------
a=$(call GET $N0/api/channels/${CH:-0}/messages); b=$(call POST $N0/api/channels/${CH:-0}/messages '{"body":"x"}')
c=$(call GET "$N0/api/search?q=x"); d=$(call GET $N0/api/channels/${CH:-0}/pins)
e=$(call GET $N0/api/channels/${CH:-0}/read); g=$(call PATCH $N0/api/channels/${CH:-0} '{"topic":"x"}')
if [ "$a$b$c$d$e$g" = 401401401401401401 ]; then ok 7.1 "Spec B routes return 401 without a token"
else bad 7.1 "no token: list messages $a, post $b, search $c, pins $d, read $e, set topic $g (want 401 each)"; fi

a=$(call GET $N0/api/channels/999999999/messages "" "$O_T"); b=$(call POST $N0/api/channels/999999999/messages '{"body":"x"}' "$O_T")
c=$(call POST $N0/api/channels/999999999/archive "{}" "$O_T"); d=$(call POST $N0/api/messages/999999999/pin "{}" "$O_T")
e=$(call GET $N0/api/messages/999999999/replies "" "$O_T")
if [ "$a$b$c$d$e" = 404404404404404 ]; then ok 7.2 "unknown channel or message returns 404"
else bad 7.2 "unknown id: list messages $a, post $b, archive $c, pin $d, replies $e (want 404 each)"; fi

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
