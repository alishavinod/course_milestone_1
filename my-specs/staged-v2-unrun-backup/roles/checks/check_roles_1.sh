#!/usr/bin/env bash
# Self-check for Roles.1 (baseline and auth). Prints PASS/FAIL per rule in /spec/roles/roles-1.md.
# Usage: bash /spec/roles/checks/check_roles_1.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_roles_1() {
  local s a b c t i bad_nodes=""
  for i in 0 1 2; do
    s=$(call GET "http://127.0.0.1:800$i/api/health")
    [ "$s" = 200 ] && [ "$(field status)" = ok ] && [ "$(field node_id)" = "$i" ] || bad_nodes="$bad_nodes 800$i"
  done
  if [ -z "$bad_nodes" ]; then ok 0.1 "health returns 200 with the right node_id on all nodes"
  else bad 0.1 "health: wrong status or node_id on port(s)$bad_nodes"; fi

  s=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$N0/api/ws?token=bogus$RUN" 2>/dev/null)
  if [ "$s" != 101 ]; then ok 0.2 "websocket with a made-up token is rejected"
  else bad 0.2 "websocket with a made-up token was accepted (101)"; fi

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
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_roles_1; finish; }
