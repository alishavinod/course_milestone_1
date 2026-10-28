#!/usr/bin/env bash
# Self-check for Roles.4 (join mode, invitations, transfer). Prints PASS/FAIL per rule in /spec/roles/roles-4.md.
# Usage: bash /spec/roles/checks/check_roles_4.sh      Exit 0 only if every rule passes.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

rules_roles_4() {
  local s a b c d r h jm ra ro C CODE1 LIVE WS2 GEN2
  # setup from Roles.2 and Roles.3: workspace WS1 owned by O, A is an admin, M is a member
  need_ws1
  [ "${A_ADMIN:-}" = 1 ] || call PATCH $N0/api/workspaces/$WS1/members/$A_ID '{"role":"admin"}' "$O_T" >/dev/null

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

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$M_ID")}" "$O_T")
  expect 8.1 "transfer to a member" 400 "$s"

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$A_T")
  expect 8.2 "admin calls transfer" 403 "$s"

  s=$(call POST $N0/api/workspaces/$WS1/transfer_ownership "{\"new_owner_id\":$(jv "$A_ID")}" "$O_T")
  ra=$(role_of "$WS1" "$A_T" "$A_ID"); ro=$(role_of "$WS1" "$A_T" "$O_ID")
  if [ "$s" = 200 ] && [ "$ra" = owner ] && [ "$ro" = admin ]; then ok 8.3 "transfer to an admin swaps owner and admin"
  else bad 8.3 "transfer: $s (want 200), new owner role '$ra' (want owner), old owner role '$ro' (want admin)"; fi

  a=$(call GET $N0/api/workspaces); b=$(call POST $N0/api/workspaces "{}")
  c=$(call GET $N0/api/workspaces/$WS1/members); d=$(call POST $N0/api/workspaces/$WS1/invitations "{}")
  if [ "$a$b$c$d" = 401401401401 ]; then ok 9.1 "routes return 401 without a token"
  else bad 9.1 "no token: list $a, create $b, members $c, invite $d (expected 401 each)"; fi
}

[ "${CHECK_ALL:-}" = 1 ] || { boot; rules_roles_4; finish; }
