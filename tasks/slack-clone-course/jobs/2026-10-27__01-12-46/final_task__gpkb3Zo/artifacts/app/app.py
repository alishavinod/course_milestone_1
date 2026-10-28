import os, re, sqlite3, hashlib, secrets, json
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = "/app/data/app.db"
NODE_ID = int(os.environ.get("NODE_ID", "0"))
os.makedirs("/app/data", exist_ok=True)

def get_db():
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db

def init_db():
    db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
      timezone TEXT NOT NULL DEFAULT 'UTC', avatar_url TEXT NOT NULL DEFAULT '',
      status_text TEXT NOT NULL DEFAULT '', status_emoji TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS tokens (
      token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspaces (
      id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL, owner_id INTEGER NOT NULL, join_mode TEXT NOT NULL DEFAULT 'open'
    );
    CREATE TABLE IF NOT EXISTS workspace_members (
      workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL,
      PRIMARY KEY(workspace_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS channels (
      id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL,
      name TEXT NOT NULL, is_private INTEGER NOT NULL DEFAULT 0,
      is_dm INTEGER NOT NULL DEFAULT 0, topic TEXT NOT NULL DEFAULT '',
      is_archived INTEGER NOT NULL DEFAULT 0,
      UNIQUE(workspace_id,name)
    );
    CREATE TABLE IF NOT EXISTS channel_members (
      channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      PRIMARY KEY(channel_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL,
      author_id INTEGER NOT NULL, body TEXT NOT NULL, parent_id INTEGER,
      created_at TEXT NOT NULL, edited_at TEXT, deleted INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pins (
      message_id INTEGER PRIMARY KEY, pinned_by INTEGER NOT NULL, pinned_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
      channel_id INTEGER NOT NULL, event_id INTEGER NOT NULL, type TEXT NOT NULL,
      payload TEXT NOT NULL, PRIMARY KEY(channel_id,event_id)
    );
    CREATE TABLE IF NOT EXISTS read_states (
      user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
      last_read_event_id INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(user_id,channel_id)
    );
    CREATE TABLE IF NOT EXISTS invitations (
      code TEXT PRIMARY KEY, workspace_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
      expires_at REAL, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0,
      invited_user_id INTEGER, revoked INTEGER NOT NULL DEFAULT 0
    );
    """)
    db.commit()
    try:
        db.execute("ALTER TABLE messages ADD COLUMN event_id INTEGER")
        db.commit()
    except sqlite3.OperationalError:
        pass
    db.close()

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120000)
    return salt.hex() + "$" + digest.hex()

def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120000).hex()
        return secrets.compare_digest(got, digest)
    except Exception:
        return False

def user_to_json(user):
    if isinstance(user, sqlite3.Row):
        user = dict(user)
    return {k: user[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}

def create_user(username, password, display_name=None):
    db = get_db()
    try:
        cur = db.execute("INSERT INTO users(username,password_hash,display_name) VALUES(?,?,?)",
                         (username, hash_password(password), display_name or username))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return row
    except sqlite3.IntegrityError:
        raise HTTPException(409, "username already exists")
    finally:
        db.close()

def issue_token(user_id):
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute("INSERT INTO tokens(token,user_id,created_at) VALUES(?,?,?)",
               (token, user_id, datetime.now(timezone.utc).isoformat()))
    db.commit(); db.close()
    return token

def require_user(request):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "authentication required")
    token = header[7:].strip()
    db = get_db()
    row = db.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?", (token,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(401, "invalid token")
    return row

app = FastAPI()

def get_workspace(slug):
    db=get_db()
    row=db.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
    db.close()
    if not row: raise HTTPException(404, "workspace not found")
    return row

def get_role(workspace_id, user_id):
    db=get_db()
    row=db.execute("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                   (workspace_id,user_id)).fetchone()
    db.close()
    return row["role"] if row else None

def add_member(workspace_id, user_id, role):
    db=get_db()
    db.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",
               (workspace_id,user_id,role))
    db.commit(); db.close()

def workspace_to_json(workspace):
    return {k: workspace[k] for k in ("id","slug","name","owner_id","join_mode")}

def channel_to_json(channel):
    return {"id":channel["id"],"workspace_id":channel["workspace_id"],
            "name":channel["name"],"is_private":bool(channel["is_private"]),
            "is_dm":bool(channel["is_dm"]),"topic":channel["topic"],
            "is_archived":bool(channel["is_archived"])}

def create_channel(workspace_id, name, is_private, creator_id):
    db=get_db()
    try:
        cur=db.execute("INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,?)",
                       (workspace_id,name,1 if is_private else 0))
        cid=cur.lastrowid
        db.execute("INSERT INTO channel_members(channel_id,user_id) VALUES(?,?)",(cid,creator_id))
        db.commit()
        return db.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(409,"channel already exists")
    finally: db.close()

def join_channel(channel, user):
    db=get_db()
    ws=db.execute("SELECT * FROM workspaces WHERE id=?", (channel["workspace_id"],)).fetchone()
    db.close()
    if not ws: raise HTTPException(404, "workspace not found")
    role=get_role(ws["id"],user["id"])
    if not can_join_channel(role):
        raise HTTPException(403,"guests cannot join channels")
    if channel["is_private"]:
        db=get_db()
        already=db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                           (channel["id"],user["id"])).fetchone()
        db.close()
        if not already:
            raise HTTPException(403,"private channel")
    if role is None:
        if ws["join_mode"] != "open": raise HTTPException(403,"workspace membership required")
        add_member(ws["id"],user["id"],"member")
    db=get_db()
    db.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES(?,?)",
               (channel["id"],user["id"]))
    db.commit(); db.close()

def _json_body(request):
    import asyncio
    try:
        return asyncio.get_event_loop().run_until_complete(request.json())
    except Exception:
        return {}

@app.post("/api/workspaces", status_code=201)
async def create_workspace(request: Request):
    user=require_user(request); b=await request.json()
    slug=b.get("slug",""); name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not isinstance(name,str) or not name:
        raise HTTPException(400,"invalid workspace")
    db=get_db()
    try:
        cur=db.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,name,user["id"]))
        wid=cur.lastrowid
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)",(wid,user["id"],"owner"))
        db.commit()
    except sqlite3.IntegrityError: db.close(); raise HTTPException(409,"workspace exists")
    db.close()
    ch=create_channel(wid,"general",False,user["id"])
    ws=get_workspace(slug)
    return {"workspace":workspace_to_json(ws),"general_channel":channel_to_json(ch)}

@app.get("/api/workspaces")
def list_workspaces(request: Request):
    user=require_user(request); db=get_db()
    rows=db.execute("""SELECT w.* FROM workspaces w JOIN workspace_members m
                       ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id""",(user["id"],)).fetchall()
    db.close()
    return {"workspaces":[workspace_to_json(x) for x in rows]}

@app.get("/api/workspaces/{slug}")
def workspace_detail(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug)
    if get_role(ws["id"],user["id"]) is None: raise HTTPException(403,"not a member")
    db=get_db()
    include = str(request.query_params.get("include_archived","")).lower() in ("1","true")
    if include:
        cs=db.execute("SELECT * FROM channels WHERE workspace_id=? ORDER BY id",(ws["id"],)).fetchall()
    else:
        cs=db.execute("SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 ORDER BY id",(ws["id"],)).fetchall()
    cs=[x for x in cs if (not x["is_private"]) or get_channel_member(x["id"],user["id"]) or ws["owner_id"]==user["id"]]
    db.close()
    return {"workspace":workspace_to_json(ws),"channels":[channel_to_json(x) for x in cs],
            "read_state":[get_read_state(user["id"],x["id"]) for x in cs if get_channel_member(x["id"],user["id"])]}

@app.get("/api/workspaces/{slug}/members")
def workspace_members(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug)
    if get_role(ws["id"],user["id"]) is None: raise HTTPException(403,"not a member")
    db=get_db(); rows=db.execute("SELECT user_id,role FROM workspace_members WHERE workspace_id=? ORDER BY user_id",(ws["id"],)).fetchall(); db.close()
    return {"members":[{"user_id":x["user_id"],"role":x["role"]} for x in rows]}

@app.post("/api/workspaces/{slug}/channels", status_code=201)
async def workspace_channels(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug); role=get_role(ws["id"],user["id"])
    if role is None: raise HTTPException(403,"not a member")
    b=await request.json(); name=b.get("name",""); private=bool(b.get("is_private",False))
    if not isinstance(name,str) or not re.fullmatch(r"[a-z0-9-]{1,32}",name):
        raise HTTPException(400,"invalid channel name")
    if b.get("topic") is not None and (not isinstance(b["topic"],str) or len(b["topic"])>250):
        raise HTTPException(400,"invalid topic")
    if not can_create_channel(role, private): raise HTTPException(403,"forbidden")
    ch=create_channel(ws["id"],name,private,user["id"])
    if b.get("topic") is not None:
        db=get_db(); db.execute("UPDATE channels SET topic=? WHERE id=?",(b["topic"],ch["id"])); db.commit(); ch=db.execute("SELECT * FROM channels WHERE id=?",(ch["id"],)).fetchone(); db.close()
    return {"channel":channel_to_json(ch)}

@app.post("/api/channels/{channel_id}/join")
def join(channel_id: int, request: Request):
    user=require_user(request); db=get_db(); ch=db.execute("SELECT * FROM channels WHERE id=?",(channel_id,)).fetchone(); db.close()
    if not ch: raise HTTPException(404,"channel not found")
    before=get_channel_member(ch["id"],user["id"]); join_channel(ch,user)
    if not before: emit_event(channel_id,"member.joined",{"user_id":user["id"]})
    return {"channel":channel_to_json(ch)}

class AuthBody(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str | None = None

@app.on_event("startup")
def startup():
    init_db()

@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

@app.get("/api/health")
def health():
    return {"status":"ok", "node_id":NODE_ID}

@app.post("/api/auth/register", status_code=201)
def register(body: AuthBody):
    if not re.fullmatch(r"[A-Za-z0-9_]+", body.username or "") or len(body.username) > 64:
        raise HTTPException(400, "invalid username")
    if len(body.password) < 8:
        raise HTTPException(400, "password too short")
    user = create_user(body.username, body.password, body.display_name)
    return {"user": user_to_json(user), "token": issue_token(user["id"])}

@app.post("/api/auth/login")
def login(body: AuthBody):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    db.close()
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    return {"user": user_to_json(user), "token": issue_token(user["id"])}

@app.get("/api/auth/me")
def me(request: Request):
    return {"user": user_to_json(require_user(request))}

@app.websocket("/api/ws")
async def websocket(ws: WebSocket):
    token=ws.query_params.get("token","")
    db=get_db(); user=db.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",(token,)).fetchone(); db.close()
    if not user:
        await ws.close(code=1008); return
    await ws.accept()
    import asyncio
    subs={}
    async def send_events(cid, since):
        db=get_db()
        rows=db.execute("SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall()
        db.close()
        for r in rows:
            f=json.loads(r["payload"]); f.update({"type":r["type"],"event_id":r["event_id"],"channel_id":cid})
            await ws.send_json(f)
        return rows[-1]["event_id"] if rows else since
    try:
        while True:
            try: msg=await asyncio.wait_for(ws.receive_json(),timeout=0.1)
            except asyncio.TimeoutError: msg=None
            if msg:
                typ=msg.get("type"); cid=int(msg.get("channel_id",0))
                ch=get_channel(cid)
                if ch["is_private"] and not get_channel_member(cid,user["id"]): continue
                if typ=="subscribe":
                    db=get_db(); head=db.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; db.close()
                    subs[cid]=head
                    await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
                elif typ=="resume":
                    since=int(msg.get("since_event_id",0)); subs[cid]=since
                    head=await send_events(cid,since); subs[cid]=head
                    await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
            for cid,last in list(subs.items()):
                head=await send_events(cid,last)
                subs[cid]=head
    except (WebSocketDisconnect,Exception):
        return

def can_create_channel(role, is_private):
    return role in ("owner","admin") or (role == "member" and not is_private)

def can_join_channel(role):
    return role != "guest"

def set_role(workspace, actor_id, target_id, new_role):
    if new_role not in ("owner","admin","member","guest"):
        raise HTTPException(400, "invalid role")
    if new_role == "owner":
        raise HTTPException(400, "use transfer ownership")
    actor=get_role(workspace["id"],actor_id)
    target=get_role(workspace["id"],target_id)
    if target is None:
        raise HTTPException(404, "member not found")
    if target == "owner" or actor == target_id:
        raise HTTPException(403, "owner protected")
    if actor not in ("owner","admin"):
        raise HTTPException(403, "forbidden")
    if actor == "admin" and target == "admin":
        raise HTTPException(403, "cannot modify admin")
    db=get_db()
    db.execute("UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
               (new_role,workspace["id"],target_id))
    db.commit(); db.close()
    return {"user_id":target_id,"role":new_role}

@app.patch("/api/workspaces/{slug}/members/{user_id}")
async def patch_member_role(slug: str, user_id: int, request: Request):
    actor=require_user(request); ws=get_workspace(slug)
    b=await request.json()
    if "role" not in b:
        raise HTTPException(400,"role required")
    member=set_role(ws,actor["id"],user_id,b["role"])
    return {"member":member}


def update_workspace(workspace, fields):
    if "join_mode" in fields and fields["join_mode"] not in ("open","invite_only"):
        raise HTTPException(400,"invalid join_mode")
    vals=[]; args=[]
    if "name" in fields:
        if not isinstance(fields["name"],str) or not fields["name"]:
            raise HTTPException(400,"invalid name")
        vals.append("name=?"); args.append(fields["name"])
    if "join_mode" in fields:
        vals.append("join_mode=?"); args.append(fields["join_mode"])
    if vals:
        db=get_db(); db.execute("UPDATE workspaces SET "+",".join(vals)+" WHERE id=?",
                               (*args,workspace["id"])); db.commit(); db.close()
    return get_workspace(workspace["slug"])

def create_invitation(workspace, creator_id, expires_in_seconds=None, max_uses=1, invited_user_id=None):
    if max_uses is None: max_uses=1
    if not isinstance(max_uses,int) or max_uses < 1: raise HTTPException(400,"invalid max_uses")
    import time
    code=secrets.token_urlsafe(18)
    expiry=(time.time()+int(expires_in_seconds)) if expires_in_seconds is not None else None
    db=get_db()
    db.execute("""INSERT INTO invitations(code,workspace_id,creator_id,expires_at,max_uses,invited_user_id)
                 VALUES(?,?,?,?,?,?)""",(code,workspace["id"],creator_id,expiry,max_uses,invited_user_id))
    db.commit(); row=db.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone(); db.close()
    return row

def invitation_to_json(row):
    out={"code":row["code"],"workspace_id":row["workspace_id"],"created_by":row["creator_id"],
         "max_uses":row["max_uses"],"uses":row["uses"]}
    if row["expires_at"] is not None: out["expires_at"]=datetime.fromtimestamp(row["expires_at"],timezone.utc).isoformat().replace("+00:00","Z")
    if row["invited_user_id"] is not None: out["invited_user_id"]=row["invited_user_id"]
    return out

def accept_invitation(code, user):
    import time
    db=get_db()
    row=db.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone()
    if not row or row["revoked"] or (row["expires_at"] is not None and row["expires_at"] <= time.time()) or row["uses"] >= row["max_uses"] or (row["invited_user_id"] is not None and row["uses"] >= 1):
        db.close(); raise HTTPException(404,"invitation not found")
    if row["invited_user_id"] is not None and row["invited_user_id"] != user["id"]:
        db.close(); raise HTTPException(403,"invitation is targeted")
    cur=db.execute("""UPDATE invitations SET uses=uses+1
                     WHERE code=? AND revoked=0 AND uses<max_uses
                     AND (invited_user_id IS NULL OR uses<1)
                     AND (expires_at IS NULL OR expires_at>?)""",(code,time.time()))
    if cur.rowcount != 1:
        db.close(); raise HTTPException(404,"invitation not found")
    db.commit(); db.close()
    add_member(row["workspace_id"],user["id"],"member")
    return row

def list_invitations(workspace):
    import time
    db=get_db()
    rows=db.execute("""SELECT * FROM invitations WHERE workspace_id=? AND revoked=0
      AND uses<max_uses AND (invited_user_id IS NULL OR uses<1)
      AND (expires_at IS NULL OR expires_at>?) ORDER BY code""",
      (workspace["id"],time.time())).fetchall()
    db.close(); return rows

def revoke_invitation(workspace, code):
    db=get_db(); row=db.execute("SELECT * FROM invitations WHERE code=? AND workspace_id=?",
                                (code,workspace["id"])).fetchone()
    if not row: db.close(); raise HTTPException(404,"invitation not found")
    db.execute("UPDATE invitations SET revoked=1 WHERE code=?",(code,)); db.commit(); db.close()

def transfer_ownership(workspace, actor_id, new_owner_id):
    if actor_id != workspace["owner_id"]: raise HTTPException(403,"owner only")
    if get_role(workspace["id"],new_owner_id) != "admin": raise HTTPException(400,"target must be admin")
    db=get_db()
    db.execute("UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?",
               (workspace["id"],actor_id))
    db.execute("UPDATE workspace_members SET role='owner' WHERE workspace_id=? AND user_id=?",
               (workspace["id"],new_owner_id))
    db.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(new_owner_id,workspace["id"]))
    db.commit(); db.close()
    return get_workspace(workspace["slug"])

@app.patch("/api/workspaces/{slug}")
async def patch_workspace(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug); role=get_role(ws["id"],user["id"])
    if role not in ("owner","admin"): raise HTTPException(403,"forbidden")
    b=await request.json()
    if "join_mode" in b and role != "owner": raise HTTPException(403,"owner only")
    return {"workspace":workspace_to_json(update_workspace(ws,b))}

@app.post("/api/workspaces/{slug}/invitations", status_code=201)
async def make_invitation(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug)
    if get_role(ws["id"],user["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    b=await request.json()
    invited_id=b.get("invited_user_id")
    if invited_id is None and b.get("invited_username"):
        db=get_db(); u=db.execute("SELECT id FROM users WHERE username=?",(b["invited_username"],)).fetchone(); db.close()
        if not u: raise HTTPException(404,"user not found")
        invited_id=u["id"]
    row=create_invitation(ws,user["id"],b.get("expires_in_seconds"),b.get("max_uses",1),invited_id)
    return {"invitation":invitation_to_json(row)}

@app.post("/api/invitations/{code}/accept")
def accept_invite(code: str, request: Request):
    user=require_user(request); row=accept_invitation(code,user)
    return {"workspace":workspace_to_json(get_workspace_by_id(row["workspace_id"]))}

def get_workspace_by_id(wid):
    db=get_db(); row=db.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); db.close()
    if not row: raise HTTPException(404,"workspace not found")
    return row

@app.get("/api/workspaces/{slug}/invitations")
def get_invitations(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug)
    if get_role(ws["id"],user["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    return {"invitations":[invitation_to_json(x) for x in list_invitations(ws)]}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
def delete_invitation(slug: str, code: str, request: Request):
    user=require_user(request); ws=get_workspace(slug)
    if get_role(ws["id"],user["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    revoke_invitation(ws,code); return {"deleted":True}

@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug: str, request: Request):
    user=require_user(request); ws=get_workspace(slug); b=await request.json()
    return {"workspace":workspace_to_json(transfer_ownership(ws,user["id"],b.get("new_owner_id")))}


def get_read_state(user_id,channel_id):
    get_channel(channel_id); ensure_message_tables(); db=get_db()
    row=db.execute("SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?",(user_id,channel_id)).fetchone()
    last=row["last_read_event_id"] if row else 0
    rows=db.execute("""SELECT m.* FROM messages m WHERE m.channel_id=? AND m.parent_id IS NULL
                      AND m.deleted=0 AND m.author_id<>? AND COALESCE(m.event_id,0)>?
                      ORDER BY m.event_id""",(channel_id,user_id,last)).fetchall()
    db.close()
    mentions=sum(1 for m in rows if user_id in resolve_mentions(m["body"],get_channel(channel_id),m["author_id"]))
    return {"channel_id":channel_id,"last_read_event_id":last,"unread_count":len(rows),"mention_count":mentions}

def mark_read(user_id,channel_id,event_id):
    get_channel(channel_id)
    try: eid=max(0,int(event_id))
    except Exception: raise HTTPException(400,"invalid event id")
    db=get_db()
    db.execute("""INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?)
                  ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=
                  CASE WHEN excluded.last_read_event_id>last_read_event_id THEN excluded.last_read_event_id ELSE last_read_event_id END""",
               (user_id,channel_id,eid))
    db.commit(); db.close()
    return get_read_state(user_id,channel_id)

def get_message(message_id):
    ensure_message_tables(); db=get_db(); row=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(message_id,)).fetchone(); db.close()
    if not row: raise HTTPException(404,"message not found")
    return row

@app.get("/api/channels/{channel_id}/read")
def get_read(channel_id:int,request:Request):
    user=require_user(request); return {"read_state":get_read_state(user["id"],channel_id)}

@app.post("/api/channels/{channel_id}/read")
async def post_read(channel_id:int,request:Request):
    user=require_user(request); b=await request.json()
    return {"read_state":mark_read(user["id"],channel_id,b.get("last_read_event_id"))}

def get_channel(channel_id):
    db=get_db(); row=db.execute("SELECT * FROM channels WHERE id=?",(channel_id,)).fetchone(); db.close()
    if not row: raise HTTPException(404,"channel not found")
    return row

def get_channel_member(channel_id,user_id):
    db=get_db(); row=db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(channel_id,user_id)).fetchone(); db.close()
    return bool(row)

def set_topic(channel,user,topic):
    if channel["is_archived"]: raise HTTPException(423,"channel archived")
    if not isinstance(topic,str) or len(topic)>250: raise HTTPException(400,"invalid topic")
    if not get_channel_member(channel["id"],user["id"]) and get_role(channel["workspace_id"],user["id"]) != "owner":
        raise HTTPException(403,"not a channel member")
    db=get_db(); db.execute("UPDATE channels SET topic=? WHERE id=?",(topic,channel["id"])); db.commit()
    row=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close(); return row

def archive_channel(channel,user,archived):
    ws=get_workspace_by_id(channel["workspace_id"])
    if ws["owner_id"] != user["id"]: raise HTTPException(403,"owner only")
    db=get_db(); db.execute("UPDATE channels SET is_archived=? WHERE id=?",(1 if archived else 0,channel["id"])); db.commit()
    row=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close(); return row

def leave_channel(channel,user):
    db=get_db(); db.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",(channel["id"],user["id"])); db.commit(); db.close()
    return get_channel(channel["id"])

def workspace_detail(workspace,user,include_archived=False):
    return {}

@app.patch("/api/channels/{channel_id}")
async def patch_channel(channel_id:int,request:Request):
    user=require_user(request); ch=get_channel(channel_id); b=await request.json()
    row=set_topic(ch,user,b.get("topic")); out=channel_to_json(row); emit_event(ch["id"],"channel.updated",{"channel":out}); return {"channel":out}

@app.post("/api/channels/{channel_id}/archive")
def archive(channel_id:int,request:Request):
    row=archive_channel(get_channel(channel_id),require_user(request),True); out=channel_to_json(row); emit_event(channel_id,"channel.archived",{"channel":out}); return {"channel":out}

@app.post("/api/channels/{channel_id}/unarchive")
def unarchive(channel_id:int,request:Request):
    row=archive_channel(get_channel(channel_id),require_user(request),False); out=channel_to_json(row); emit_event(channel_id,"channel.unarchived",{"channel":out}); return {"channel":out}

@app.post("/api/channels/{channel_id}/leave")
def leave(channel_id:int,request:Request):
    user=require_user(request); row=leave_channel(get_channel(channel_id),user); out=channel_to_json(row); emit_event(channel_id,"member.left",{"user_id":user["id"]}); return {"channel":out}

@app.get("/api/channels/{channel_id}/members")
def channel_members(channel_id:int,request:Request):
    user=require_user(request); ch=get_channel(channel_id)
    if ch["is_private"] and not get_channel_member(channel_id,user["id"]) and get_role(ch["workspace_id"],user["id"]) != "owner":
        raise HTTPException(403,"not a member")
    db=get_db()
    rows=db.execute("SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=? ORDER BY u.id",(channel_id,)).fetchall()
    db.close()
    return {"members":[user_to_json(x) for x in rows]}


def ensure_message_tables():
    return

def emit_event(channel_id,type,payload):
    ensure_message_tables()
    import time
    for attempt in range(8):
        db=get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            head=db.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(channel_id,)).fetchone()["n"]
            eid=head+1
            db.execute("INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)",
                       (channel_id,eid,type,json.dumps(payload)))
            db.commit(); db.close(); return eid
        except sqlite3.OperationalError as e:
            db.rollback(); db.close()
            if "locked" not in str(e).lower() or attempt == 7: raise
            time.sleep(0.03*(attempt+1))

def channel_head(channel_id):
    ensure_message_tables(); db=get_db(); n=db.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(channel_id,)).fetchone()["n"]; db.close(); return n

def resolve_mentions(body,channel,author_id):
    db=get_db(); ws=channel["workspace_id"]
    ids=[]
    for m in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)",body):
        if m.lower() in ("channel","here"):
            rows=db.execute("SELECT user_id FROM channel_members WHERE channel_id=? AND user_id<>?",(channel["id"],author_id)).fetchall()
            ids += [x["user_id"] for x in rows]
        else:
            row=db.execute("""SELECT u.id FROM users u JOIN workspace_members wm ON wm.user_id=u.id
                             WHERE wm.workspace_id=? AND u.username=? AND u.id<>?""",(ws,m,author_id)).fetchone()
            if row: ids.append(row["id"])
    db.close()
    return list(dict.fromkeys(ids))

def post_message(channel, author, body, parent_id=None):
    if channel["is_archived"]: raise HTTPException(423,"channel archived")
    if not isinstance(body,str) or not body.strip(): raise HTTPException(400,"empty message")
    if not get_channel_member(channel["id"],author["id"]): raise HTTPException(403,"not a channel member")
    ensure_message_tables(); db=get_db()
    import time
    if parent_id is not None:
        parent=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(parent_id,)).fetchone()
        if not parent or parent["channel_id"] != channel["id"]: db.close(); raise HTTPException(404,"parent not found")
    now=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
    for attempt in range(10):
        try:
            cur=db.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",
                           (channel["id"],author["id"],body,parent_id,now))
            db.commit(); row=db.execute("SELECT * FROM messages WHERE id=?",(cur.lastrowid,)).fetchone(); db.close()
            return row
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 9:
                db.rollback(); db.close(); raise
            time.sleep(0.05*(attempt+1))

def message_to_json(message):
    db=get_db()
    author=db.execute("SELECT * FROM users WHERE id=?",(message["author_id"],)).fetchone()
    rc=db.execute("SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0",(message["id"],)).fetchone()["n"]
    db.close()
    return {"id":message["id"],"channel_id":message["channel_id"],"author_id":message["author_id"],
            "author":user_to_json(author),"body":message["body"],"parent_id":message["parent_id"],
            "reply_count":rc,"created_at":message["created_at"],"edited_at":message["edited_at"],
            "files":[],"reactions":[],"mentions":resolve_mentions(message["body"],get_channel(message["channel_id"]),message["author_id"])}

def encode_cursor(message_id):
    import base64
    return base64.urlsafe_b64encode(("m:"+str(int(message_id))).encode()).decode().rstrip("=")

def decode_cursor(cursor):
    import base64
    try:
        raw=base64.urlsafe_b64decode(cursor+"===" ).decode()
        if not raw.startswith("m:") or not raw[2:].isdigit(): raise ValueError()
        return int(raw[2:])
    except Exception: raise HTTPException(400,"invalid cursor")

def read_limit(request):
    raw=request.query_params.get("limit","50")
    try: n=int(raw)
    except Exception: raise HTTPException(400,"invalid limit")
    if n<1 or n>200: raise HTTPException(400,"invalid limit")
    return n

def page_rows(rows,limit):
    page=rows[:limit]; nxt=encode_cursor(page[-1]["id"]) if len(rows)>limit else None
    return page,nxt

def list_messages(channel,limit,cursor):
    ensure_message_tables(); db=get_db(); args=[channel["id"]]
    sql="SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0"
    if cursor is not None: sql+=" AND id<?"; args.append(decode_cursor(cursor))
    sql+=" ORDER BY id DESC LIMIT ?"; args.append(limit+1)
    rows=db.execute(sql,args).fetchall(); db.close(); return page_rows(rows,limit)

def list_replies(parent,limit,cursor):
    ensure_message_tables(); db=get_db(); args=[parent["id"]]
    sql="SELECT * FROM messages WHERE parent_id=? AND deleted=0"
    if cursor is not None: sql+=" AND id>?"; args.append(decode_cursor(cursor))
    sql+=" ORDER BY id ASC LIMIT ?"; args.append(limit+1)
    rows=db.execute(sql,args).fetchall(); db.close()
    page=rows[:limit]; nxt=encode_cursor(page[-1]["id"]) if len(rows)>limit else None
    return page,nxt

def accessible(user_id,channel_id):
    db=get_db()
    ch=db.execute("SELECT * FROM channels WHERE id=?",(channel_id,)).fetchone()
    ok=bool(ch and get_channel_member(channel_id,user_id))
    db.close(); return ok

def search_messages(user,text,workspace_id,limit,cursor):
    ensure_message_tables(); db=get_db(); args=["%"+text+"%",user["id"]]
    sql="""SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id
      WHERE m.deleted=0 AND m.body LIKE ? AND EXISTS
      (SELECT 1 FROM channel_members cm WHERE cm.channel_id=m.channel_id AND cm.user_id=?)"""
    if workspace_id is not None: sql+=" AND c.workspace_id=?"; args.append(workspace_id)
    if cursor is not None: sql+=" AND m.id<?"; args.append(decode_cursor(cursor))
    sql+=" ORDER BY m.id DESC LIMIT ?"; args.append(limit+1)
    rows=db.execute(sql,args).fetchall(); db.close(); return page_rows(rows,limit)

def pin_message(message,user):
    ch=get_channel(message["channel_id"])
    if ch["is_archived"]: raise HTTPException(423,"channel archived")
    if not get_channel_member(ch["id"],user["id"]): raise HTTPException(403,"not a member")
    now=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
    db=get_db(); db.execute("INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)",(message["id"],user["id"],now)); db.commit()
    row=db.execute("SELECT * FROM pins WHERE message_id=?",(message["id"],)).fetchone(); db.close()
    return {"message_id":row["message_id"],"pinned_by":row["pinned_by"],"pinned_at":row["pinned_at"]}

def unpin_message(message):
    ensure_message_tables(); db=get_db(); db.execute("DELETE FROM pins WHERE message_id=?",(message["id"],)); db.commit(); db.close()

def list_pins(channel,user,limit,cursor):
    if channel["is_private"] and not get_channel_member(channel["id"],user["id"]): raise HTTPException(403,"not a member")
    ensure_message_tables(); db=get_db(); args=[channel["id"]]
    sql="SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0"
    if cursor is not None: sql+=" AND p.message_id<?"; args.append(decode_cursor(cursor))
    sql+=" ORDER BY p.message_id DESC LIMIT ?"; args.append(limit+1)
    rows=db.execute(sql,args).fetchall(); db.close()
    page=rows[:limit]; nxt=encode_cursor(page[-1]["message_id"]) if len(rows)>limit else None
    return page,nxt

def _message_access(request,ch):
    user=require_user(request)
    if not get_channel_member(ch["id"],user["id"]): raise HTTPException(403,"not a channel member")
    return user

@app.post("/api/channels/{channel_id}/messages",status_code=201)
async def post_message_route(channel_id:int,request:Request):
    ch=get_channel(channel_id); user=_message_access(request,ch); b=await request.json()
    m=post_message(ch,user,b.get("body",""),b.get("parent_id")); obj=message_to_json(m)
    eid=emit_event(ch["id"],"message.reply" if m["parent_id"] is not None else "message.created",{"message":obj})
    db=get_db(); db.execute("UPDATE messages SET event_id=? WHERE id=?",(eid,m["id"])); db.commit(); db.close()
    return {"message":obj}

@app.get("/api/channels/{channel_id}/messages")
def get_messages(channel_id:int,request:Request):
    ch=get_channel(channel_id); _message_access(request,ch); rows,nxt=list_messages(ch,read_limit(request),request.query_params.get("cursor"))
    return {"messages":[message_to_json(x) for x in rows],"next_cursor":nxt}

@app.get("/api/messages/{parent_id}/replies")
def get_replies(parent_id:int,request:Request):
    user=require_user(request); ensure_message_tables(); db=get_db(); parent=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(parent_id,)).fetchone(); db.close()
    if not parent: raise HTTPException(404,"message not found")
    ch=get_channel(parent["channel_id"])
    if not get_channel_member(ch["id"],user["id"]): raise HTTPException(403,"not a member")
    rows,nxt=list_replies(parent,read_limit(request),request.query_params.get("cursor"))
    return {"replies":[message_to_json(x) for x in rows],"next_cursor":nxt}

@app.get("/api/search")
def search(request:Request):
    user=require_user(request); q=request.query_params.get("q","")
    rows,nxt=search_messages(user,q,request.query_params.get("workspace_id"),read_limit(request),request.query_params.get("cursor"))
    return {"results":[{"message":message_to_json(x)} for x in rows],"next_cursor":nxt}

@app.post("/api/messages/{message_id}/pin")
def pin(message_id:int,request:Request):
    user=require_user(request); ensure_message_tables(); db=get_db(); m=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(message_id,)).fetchone(); db.close()
    if not m: raise HTTPException(404,"message not found")
    return {"pin":pin_message(m,user)}

@app.delete("/api/messages/{message_id}/pin")
def unpin(message_id:int,request:Request):
    require_user(request); ensure_message_tables(); db=get_db(); m=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(message_id,)).fetchone(); db.close()
    if not m: raise HTTPException(404,"message not found")
    unpin_message(m); return {"unpinned":True}

@app.get("/api/channels/{channel_id}/pins")
def pins(channel_id:int,request:Request):
    user=require_user(request); ch=get_channel(channel_id); rows,nxt=list_pins(ch,user,read_limit(request),request.query_params.get("cursor"))
    return {"pins":[{"message":message_to_json(x),"pinned_by":x["pinned_by"],"pinned_at":x["pinned_at"]} for x in rows],"next_cursor":nxt}
