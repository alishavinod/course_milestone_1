import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

DB_PATH = os.environ.get("HUDDLE_DB", "/app/data/app.db")
NODE_ID = int(os.environ.get("NODE_ID", "0"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db():
    db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL, display_name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC',
      avatar_url TEXT NOT NULL DEFAULT '', status_text TEXT NOT NULL DEFAULT '',
      status_emoji TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS tokens (
      token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspaces (
      id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      owner_id INTEGER NOT NULL, join_mode TEXT NOT NULL DEFAULT 'open'
    );
    CREATE TABLE IF NOT EXISTS workspace_members (
      workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL,
      PRIMARY KEY(workspace_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS channels (
      id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, name TEXT NOT NULL,
      is_private INTEGER NOT NULL DEFAULT 0, is_dm INTEGER NOT NULL DEFAULT 0,
      topic TEXT NOT NULL DEFAULT '', is_archived INTEGER NOT NULL DEFAULT 0,
      UNIQUE(workspace_id,name)
    );
    CREATE TABLE IF NOT EXISTS channel_members (
      channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      PRIMARY KEY(channel_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS invitations (
      code TEXT PRIMARY KEY, workspace_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
      invited_user_id INTEGER, expires_at REAL, max_uses INTEGER NOT NULL DEFAULT 1,
      uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL
    );
    """)
    db.commit()
    db.close()

def hash_password(password):
    salt = secrets.token_hex(16)
    return salt + "$" + hashlib.sha256((salt + password).encode()).hexdigest()

def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        return secrets.compare_digest(digest, hashlib.sha256((salt + password).encode()).hexdigest())
    except Exception:
        return False

def create_user(username, password, display_name=None):
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ValueError("invalid username")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("invalid password")
    db = get_db()
    try:
        name = display_name if isinstance(display_name, str) and display_name else username
        cur = db.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",
                         (username, hash_password(password), name))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return row
    except sqlite3.IntegrityError:
        raise FileExistsError("username exists")
    finally:
        db.close()

def issue_token(user_id):
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute("INSERT INTO tokens(token,user_id,created_at) VALUES(?,?,?)",
               (token, user_id, datetime.now(timezone.utc).isoformat()))
    db.commit()
    db.close()
    return token

def user_to_json(user):
    if not user:
        return None
    return {k: user[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}

def require_user(request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "authentication required")
    token = auth[7:].strip()
    db = get_db()
    row = db.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?", (token,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(401, "invalid token")
    return row


def get_workspace(slug):
    db = get_db()
    row = db.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "workspace not found")
    return row

def get_role(workspace_id, user_id):
    db = get_db()
    row = db.execute("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                     (workspace_id, user_id)).fetchone()
    db.close()
    return row["role"] if row else None

def add_member(workspace_id, user_id, role):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",
               (workspace_id, user_id, role))
    db.commit()
    db.close()

def workspace_to_json(workspace):
    return {k: workspace[k] for k in ("id","slug","name","owner_id","join_mode")}

def create_channel(workspace_id, name, is_private, creator_id):
    db = get_db()
    try:
        cur = db.execute("""INSERT INTO channels(workspace_id,name,is_private)
                            VALUES(?,?,?)""", (workspace_id, name, 1 if is_private else 0))
        cid = cur.lastrowid
        db.execute("INSERT INTO channel_members(channel_id,user_id) VALUES(?,?)", (cid, creator_id))
        db.commit()
        return db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    except sqlite3.IntegrityError:
        raise FileExistsError("channel exists")
    finally:
        db.close()

def channel_to_json(channel):
    return {k: (bool(channel[k]) if k in ("is_private","is_dm","is_archived") else channel[k])
            for k in ("id","workspace_id","name","is_private","is_dm","topic","is_archived")}

def join_channel(channel, user):
    workspace_id = channel["workspace_id"]
    role = get_role(workspace_id, user["id"])
    db = get_db()
    already_member = db.execute(
        "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
        (channel["id"], user["id"])
    ).fetchone()
    db.close()
    if not can_join_channel(role):
        raise HTTPException(403, "guest cannot join")
    if channel["is_private"] and not already_member:
        raise HTTPException(403, "private channel")
    if not role:
        ws = get_workspace_by_id(workspace_id)
        if ws["join_mode"] != "open":
            raise HTTPException(403, "workspace invitation required")
        add_member(workspace_id, user["id"], "member")
    db = get_db()
    db.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES(?,?)",
               (channel["id"], user["id"]))
    db.commit()
    row = db.execute("SELECT * FROM channels WHERE id=?", (channel["id"],)).fetchone()
    db.close()
    return row

def get_workspace_by_id(workspace_id):
    db = get_db()
    row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "workspace not found")
    return row

app = FastAPI()


def set_role(workspace, actor_id, target_id, new_role):
    if new_role == "owner":
        raise HTTPException(400, "use transfer ownership")
    if new_role not in ("admin", "member", "guest"):
        raise HTTPException(400, "invalid role")
    actor = get_role(workspace["id"], actor_id)
    target = get_role(workspace["id"], target_id)
    if not actor or not target:
        raise HTTPException(403, "not permitted")
    if target == "owner" or target_id == actor_id:
        raise HTTPException(403, "owner protected")
    if actor not in ("owner", "admin"):
        raise HTTPException(403, "insufficient role")
    if actor == "admin" and target == "admin":
        raise HTTPException(403, "cannot modify admin")
    db = get_db()
    db.execute("UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
               (new_role, workspace["id"], target_id))
    db.commit()
    row = db.execute("SELECT user_id,role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                     (workspace["id"], target_id)).fetchone()
    db.close()
    return row

def can_create_channel(role, is_private):
    if role in ("owner", "admin"):
        return True
    return role == "member" and not is_private

def can_join_channel(role):
    return role != "guest"

class RoleBody(BaseModel):
    role: str

@app.patch("/api/workspaces/{slug}/members/{user_id}")
async def change_member_role(slug: str, user_id: int, request: Request):
    actor = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("role"), str):
        raise HTTPException(400, "invalid role")
    ws = get_workspace(slug)
    row = set_role(ws, actor["id"], user_id, data["role"])
    return {"member": dict(row)}


def update_workspace(workspace, fields):
    name = fields.get("name", workspace["name"])
    join_mode = fields.get("join_mode", workspace["join_mode"])
    if join_mode not in ("open", "invite_only"):
        raise HTTPException(400, "invalid join mode")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "invalid name")
    db = get_db()
    db.execute("UPDATE workspaces SET name=?,join_mode=? WHERE id=?",
               (name, join_mode, workspace["id"]))
    db.commit()
    row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace["id"],)).fetchone()
    db.close()
    return row

def create_invitation(workspace, creator_id, expires_in_seconds=0, max_uses=1, invited_user_id=None):
    import time
    try:
        max_uses = int(max_uses)
        expires_in_seconds = int(expires_in_seconds or 0)
    except Exception:
        raise HTTPException(400, "invalid invitation parameters")
    if max_uses < 1 or expires_in_seconds < 0:
        raise HTTPException(400, "invalid invitation parameters")
    code = secrets.token_urlsafe(18)
    now = time.time()
    expires = now + expires_in_seconds if expires_in_seconds else None
    db = get_db()
    db.execute("""INSERT INTO invitations
      (code,workspace_id,creator_id,invited_user_id,expires_at,max_uses,created_at)
      VALUES(?,?,?,?,?,?,?)""",
      (code, workspace["id"], creator_id, invited_user_id, expires, max_uses, now))
    db.commit()
    row = db.execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
    db.close()
    return row

def invitation_to_json(row):
    import datetime
    result = {k: row[k] for k in ("code","workspace_id","creator_id","invited_user_id","max_uses","uses")}
    result["expires_at"] = (datetime.datetime.fromtimestamp(row["expires_at"], datetime.timezone.utc).isoformat()
                            if row["expires_at"] else None)
    return result

def accept_invitation(code, user):
    import time
    db = get_db()
    row = db.execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "invitation not found")
    if row["invited_user_id"] is not None and row["invited_user_id"] != user["id"]:
        db.close()
        raise HTTPException(403, "invitation targeted to another user")
    if row["revoked"] or row["uses"] >= row["max_uses"] or (row["expires_at"] and row["expires_at"] <= time.time()):
        db.close()
        raise HTTPException(404, "invitation unavailable")
    cur = db.execute("""UPDATE invitations SET uses=uses+1
                        WHERE code=? AND revoked=0 AND uses<max_uses
                        AND (expires_at IS NULL OR expires_at>?)""", (code, time.time()))
    if cur.rowcount != 1:
        db.close()
        raise HTTPException(404, "invitation unavailable")
    db.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",
               (row["workspace_id"], user["id"], "member"))
    db.commit()
    db.close()
    return get_workspace_by_id(row["workspace_id"])

def list_invitations(workspace):
    import time
    db = get_db()
    rows = db.execute("""SELECT * FROM invitations WHERE workspace_id=? AND revoked=0
                         AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)
                         ORDER BY created_at DESC""", (workspace["id"], time.time())).fetchall()
    db.close()
    return rows

def revoke_invitation(workspace, code):
    db = get_db()
    cur = db.execute("UPDATE invitations SET revoked=1 WHERE workspace_id=? AND code=?",
                     (workspace["id"], code))
    db.commit()
    db.close()
    return True

def transfer_ownership(workspace, actor_id, new_owner_id):
    if get_role(workspace["id"], actor_id) != "owner":
        raise HTTPException(403, "owner only")
    if get_role(workspace["id"], new_owner_id) != "admin":
        raise HTTPException(400, "target must be admin")
    db = get_db()
    db.execute("UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?",
               (workspace["id"], actor_id))
    db.execute("UPDATE workspace_members SET role='owner' WHERE workspace_id=? AND user_id=?",
               (workspace["id"], new_owner_id))
    db.execute("UPDATE workspaces SET owner_id=? WHERE id=?", (new_owner_id, workspace["id"]))
    db.commit()
    row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace["id"],)).fetchone()
    db.close()
    return row

class WorkspacePatchBody(BaseModel):
    name: str | None = None
    join_mode: str | None = None

class InvitationBody(BaseModel):
    expires_in_seconds: int | None = None
    max_uses: int | None = None
    invited_username: str | None = None
    invited_user_id: int | None = None

class TransferBody(BaseModel):
    new_owner_id: int

@app.patch("/api/workspaces/{slug}")
async def patch_workspace(slug: str, request: Request):
    user = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid body")
    allowed = {"name", "join_mode"}
    if any(k not in allowed for k in data):
        raise HTTPException(400, "invalid body")
    ws = get_workspace(slug)
    if get_role(ws["id"], user["id"]) not in ("owner", "admin"):
        raise HTTPException(403, "insufficient role")
    return {"workspace": workspace_to_json(update_workspace(ws, data))}

@app.post("/api/workspaces/{slug}/invitations", status_code=201)
async def make_invitation(slug: str, request: Request):
    user = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid invitation")
    ws = get_workspace(slug)
    if get_role(ws["id"], user["id"]) not in ("owner", "admin"):
        raise HTTPException(403, "insufficient role")
    target = data.get("invited_user_id")
    invited_username = data.get("invited_username")
    if invited_username is not None:
        if not isinstance(invited_username, str):
            raise HTTPException(400, "invalid invited username")
        db = get_db()
        found = db.execute("SELECT id FROM users WHERE username=?", (invited_username,)).fetchone()
        db.close()
        if not found:
            raise HTTPException(400, "unknown invited user")
        target = found["id"]
    row = create_invitation(ws, user["id"], data.get("expires_in_seconds", 0),
                            data.get("max_uses", 1), target)
    return {"invitation": invitation_to_json(row)}

@app.post("/api/invitations/{code}/accept")
async def accept_invite(code: str, request: Request):
    user = require_user(request)
    ws = accept_invitation(code, user)
    return {"workspace": workspace_to_json(ws)}

@app.get("/api/workspaces/{slug}/invitations")
async def get_invitations(slug: str, request: Request):
    user = require_user(request)
    ws = get_workspace(slug)
    if get_role(ws["id"], user["id"]) not in ("owner", "admin"):
        raise HTTPException(403, "insufficient role")
    return {"invitations": [invitation_to_json(x) for x in list_invitations(ws)]}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
async def delete_invitation(slug: str, code: str, request: Request):
    user = require_user(request)
    ws = get_workspace(slug)
    if get_role(ws["id"], user["id"]) not in ("owner", "admin"):
        raise HTTPException(403, "insufficient role")
    revoke_invitation(ws, code)
    return {"deleted": True}

@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug: str, request: Request):
    user = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("new_owner_id"), int):
        raise HTTPException(400, "invalid owner")
    ws = get_workspace(slug)
    return {"workspace": workspace_to_json(transfer_ownership(ws, user["id"], data["new_owner_id"]))}

class WorkspaceBody(BaseModel):
    slug: str
    name: str

class ChannelBody(BaseModel):
    name: str
    is_private: bool = False
    topic: str = ""

@app.post("/api/workspaces", status_code=201)
async def create_workspace(request: Request):
    user = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid body")
    slug = data.get("slug")
    name = data.get("name")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]{2,32}", slug):
        raise HTTPException(400, "invalid slug")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "invalid name")
    db = get_db()
    try:
        cur = db.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",
                         (slug, name, user["id"]))
        wid = cur.lastrowid
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",
                   (wid, user["id"], "owner"))
        db.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)", (wid, "general"))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO channel_members(channel_id,user_id) VALUES(?,?)", (cid,user["id"]))
        db.commit()
        ws = db.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        ch = db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(409, "workspace already exists")
    finally:
        db.close()
    return {"workspace": workspace_to_json(ws), "general_channel": channel_to_json(ch)}

@app.get("/api/workspaces")
async def list_workspaces(request: Request):
    user = require_user(request)
    db = get_db()
    rows = db.execute("""SELECT w.* FROM workspaces w JOIN workspace_members m
                         ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id""",
                      (user["id"],)).fetchall()
    db.close()
    return {"workspaces": [workspace_to_json(x) for x in rows]}

@app.get("/api/workspaces/{slug}/members")
async def workspace_members(slug: str, request: Request):
    user = require_user(request)
    ws = get_workspace(slug)
    if not get_role(ws["id"], user["id"]):
        raise HTTPException(403, "not a member")
    db = get_db()
    rows = db.execute("SELECT user_id,role FROM workspace_members WHERE workspace_id=? ORDER BY user_id",
                      (ws["id"],)).fetchall()
    db.close()
    return {"members": [dict(x) for x in rows]}

@app.post("/api/workspaces/{slug}/channels", status_code=201)
async def add_channel(slug: str, request: Request):
    user = require_user(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise HTTPException(400, "invalid channel")
    name = data["name"]
    is_private = data.get("is_private", False)
    topic = data.get("topic", "")
    if not isinstance(is_private, bool) or not isinstance(topic, str):
        raise HTTPException(400, "invalid channel")
    ws = get_workspace(slug)
    role = get_role(ws["id"], user["id"])
    if not can_create_channel(role, is_private):
        raise HTTPException(403, "insufficient role")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", name) or len(topic) > 250:
        raise HTTPException(400, "invalid channel")
    try:
        ch = create_channel(ws["id"], name, is_private, user["id"])
    except FileExistsError:
        raise HTTPException(409, "channel exists")
    return {"channel": channel_to_json(ch)}

@app.post("/api/channels/{channel_id}/join")
async def join(channel_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    ch = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    db.close()
    if not ch:
        raise HTTPException(404, "channel not found")
    return {"channel": channel_to_json(join_channel(ch, user))}

class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str | None = None

class LoginBody(BaseModel):
    username: str
    password: str


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"error": "invalid request"}, status_code=400)

@app.exception_handler(Exception)
async def generic_error(request, exc):
    return JSONResponse({"error": "invalid request"}, status_code=400)

@app.get("/api/health")
async def health():
    return {"status": "ok", "node_id": NODE_ID}

@app.post("/api/auth/register", status_code=201)
async def register(body: RegisterBody):
    try:
        user = create_user(body.username, body.password, body.display_name)
    except FileExistsError:
        raise HTTPException(409, "username already exists")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"user": user_to_json(user), "token": issue_token(user["id"])}

@app.post("/api/auth/login")
async def login(body: LoginBody):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    db.close()
    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(401, "invalid credentials")
    return {"user": user_to_json(user), "token": issue_token(user["id"])}

@app.get("/api/auth/me")
async def me(request: Request):
    return {"user": user_to_json(require_user(request))}

@app.websocket("/api/ws")
async def websocket(ws: WebSocket):
    token = ws.query_params.get("token", "")
    db = get_db()
    user = db.execute("SELECT u.id FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?", (token,)).fetchone()
    db.close()
    if not user:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await ws.send_json({"type": "connected"})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass

@app.on_event("startup")
async def startup():
    init_db()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
