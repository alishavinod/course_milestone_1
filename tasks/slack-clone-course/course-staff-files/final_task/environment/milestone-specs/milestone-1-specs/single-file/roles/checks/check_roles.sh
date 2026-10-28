#!/usr/bin/env bash
# Self-check for Spec A. Prints PASS/FAIL per rule in /spec/roles/roles.md.
# Usage: bash /spec/roles/checks/check_roles.sh      Exit 0 only if every rule passes.
set -u
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_body.json
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
# list_has <list-key> <item-field> <value>  -> prints "yes" if that list in the last body has a matching item
list_has() {
  python3 -c 'import json,sys
try:
    if any(str(i.get(sys.argv[3]))==sys.argv[4] for i in json.load(open(sys.argv[1]))[sys.argv[2]]): print("yes")
except Exception: pass' "$BODY" "$1" "$2" "$3"
}
# role_of <slug> <token> <user_id>  -> prints that user's role in the workspace
role_of() {
  call GET "$N0/api/workspaces/$1/members" "" "$2" >/dev/null
  python3 -c 'import json,sys
try:
    for m in json.load(open(sys.argv[1]))["members"]:
        if str(m.get("user_id"))==sys.argv[2]: print(m.get("role"))
except Exception: pass' "$BODY" "$3"
}
# new_user <NAME>  -> registers a user, sets NAME_T (token) and NAME_ID
new_user() {
  call POST $N0/api/auth/register "{\"username\":\"u${RUN}_$1\",\"password\":\"$PW\"}" >/dev/null
  printf -v "$1_T" '%s' "$(field token)"; printf -v "$1_ID" '%s' "$(field user.id)"
}
# jv <id>  -> the id as a JSON value (number stays a number, anything else is quoted)
jv() { if [[ "$1" =~ ^[0-9]+$ ]]; then echo "$1"; else echo "\"$1\""; fi; }
# py <python expression on d>  -> evaluates it on the last response body d, or prints nothing
py() {
  python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))
except Exception: pass' "$BODY" "$1"
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

# ---------- 0. baseline ----------
bad_nodes=""
for i in 0 1 2; do
  s=$(call GET "http://127.0.0.1:800$i/api/health")
  [ "$s" = 200 ] && [ "$(field status)" = ok ] && [ "$(field node_id)" = "$i" ] || bad_nodes="$bad_nodes 800$i"
done
if [ -z "$bad_nodes" ]; then ok 0.1 "health returns 200 with the right node_id on all nodes"
else bad 0.1 "health: wrong status or node_id on port(s)$bad_nodes"; fi

bad_ws=""
for i in 0 1 2; do
  s=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "http://127.0.0.1:800$i/api/ws?token=bogus$RUN" 2>/dev/null)
  [ "$s" = 101 ] && bad_ws="$bad_ws 800$i"
done
if [ -z "$bad_ws" ]; then ok 0.2 "websocket with a made-up token is rejected on every node"
else bad 0.2 "websocket with a made-up token was accepted (101) on port(s)$bad_ws"; fi

# 0.3 runs first: it kills the node on port 8001 on purpose, and the other rules then use
# the node that start.sh brought back
pid=$(ss -ltnpH 2>/dev/null | grep -E ':8001 ' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
if [ -z "$pid" ]; then bad 0.3 "could not find the process listening on 8001 with ss -ltnp"
else
  kill -9 "$pid"; sleep 1
  a=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 $N0/api/health); c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 $N2/api/health)
  back=no; for _ in $(seq 1 16); do curl -fs --max-time 1 $N1/api/health >/dev/null 2>&1 && { back=yes; break; }; sleep 0.5; done
  if [ "$a$c" = 200200 ] && [ "$back" = yes ]; then ok 0.3 "after kill -9 of node 8001, 8000 and 8002 keep serving and 8001 comes back"
  else bad 0.3 "after kill -9 of node 8001: 8000 got $a, 8002 got $c (want 200 each), 8001 back within 8s: $back (start.sh must start a dead node again)"; fi
fi

# ---------- 1. auth ----------
s=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}")
O_T=$(field token); O_ID=$(field user.id)
if [ "$s" = 201 ] && [ "${#O_T}" -ge 16 ]; then ok 1.1 "register returns 201 with user and token"
else bad 1.1 "register: expected 201 and a token of 16+ chars, got status $s, token length ${#O_T}"; fi

s=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}")
expect 1.2 "duplicate username" 409 "$s"

a=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RUN\",\"password\":\"$PW\"}")
b=$(call POST $N0/api/auth/register "{\"username\":\"u${RUN}_short\",\"password\":\"short77\"}")
if [ "$a" = 400 ] && [ "$b" = 400 ]; then ok 1.3 "bad username and short password return 400"
else bad 1.3 "expected 400 for a username with a space and a 7 char password, got $a and $b"; fi

a=$(call POST $N0/api/auth/login "{\"username\":\"u${RUN}_O\",\"password\":\"$PW\"}"); t=$(field token)
b=$(call POST $N0/api/auth/login "{\"username\":\"u${RUN}_O\",\"password\":\"WRONG12345\"}")
if [ "$a" = 200 ] && [ -n "$t" ] && [ "$b" = 401 ]; then ok 1.4 "login 200, wrong password 401"
else bad 1.4 "expected login 200 with a token and wrong password 401, got $a and $b"; fi

a=$(call GET $N0/api/auth/me "" "$O_T"); b=$(call GET $N0/api/auth/me); c=$(call GET $N0/api/auth/me "" "fake-token-$RUN")
if [ "$a" = 200 ] && [ "$b" = 401 ] && [ "$c" = 401 ]; then ok 1.5 "me 200, no token 401, made-up token 401"
else bad 1.5 "expected me 200, no token 401, made-up token 401, got $a, $b, $c"; fi

a=$(call GET $N1/api/auth/me "" "$O_T"); b=$(call GET $N2/api/auth/me "" "$O_T")
if [ "$a" = 200 ] && [ "$b" = 200 ]; then ok 1.6 "token works on 8001 and 8002"
else bad 1.6 "token from 8000: me on 8001 got $a, on 8002 got $b (expected 200)"; fi

call POST $N0/api/auth/register "{\"username\":\"u${RUN}_dn\",\"password\":\"$PW\",\"display_name\":\"Dee Enn\"}" >/dev/null
dn=$(field user.display_name); f=$(py 'all(k in d["user"] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"])')
call GET $N1/api/auth/me "" "$O_T" >/dev/null; g=$(py 'all(k in d["user"] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"])')
a=$(call POST $N0/api/auth/login "{\"username\":\"nobody$RUN\",\"password\":\"$PW\"}")
if [ "$dn" = "Dee Enn" ] && [ "$f$g" = TrueTrue ] && [ "$a" = 401 ]; then ok 1.7 "display_name is kept, users have every UserObj field, unknown username login 401"
else bad 1.7 "display_name '$dn' (want Dee Enn), every UserObj field on register: '$f', on me: '$g' (want True), login of an unknown username: $a (want 401)"; fi

# more test users: A (becomes admin), M (member), G (becomes guest), Z, X, Y
for u in A M G Z X Y W V P Q E F; do new_user $u; done

# ---------- 2. workspaces ----------
WS1="ws-$RUN-a"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$WS1\",\"name\":\"Check A\"}" "$O_T")
GEN1=$(field general_channel.id)
if [ "$s" = 201 ] && [ "$(field workspace.owner_id)" = "$O_ID" ] && [ "$(field workspace.join_mode)" = open ] \
   && [ "$(field general_channel.name)" = general ] && [ -n "$(field workspace.id)" ]; then
  ok 2.1 "create workspace returns 201 with workspace and #general"
else bad 2.1 "create workspace: expected 201, owner_id=creator, join_mode=open, general_channel.name=general; got status $s"; fi

s=$(call POST $N0/api/workspaces "{\"slug\":\"$WS1\",\"name\":\"Again\"}" "$O_T")
expect 2.2 "duplicate slug" 409 "$s"

s=$(call POST $N0/api/workspaces "{\"slug\":\"Bad_Slug\",\"name\":\"Bad\"}" "$O_T")
expect 2.3 "invalid slug" 400 "$s"

call GET $N0/api/workspaces "" "$O_T" >/dev/null; a=$(list_has workspaces slug "$WS1")
call GET $N0/api/workspaces "" "$X_T" >/dev/null; b=$(list_has workspaces slug "$WS1")
if [ "$a" = yes ] && [ -z "$b" ]; then ok 2.4 "workspace list shows only the caller's workspaces"
else bad 2.4 "owner sees it: '${a:-no}', non-member sees it: '${b:-no}' (expected yes and no)"; fi

r=$(role_of "$WS1" "$O_T" "$O_ID")
if [ "$r" = owner ]; then ok 2.5 "members list shows the creator as owner"
else bad 2.5 "members: creator's role is '$r', expected owner"; fi

call GET $N0/api/workspaces "" "$O_T" >/dev/null
f=$(py '[sorted(k for k in ["id","slug","name","owner_id","join_mode"] if k in w) for w in d["workspaces"] if w.get("slug")=="'"$WS1"'"]==[["id","join_mode","name","owner_id","slug"]]')
a=$(call GET $N0/api/workspaces/nope-$RUN "" "$O_T"); b=$(call POST $N0/api/workspaces '{"slug":"a","name":"x"}' "$O_T")
if [ "$f" = True ] && [ "$a" = 404 ] && [ "$b" = 400 ]; then ok 2.6 "listed workspaces have every field, unknown slug 404, 1-char slug 400"
else bad 2.6 "listed workspace has id, slug, name, owner_id, join_mode: '$f' (want True), GET unknown slug: $a (want 404), slug 'a': $b (want 400)"; fi

# ---------- 3. joining channels ----------
s=$(call POST $N1/api/channels/$GEN1/join "{}" "$A_T"); r=$(role_of "$WS1" "$O_T" "$A_ID")
if [ "$s" = 200 ] && [ "$r" = member ]; then ok 3.1 "non-member joins #general of an open workspace and becomes member"
else bad 3.1 "join on open workspace: expected 200 and role member, got $s and '$r'"; fi
for u in M G Z; do t="${u}_T"; call POST $N0/api/channels/$GEN1/join "{}" "${!t}" >/dev/null; done

s=$(call POST $N0/api/channels/999999999/join "{}" "$A_T")
expect 3.2 "join unknown channel" 404 "$s"

call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"priv-$RUN\",\"is_private\":true}" "$O_T" >/dev/null
PRIV=$(field channel.id)
s=$(call POST $N0/api/channels/${PRIV:-0}/join "{}" "$M_T")
expect 3.3 "join a private channel you are not in" 403 "$s"

# ---------- 4. roles ----------
s=$(call PATCH $N0/api/workspaces/$WS1/members/$A_ID '{"role":"admin"}' "$O_T")
mr=$(field member.role); r=$(role_of "$WS1" "$O_T" "$A_ID")
if [ "$s" = 200 ] && [ "$mr" = admin ] && [ "$r" = admin ]; then ok 4.1 "owner promotes a member to admin"
else bad 4.1 "promote: expected 200, member.role=admin and admin in the members list; got $s, '$mr', '$r'"; fi

call PATCH $N0/api/workspaces/$WS1/members/$Z_ID '{"role":"admin"}' "$O_T" >/dev/null
a=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"guest"}' "$A_T")
b=$(call PATCH $N0/api/workspaces/$WS1/members/$Z_ID '{"role":"member"}' "$A_T")
if [ "$a" = 200 ] && [ "$b" = 403 ]; then ok 4.2 "admin can change a member, not another admin"
else bad 4.2 "admin sets member to guest: $a (want 200), admin changes another admin: $b (want 403)"; fi

a=$(call PATCH $N0/api/workspaces/$WS1/members/$O_ID '{"role":"member"}' "$A_T")
b=$(call PATCH $N0/api/workspaces/$WS1/members/$O_ID '{"role":"admin"}' "$O_T")
if [ "$a" = 403 ] && [ "$b" = 403 ]; then ok 4.3 "nobody can change the owner's role with PATCH"
else bad 4.3 "admin changes owner: $a, owner changes self: $b (expected 403 each)"; fi

s=$(call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"owner"}' "$O_T")
expect 4.4 "PATCH role to owner" 400 "$s"

s=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"member"}' "$M_T")
expect 4.5 "member changes someone's role" 403 "$s"

new_user NM
a=$(call PATCH $N0/api/workspaces/$WS1/members/$G_ID '{"role":"superuser"}' "$O_T")
b=$(call PATCH $N0/api/workspaces/$WS1/members/$NM_ID '{"role":"member"}' "$O_T")
c=$(call PATCH $N0/api/workspaces/$WS1/members/999999999 '{"role":"member"}' "$O_T")
if [ "$a$b$c" = 400404404 ]; then ok 4.6 "unknown role 400, a user who is not in the workspace 404"
else bad 4.6 "role 'superuser': $a (want 400), role of a registered user not in the workspace: $b, of user 999999999: $c (want 404 each)"; fi

a=$(call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"admin"}' "$A_T"); r=$(role_of "$WS1" "$O_T" "$M_ID")
call PATCH $N0/api/workspaces/$WS1/members/$M_ID '{"role":"member"}' "$O_T" >/dev/null
if [ "$a" = 200 ] && [ "$r" = admin ]; then ok 4.7 "admin promotes a member to admin"
else bad 4.7 "admin promotes a member: $a with role '$r' (want 200, admin)"; fi

# ---------- 5. channel permissions ----------
call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"pub-$RUN\"}" "$O_T" >/dev/null; PUB=$(field channel.id)
a=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"g-$RUN\"}" "$G_T")
b=$(call POST $N0/api/channels/${PUB:-0}/join "{}" "$G_T")
if [ "$a" = 403 ] && [ "$b" = 403 ]; then ok 5.1 "guest cannot create or join channels"
else bad 5.1 "guest create: $a, guest join public: $b (expected 403 each)"; fi

a=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"m-$RUN\"}" "$M_T")
b=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"mp-$RUN\",\"is_private\":true}" "$M_T")
if [ "$a" = 201 ] && [ "$b" = 403 ]; then ok 5.2 "member can create public, not private"
else bad 5.2 "member public: $a (want 201), member private: $b (want 403)"; fi

s=$(call POST $N0/api/workspaces/$WS1/channels "{\"name\":\"ap-$RUN\",\"is_private\":true}" "$A_T")
expect 5.3 "admin creates a private channel" 201 "$s"

# ---------- 6. join mode ----------
WS2="ws-$RUN-b"
call POST $N0/api/workspaces "{\"slug\":\"$WS2\",\"name\":\"Check B\"}" "$O_T" >/dev/null; GEN2=$(field general_channel.id)
a=$(call PATCH $N0/api/workspaces/$WS2 '{"join_mode":"invite_only"}' "$O_T"); jm=$(field workspace.join_mode)
b=$(call PATCH $N0/api/workspaces/$WS1 '{"name":"By Member"}' "$M_T")
if [ "$a" = 200 ] && [ "$jm" = invite_only ] && [ "$b" = 403 ]; then ok 6.1 "owner sets invite_only, member cannot PATCH"
else bad 6.1 "owner PATCH: $a with join_mode '$jm' (want 200, invite_only), member PATCH: $b (want 403)"; fi

s=$(call POST $N0/api/channels/${GEN2:-0}/join "{}" "$X_T")
expect 6.2 "non-member joins a channel in an invite_only workspace" 403 "$s"

s=$(call PATCH $N0/api/workspaces/$WS2 '{"join_mode":"sometimes"}' "$O_T")
expect 6.3 "invalid join_mode" 400 "$s"

a=$(call PATCH $N0/api/workspaces/$WS1 '{"name":"By Owner"}' "$O_T"); n1=$(field workspace.name)
b=$(call PATCH $N1/api/workspaces/$WS1 '{"name":"By Admin"}' "$A_T"); n2=$(field workspace.name)
if [ "$a$b" = 200200 ] && [ "$n1|$n2" = "By Owner|By Admin" ]; then ok 6.4 "owner and admin rename the workspace"
else bad 6.4 "owner rename: $a with name '$n1', admin rename: $b with name '$n2' (want 200 and the new name each)"; fi

# ---------- 7. invitations ----------
a=$(call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T"); CODE1=$(field invitation.code)
b=$(call POST $N0/api/workspaces/$WS1/invitations "{}" "$M_T")
if [ "$a" = 201 ] && [ -n "$CODE1" ] && [ "$b" = 403 ]; then ok 7.1 "owner creates an invitation with a code, member cannot"
else bad 7.1 "owner create: $a with code '${CODE1}' (want 201), member create: $b (want 403)"; fi

a=$(call POST $N0/api/invitations/${CODE1:-none}/accept "{}" "$X_T"); r=$(role_of "$WS2" "$O_T" "$X_ID")
b=$(call POST $N0/api/channels/${GEN2:-0}/join "{}" "$X_T")
if [ "$a" = 200 ] && [ "$r" = member ] && [ "$b" = 200 ]; then ok 7.2 "accept makes the user a member who can join channels"
else bad 7.2 "accept: $a (want 200), role '$r' (want member), join after accept: $b (want 200)"; fi

a=$(call POST $N0/api/invitations/${CODE1:-none}/accept "{}" "$Y_T")
b=$(call POST $N0/api/invitations/nosuchcode$RUN/accept "{}" "$Y_T")
if [ "$a" = 404 ] && [ "$b" = 404 ]; then ok 7.3 "used-up and unknown codes return 404"
else bad 7.3 "second accept of a 1-use code: $a, unknown code: $b (expected 404 each)"; fi

call POST $N0/api/workspaces/$WS2/invitations "{\"invited_user_id\":$(jv "$Z_ID")}" "$O_T" >/dev/null; C=$(field invitation.code)
a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Z_T")
if [ "$a" = 403 ] && [ "$b" = 200 ]; then ok 7.4 "targeted invitation: other user 403, invited user 200"
else bad 7.4 "targeted invitation: other user got $a (want 403), invited user got $b (want 200)"; fi

call POST $N0/api/workspaces/$WS2/invitations '{"expires_in_seconds":1}' "$O_T" >/dev/null; C=$(field invitation.code)
sleep 2
s=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T")
expect 7.5 "expired invitation" 404 "$s"

call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; LIVE=$(field invitation.code)
a=$(call GET $N0/api/workspaces/$WS2/invitations "" "$O_T"); h=$(list_has invitations code "$LIVE")
b=$(call GET $N0/api/workspaces/$WS2/invitations "" "$X_T")
if [ "$a" = 200 ] && [ "$h" = yes ] && [ "$b" = 403 ]; then ok 7.6 "owner lists invitations, member cannot"
else bad 7.6 "owner list: $a, includes new code: '${h:-no}' (want 200, yes), member list: $b (want 403)"; fi

a=$(call DELETE $N0/api/workspaces/$WS2/invitations/${LIVE:-none} "" "$O_T"); d=$(field deleted)
b=$(call POST $N0/api/invitations/${LIVE:-none}/accept "{}" "$Y_T")
if [ "$a" = 200 ] && [ "$d" = True ] && [ "$b" = 404 ]; then ok 7.7 "revoke returns deleted:true and the code stops working"
else bad 7.7 "revoke: $a with deleted='$d' (want 200, true), accept after revoke: $b (want 404)"; fi

call POST $N0/api/workspaces/$WS2/invitations '{"max_uses":2}' "$O_T" >/dev/null; C=$(field invitation.code)
a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$W_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$V_T")
c=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$P_T")
if [ "$a$b$c" = 200200404 ]; then ok 7.8 "max_uses=2 lets two users in, not a third"
else bad 7.8 "max_uses=2: first $a, second $b (want 200 each), third $c (want 404)"; fi

call POST $N0/api/workspaces/$WS2/invitations "{\"invited_username\":\"u${RUN}_Q\"}" "$O_T" >/dev/null; C=$(field invitation.code)
a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Y_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$Q_T")
if [ "$a" = 403 ] && [ "$b" = 200 ]; then ok 7.9 "invited_username: other user 403, invited user 200"
else bad 7.9 "invited_username invitation: other user got $a (want 403), invited user got $b (want 200)"; fi

s=$(call POST $N0/api/workspaces/$WS1/invitations "{}" "$A_T")
expect 7.10 "admin creates an invitation" 201 "$s"

call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; C=$(field invitation.code)
call POST $N0/api/invitations/${C:-none}/accept "{}" "$E_T" >/dev/null
s=$(call GET $N0/api/workspaces/$WS2/invitations "" "$O_T"); h=$(list_has invitations code "$C")
if [ "$s" = 200 ] && [ -n "$C" ] && [ -z "$h" ]; then ok 7.11 "used-up invitations are not listed"
else bad 7.11 "list: $s; a code whose only use is taken is still listed: '${h:-no}' (want 200 and no)"; fi

call POST $N0/api/workspaces/$WS2/invitations "{\"invited_user_id\":$(jv "$F_ID"),\"max_uses\":5}" "$O_T" >/dev/null; C=$(field invitation.code)
a=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$F_T"); b=$(call POST $N0/api/invitations/${C:-none}/accept "{}" "$F_T")
if [ "$a" = 200 ] && [ "$b" = 404 ]; then ok 7.12 "targeted invitations are single-use, even with max_uses 5"
else bad 7.12 "targeted invitation with max_uses 5: first accept $a (want 200), second accept $b (want 404)"; fi

call POST $N0/api/workspaces/$WS2/invitations "{}" "$O_T" >/dev/null; C=$(field invitation.code)
a=$(call DELETE $N0/api/workspaces/$WS2/invitations/${C:-none} "" "$E_T")
b=$(call DELETE $N0/api/workspaces/$WS2/invitations/nosuchcode$RUN "" "$O_T")
c=$(call POST $N0/api/invitations/${C:-none}/accept "{}")
if [ "$a$b$c" = 403404401 ]; then ok 7.13 "member cannot revoke, unknown code 404, accept needs a token"
else bad 7.13 "member revokes: $a (want 403), owner revokes an unknown code: $b (want 404), accept with no token: $c (want 401)"; fi

# ---------- 8. ownership transfer ----------
s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$M_ID")}" "$O_T")
expect 8.1 "transfer to a member" 400 "$s"

s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$A_T")
expect 8.2 "admin calls transfer" 403 "$s"

s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$O_T")
ra=$(role_of "$WS1" "$A_T" "$A_ID"); ro=$(role_of "$WS1" "$A_T" "$O_ID")
if [ "$s" = 200 ] && [ "$ra" = owner ] && [ "$ro" = admin ]; then ok 8.3 "transfer to an admin swaps owner and admin"
else bad 8.3 "transfer: $s (want 200), new owner role '$ra' (want owner), old owner role '$ro' (want admin)"; fi

call GET $N0/api/workspaces "" "$A_T" >/dev/null; o1=$(py '[str(w.get("owner_id")) for w in d["workspaces"] if w.get("slug")=="'"$WS1"'"]')
call GET $N1/api/workspaces/$WS1 "" "$A_T" >/dev/null; o2=$(py 'str(d["workspace"]["owner_id"])')
a=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$Z_ID")}" "$O_T")
if [ "$o1" = "['$A_ID']" ] && [ "$o2" = "$A_ID" ] && [ "$a" = 403 ]; then ok 8.4 "owner_id follows the transfer, the old owner cannot transfer again"
else bad 8.4 "after the transfer, owner_id in GET /api/workspaces: $o1, in GET /api/workspaces/{slug}: '$o2' (want $A_ID), old owner transfers again: $a (want 403)"; fi

# ---------- 9. auth everywhere ----------
a=$(call GET $N0/api/workspaces); b=$(call POST $N0/api/workspaces "{}")
c=$(call GET $N0/api/workspaces/$WS1/members); d=$(call POST $N0/api/workspaces/$WS1/invitations "{}")
if [ "$a$b$c$d" = 401401401401 ]; then ok 9.1 "routes return 401 without a token"
else bad 9.1 "no token: list $a, create $b, members $c, invite $d (expected 401 each)"; fi

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
