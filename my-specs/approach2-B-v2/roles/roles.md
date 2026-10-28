# Spec A: roles and invitations

Check: `bash /spec/roles/checks/check_roles.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`.

## Tips

- Run each node as its own process. `start.sh` should restart a node if it dies.
- Keep all data in one SQLite file (for example `/app/data/app.db`) so all three nodes see the
  same users, tokens and workspaces.
- Never return `422`. Bad input is `400`. Check the token first: no valid token is `401`.
- Roles are per workspace: `owner`, `admin`, `member`, `guest`.

## 0. Baseline (milestone 0)

0.1 **Health.** `GET /api/health` on ports `8000`, `8001`, `8002` returns `200` with
    `{"status": "ok", "node_id": N}` where `N` is `0`, `1`, `2`.

0.2 **Bad WebSocket token.** A WebSocket connection to `/api/ws?token=<made-up>` is rejected
    (the server does not answer `101`).

## 1. Auth

1.1 **Register.** `POST /api/auth/register` with `{username, password}` returns `201` with
    `{user, token}`. `token` is at least 16 characters.

1.2 **Duplicate username.** Registering the same username again returns `409`.

1.3 **Bad input.** A username with a space returns `400`. A 7-character password returns `400`.

1.4 **Login.** `POST /api/auth/login` returns `200` with `{user, token}`. A wrong password
    returns `401`.

1.5 **Me.** `GET /api/auth/me` with a valid token returns `200` with `{user}`. No token, or a
    made-up token, returns `401`.

1.6 **Every node.** A token from port `8000` works for `GET /api/auth/me` on `8001` and `8002`.

## 2. Workspaces

2.1 **Create.** `POST /api/workspaces` with `{slug, name}` returns `201` with
    `{workspace, general_channel}`. `workspace` has `id, slug, name, owner_id, join_mode`.
    `owner_id` is the creator, `join_mode` is `"open"`, and `general_channel.name` is
    `"general"`. The creator's role is `owner`.

2.2 **Duplicate slug.** The same slug again returns `409`.

2.3 **Bad slug.** The slug must match `^[a-z0-9-]{2,32}$`. `Bad_Slug` returns `400`.

2.4 **List.** `GET /api/workspaces` returns `200` with `{workspaces: [...]}`, only the ones the
    caller is a member of.

2.5 **Members.** `GET /api/workspaces/{slug}/members` returns `200` with
    `{members: [{user_id, role}, ...]}`. The creator has role `owner`.

## 3. Joining channels

3.1 **Open workspace.** When `join_mode` is `"open"`, a user who is not a member can call
    `POST /api/channels/{id}/join` on a public channel and gets `200`. They become a workspace
    `member`.

3.2 **Unknown channel.** Joining channel `999999999` returns `404`.

3.3 **Private channel.** Joining a private channel you are not in returns `403`.

## 4. Roles

Change a role with `PATCH /api/workspaces/{slug}/members/{user_id}` and body `{role}`.

4.1 **Promote.** The owner sets a member to `admin` and gets `200` with
    `{member: {user_id, role: "admin"}}`. The members list now shows `admin`.

4.2 **Admin changes a member.** An admin can set a member to `guest` (`200`), but cannot change
    another admin (`403`).

4.3 **Owner is protected.** An admin changing the owner's role gets `403`. The owner changing
    their own role also gets `403`.

4.4 **No PATCH to owner.** Setting `role` to `"owner"` returns `400`. Use `transfer_ownership`.

4.5 **Members cannot change roles.** A `member` changing someone's role gets `403`.

## 5. Channel permissions

Create a channel with `POST /api/workspaces/{slug}/channels` and body `{name, is_private?}`.
It returns `201 {channel}`.

5.1 **Guests.** A `guest` cannot create a channel (`403`) and cannot join a public channel
    (`403`).

5.2 **Members.** A `member` can create a public channel (`201`) but not a private one (`403`).

5.3 **Admins.** An `admin` can create a private channel (`201`).

## 6. Join mode

`PATCH /api/workspaces/{slug}` takes `{name?, join_mode?}` and returns `200 {workspace}`.

6.1 **Change join mode.** The owner sets `join_mode` to `"invite_only"` and gets `200`. A
    `member` trying the same PATCH gets `403`.

6.2 **Invite-only.** In an `"invite_only"` workspace, a non-member joining a channel gets `403`.

6.3 **Bad join mode.** `join_mode` of `"sometimes"` returns `400`.

## 7. Invitations

7.1 **Create.** `POST /api/workspaces/{slug}/invitations` by the owner returns `201` with
    `{invitation}` that has a `code`. A `member` gets `403`.

7.2 **Accept.** `POST /api/invitations/{code}/accept` returns `200`. The user becomes a
    `member` and can now join channels in the invite-only workspace.

7.3 **Used up or unknown.** `max_uses` defaults to `1`, so a second user accepting the same
    code gets `404`. An unknown code returns `404`.

7.4 **Targeted.** For an invitation with `{invited_user_id}`, any other user gets `403`. The
    invited user gets `200`.

7.5 **Expired.** An invitation with `{expires_in_seconds: 1}`, accepted 2 seconds later,
    returns `404`.

7.6 **List.** `GET /api/workspaces/{slug}/invitations` by the owner returns `200` with
    `{invitations: [...]}` including active codes. A `member` gets `403`.

7.7 **Revoke.** `DELETE /api/workspaces/{slug}/invitations/{code}` by the owner returns `200`
    with `{"deleted": true}`. Accepting that code afterwards returns `404`.

## 8. Ownership transfer

`POST /api/workspaces/{slug}/transfer_ownership` with body `{new_owner_id}`. Owner only.

8.1 **Must be an admin.** Transferring to a `member` returns `400`.

8.2 **Owner only.** An `admin` calling it gets `403`.

8.3 **Transfer.** Transferring to an `admin` returns `200`. The target becomes `owner` and the
    old owner becomes `admin`.

## 9. Auth everywhere

9.1 **No token.** `GET /api/workspaces`, `POST /api/workspaces`,
    `GET /api/workspaces/{slug}/members` and `POST /api/workspaces/{slug}/invitations` return
    `401` with no token.

Every request must answer within 5 seconds.
