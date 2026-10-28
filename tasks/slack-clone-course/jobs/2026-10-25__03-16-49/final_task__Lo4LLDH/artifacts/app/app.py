import os, re, sqlite3, hashlib, secrets, json, asyncio, base64
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

DB_PATH="/app/data/app.db"
os.makedirs("/app/data",exist_ok=True)
NODE_ID=int(os.getenv("NODE_ID","0"))
app=FastAPI()

def get_db():
    db=sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    db=get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER,created_at TEXT);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS read_state(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    """)
    db.commit(); db.close()
init_db()

def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def hash_password(password): return hashlib.sha256(("huddle:"+password).encode()).hexdigest()
def user_to_json(u):
    return {k:u[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def create_user(username,password,display_name=None):
    db=get_db()
    try:
        cur=db.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(username,hash_password(password),display_name or username)); db.commit()
    except sqlite3.IntegrityError: db.close(); raise HTTPException(409,"username already exists")
    u=db.execute("SELECT * FROM users WHERE id=?",(cur.lastrowid,)).fetchone(); db.close(); return u
def issue_token(user_id):
    t=secrets.token_urlsafe(32); db=get_db(); db.execute("INSERT INTO tokens VALUES(?,?,?)",(t,user_id,now())); db.commit(); db.close(); return t
def require_user(request):
    h=request.headers.get("authorization","")
    if not h.lower().startswith("bearer "): raise HTTPException(401,"authentication required")
    db=get_db(); u=db.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",(h[7:].strip(),)).fetchone(); db.close()
    if not u: raise HTTPException(401,"invalid token")
    return u
def clean_body(request):
    return request.json()
def ws_user(token):
    db=get_db(); u=db.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",(token,)).fetchone(); db.close(); return u
def workspace_json(r): return {k:r[k] for k in ("id","slug","name","owner_id","join_mode")}
def channel_json(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"] or "","is_archived":bool(r["is_archived"])}
def member_role(db,wid,uid):
    r=db.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(wid,uid)).fetchone(); return r["role"] if r else None
def accessible(db,cid,uid):
    r=db.execute("SELECT c.*,m.user_id FROM channels c LEFT JOIN channel_members m ON c.id=m.channel_id AND m.user_id=? WHERE c.id=?",(uid,cid)).fetchone()
    if not r: raise HTTPException(404,"channel not found")
    if r["is_private"] and r["user_id"] is None: raise HTTPException(403,"forbidden")
    return r
def message_json(db,r):
    author=db.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    reps=[]
    for x in db.execute("SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji",(r["id"],)):
        reps.append({"emoji":x["emoji"],"count":x["n"],"user_ids":[int(v) for v in x["ids"].split(",")]})
    replies=db.execute("SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()["n"]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_to_json(author),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":reps,"mentions":[]}
@app.exception_handler(HTTPException)
async def he(request,exc): return JSONResponse({"error":exc.detail},status_code=exc.status_code)
@app.exception_handler(Exception)
async def ee(request,exc): return JSONResponse({"error":str(exc)},status_code=400)

@app.get("/api/health")
def health(): return {"status":"ok","node_id":NODE_ID}
@app.post("/api/auth/register",status_code=201)
async def register(request:Request):
    try: b=await request.json()
    except: raise HTTPException(400,"invalid json")
    u=b.get("username",""); p=b.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: raise HTTPException(400,"invalid input")
    x=create_user(u,p,b.get("display_name")); return {"user":user_to_json(x),"token":issue_token(x["id"])}
@app.post("/api/auth/login")
async def login(request:Request):
    b=await request.json(); db=get_db(); u=db.execute("SELECT * FROM users WHERE username=? AND password=?",(b.get("username"),hash_password(b.get("password","")))).fetchone(); db.close()
    if not u: raise HTTPException(401,"invalid credentials")
    return {"user":user_to_json(u),"token":issue_token(u["id"])}
@app.get("/api/auth/me")
def me(request:Request): return {"user":user_to_json(require_user(request))}
@app.get("/api/users/{uid}")
def get_user(uid:int):
    db=get_db(); u=db.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); db.close()
    if not u: raise HTTPException(404,"user not found")
    return {"user":user_to_json(u)}
@app.patch("/api/users/me")
async def patch_me(request:Request):
    u=require_user(request); b=await request.json(); allowed={"display_name","timezone","avatar_url","status_text","status_emoji"}
    if "timezone" in b and (not isinstance(b["timezone"],str) or len(b["timezone"])>64): raise HTTPException(400,"invalid timezone")
    db=get_db(); vals=[b[k] for k in allowed if k in b]; 
    if vals:
        sets=",".join(k+"=?" for k in allowed if k in b); db.execute("UPDATE users SET "+sets+" WHERE id=?",vals+[u["id"]]); db.commit()
    x=db.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone(); db.close(); return {"user":user_to_json(x)}

@app.post("/api/workspaces",status_code=201)
async def create_ws(request:Request):
    u=require_user(request); b=await request.json(); slug=b.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not b.get("name"): raise HTTPException(400,"invalid workspace")
    db=get_db()
    try:
        c=db.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,b["name"],u["id"])); wid=c.lastrowid
        db.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner")); gc=db.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=gc.lastrowid; db.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); db.commit()
    except sqlite3.IntegrityError: db.close(); raise HTTPException(409,"duplicate workspace")
    w=db.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=db.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); db.close()
    return {"workspace":workspace_json(w),"general_channel":channel_json(ch)}
@app.get("/api/workspaces")
def list_ws(request:Request):
    u=require_user(request); db=get_db(); rows=db.execute("SELECT DISTINCT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=? OR w.owner_id=?",(u["id"],u["id"])).fetchall(); db.close(); return {"workspaces":[workspace_json(x) for x in rows]}
@app.get("/api/workspaces/{slug}")
def get_ws(slug:str,request:Request,include_archived:Optional[str]=None):
    u=require_user(request); w=get_workspace(slug); db=get_db()
    rows=db.execute("SELECT c.* FROM channels c LEFT JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=? WHERE c.workspace_id=? AND (c.is_private=0 OR cm.user_id IS NOT NULL) AND (? OR c.is_archived=0)",(u["id"],w["id"],str(include_archived).lower() in ("1","true","yes"))).fetchall()
    db.close()
    return {"workspace":workspace_to_json(w),"channels":[channel_to_json(x) for x in rows],
            "read_state":[get_read_state(u["id"],x["id"]) for x in rows if get_role(w["id"],u["id"]) is not None]}


def emit_event(channel_id,type,payload):
    db=get_db()
    db.execute("BEGIN IMMEDIATE")
    x=db.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(channel_id,)).fetchone()["n"]
    db.execute("INSERT INTO events VALUES(?,?,?,?)",(channel_id,x,type,json.dumps(payload,separators=(",",":")))); db.commit(); db.close(); return x

def channel_head(channel_id):
    db=get_db(); x=db.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(channel_id,)).fetchone()["n"]; db.close(); return x

def resolve_mentions(body,channel,author_id):
    db=get_db(); ids=[]; ws=db.execute("SELECT workspace_id FROM channels WHERE id=?",(channel["id"],)).fetchone()["workspace_id"]
    members={x["id"] for x in db.execute("SELECT u.id FROM users u JOIN members m ON m.user_id=u.id WHERE m.workspace_id=?",(ws,))}
    cm={x["user_id"] for x in db.execute("SELECT user_id FROM channel_members WHERE channel_id=?",(channel["id"],))}
    if re.search(r"(?<![\w@])@channel(?!\w)",body): ids += [x for x in cm if x!=author_id]
    for name in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)\b",body):
        u=db.execute("SELECT id FROM users WHERE username=?",(name,)).fetchone()
        if u and u["id"] in members and u["id"] in cm and u["id"]!=author_id: ids.append(u["id"])
    db.close(); return list(dict.fromkeys(ids))
@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
    u=ws_user(ws.query_params.get("token",""))
    if not u: await ws.close(code=1008); return
    await ws.accept(); subs={}
    async def send_events(cid,since):
        db=get_db(); rows=db.execute("SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall(); db.close()
        for r in rows:
            f=json.loads(r["payload"]); f.update({"type":r["type"],"event_id":r["event_id"],"channel_id":cid}); await ws.send_json(f)
    try:
        while True:
            try: msg=await asyncio.wait_for(ws.receive_json(),0.1)
            except asyncio.TimeoutError:
                for cid,last in list(subs.items()): await send_events(cid,last); subs[cid]=channel_head(cid)
                continue
            typ=msg.get("type"); cid=int(msg.get("channel_id",0))
            if typ=="subscribe":
                h=channel_head(cid); subs[cid]=h; await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":h})
            elif typ=="resume":
                since=int(msg.get("since_event_id",0)); await send_events(cid,since); h=channel_head(cid); subs[cid]=h; await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":h})
    except (WebSocketDisconnect,Exception): return

@app.post("/api/workspaces/{slug}/channels",status_code=201)
async def create_channel(slug:str,request:Request):
    u=require_user(request); b=await request.json(); db=get_db(); w=db.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: db.close(); raise HTTPException(404,"workspace not found")
    role=member_role(db,w["id"],u["id"])
    if role not in ("owner","admin","member"): db.close(); raise HTTPException(403,"forbidden")
    if b.get("is_private") and role not in ("owner","admin"): db.close(); raise HTTPException(403,"forbidden")
    name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name) or len(b.get("topic",""))>250: db.close(); raise HTTPException(400,"invalid channel")
    try: c=db.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(b.get("is_private"))),b.get("topic",""))); db.execute("INSERT INTO channel_members VALUES(?,?)",(c.lastrowid,u["id"])); db.commit()
    except sqlite3.IntegrityError: db.close(); raise HTTPException(409,"duplicate channel")
    r=db.execute("SELECT * FROM channels WHERE id=?",(c.lastrowid,)).fetchone(); db.close(); return {"channel":channel_json(r)}

def get_workspace(slug):
    db=get_db(); w=db.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone(); db.close()
    if not w: raise HTTPException(404,"workspace not found")
    return w

def get_role(workspace_id,user_id):
    db=get_db(); r=db.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(workspace_id,user_id)).fetchone(); db.close()
    return r["role"] if r else None

def add_member(workspace_id,user_id,role="member"):
    db=get_db(); db.execute("INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)",(workspace_id,user_id,role)); db.commit(); db.close()

def workspace_to_json(workspace):
    return {k:workspace[k] for k in ("id","slug","name","owner_id","join_mode")}

def create_channel(workspace_id,name,is_private,creator_id):
    db=get_db()
    try: cur=db.execute("INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,?)",(workspace_id,name,int(bool(is_private))))
    except sqlite3.IntegrityError: db.close(); raise HTTPException(409,"duplicate channel")
    db.execute("INSERT INTO channel_members(channel_id,user_id) VALUES(?,?)",(cur.lastrowid,creator_id)); db.commit()
    r=db.execute("SELECT * FROM channels WHERE id=?",(cur.lastrowid,)).fetchone(); db.close(); return r

def channel_to_json(channel):
    return {"id":channel["id"],"workspace_id":channel["workspace_id"],"name":channel["name"],
            "is_private":bool(channel["is_private"]),"is_dm":bool(channel["is_dm"]),
            "topic":channel["topic"] or "","is_archived":bool(channel["is_archived"])}

def join_channel(channel,user):
    db=get_db()
    w=db.execute("SELECT * FROM workspaces WHERE id=?",(channel["workspace_id"],)).fetchone()
    if channel["is_private"]:
        existing=db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(channel["id"],user["id"])).fetchone()
        if not existing: db.close(); raise HTTPException(403,"private channel")
    role=db.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],user["id"])).fetchone()
    if role and role["role"]=="guest": db.close(); raise HTTPException(403,"guests cannot join channels")
    if not role:
        if w["join_mode"]!="open": db.close(); raise HTTPException(403,"workspace membership required")
        db.execute("INSERT INTO members(workspace_id,user_id,role) VALUES(?,?,?)",(w["id"],user["id"],"member"))
    existed=db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(channel["id"],user["id"])).fetchone()
    db.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES(?,?)",(channel["id"],user["id"]))
    db.commit(); r=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close()
    if not existed: emit_event(channel["id"],"member.joined",{"user_id":user["id"]})
    return r

@app.get("/api/workspaces/{slug}/members")
def workspace_members(slug:str,request:Request):
    u=require_user(request); w=get_workspace(slug); db=get_db()
    if not db.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone(): db.close(); raise HTTPException(403,"forbidden")
    rows=db.execute("SELECT user_id,role FROM members WHERE workspace_id=? ORDER BY user_id",(w["id"],)).fetchall(); db.close()
    return {"members":[{"user_id":r["user_id"],"role":r["role"]} for r in rows]}

@app.post("/api/channels/{cid}/join")
def join(cid:int,request:Request):
    u=require_user(request); db=get_db(); c=db.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); db.close()
    if not c: raise HTTPException(404,"channel not found")
    return {"channel":channel_to_json(join_channel(c,u))}


def can_create_channel(role,is_private):
    return role in ("owner","admin") if is_private else role in ("owner","admin","member")

def can_join_channel(role):
    return role in ("owner","admin","member")

def set_role(workspace,actor_id,target_id,new_role):
    if new_role not in ("owner","admin","member","guest"): raise HTTPException(400,"invalid role")
    if new_role=="owner": raise HTTPException(400,"use transfer_ownership")
    db=get_db()
    actor=db.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(workspace["id"],actor_id)).fetchone()
    target=db.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(workspace["id"],target_id)).fetchone()
    if not actor or not target: db.close(); raise HTTPException(403,"forbidden")
    ar,tr=actor["role"],target["role"]
    if tr=="owner" or (ar not in ("owner","admin")) or (ar=="admin" and tr=="admin"): db.close(); raise HTTPException(403,"forbidden")
    db.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",(new_role,workspace["id"],target_id)); db.commit()
    out=db.execute("SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?",(workspace["id"],target_id)).fetchone(); db.close()
    return {"user_id":out["user_id"],"role":out["role"]}

@app.patch("/api/workspaces/{slug}/members/{uid}")
async def patch_member(slug:str,uid:int,request:Request):
    u=require_user(request); w=get_workspace(slug); b=await request.json()
    return {"member":set_role(w,u["id"],uid,b.get("role"))}

def update_workspace(workspace,fields):
    if "join_mode" in fields and fields["join_mode"] not in ("open","invite_only"):
        raise HTTPException(400,"invalid join mode")
    allowed={"name","join_mode"}
    vals={k:fields[k] for k in allowed if k in fields}
    if vals:
        db=get_db(); sets=",".join(k+"=?" for k in vals)
        db.execute("UPDATE workspaces SET "+sets+" WHERE id=?",list(vals.values())+[workspace["id"]]); db.commit()
        workspace=db.execute("SELECT * FROM workspaces WHERE id=?",(workspace["id"],)).fetchone(); db.close()
    return workspace

def create_invitation(workspace,creator_id,expires_in_seconds=None,max_uses=1,invited_user_id=None):
    role=get_role(workspace["id"],creator_id)
    if role not in ("owner","admin"): raise HTTPException(403,"forbidden")
    try: max_uses=int(max_uses)
    except: raise HTTPException(400,"invalid max_uses")
    if max_uses<1: raise HTTPException(400,"invalid max_uses")
    code=secrets.token_urlsafe(18)
    exp=None
    if expires_in_seconds is not None:
        try: exp=datetime.fromtimestamp(datetime.now(timezone.utc).timestamp()+int(expires_in_seconds),timezone.utc).isoformat().replace("+00:00","Z")
        except: raise HTTPException(400,"invalid expiry")
    db=get_db(); db.execute("INSERT INTO invitations(code,workspace_id,invited_user_id,expires_at,max_uses) VALUES(?,?,?,?,?)",(code,workspace["id"],invited_user_id,exp,max_uses)); db.commit()
    r=db.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone(); db.close(); return r

def invitation_json(r):
    return {"code":r["code"],"workspace_id":r["workspace_id"],"invited_user_id":r["invited_user_id"],"expires_at":r["expires_at"],"max_uses":r["max_uses"],"uses":r["uses"]}

def accept_invitation(code,user):
    db=get_db(); r=db.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone()
    if not r or r["revoked"] or r["uses"]>=r["max_uses"] or (r["expires_at"] and r["expires_at"]<=now()): db.close(); raise HTTPException(404,"invitation not found")
    if r["invited_user_id"] is not None and r["invited_user_id"]!=user["id"]: db.close(); raise HTTPException(403,"invitation targeted")
    cur=db.execute("UPDATE invitations SET uses=uses+1 WHERE code=? AND revoked=0 AND uses<max_uses",(code,))
    if cur.rowcount!=1: db.close(); raise HTTPException(404,"invitation not found")
    db.execute("INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)",(r["workspace_id"],user["id"],"member")); db.commit(); db.close()
    return True

def list_invitations(workspace):
    db=get_db(); rows=db.execute("SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)",(workspace["id"],now())).fetchall(); db.close(); return rows

def revoke_invitation(workspace,code):
    db=get_db(); r=db.execute("SELECT * FROM invitations WHERE workspace_id=? AND code=?",(workspace["id"],code)).fetchone()
    if not r: db.close(); raise HTTPException(404,"invitation not found")
    db.execute("UPDATE invitations SET revoked=1 WHERE workspace_id=? AND code=?",(workspace["id"],code)); db.commit(); db.close(); return True

def transfer_ownership(workspace,actor_id,new_owner_id):
    if get_role(workspace["id"],actor_id)!="owner": raise HTTPException(403,"owner only")
    if get_role(workspace["id"],new_owner_id)!="admin": raise HTTPException(400,"target must be admin")
    db=get_db(); db.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(workspace["id"],actor_id)); db.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(workspace["id"],new_owner_id)); db.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(new_owner_id,workspace["id"])); db.commit()
    out=db.execute("SELECT * FROM workspaces WHERE id=?",(workspace["id"],)).fetchone(); db.close(); return out

@app.patch("/api/workspaces/{slug}")
async def patch_workspace(slug:str,request:Request):
    u=require_user(request); w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    return {"workspace":workspace_to_json(update_workspace(w,await request.json()))}

@app.post("/api/workspaces/{slug}/invitations",status_code=201)
async def post_invitation(slug:str,request:Request):
    u=require_user(request); w=get_workspace(slug); b=await request.json()
    target=b.get("invited_user_id")
    if target is None and b.get("invited_username"):
        db=get_db(); x=db.execute("SELECT id FROM users WHERE username=?",(b["invited_username"],)).fetchone(); db.close()
        if not x: raise HTTPException(404,"user not found")
        target=x["id"]
    return {"invitation":invitation_json(create_invitation(w,u["id"],b.get("expires_in_seconds"),b.get("max_uses",1),target))}

@app.post("/api/invitations/{code}/accept")
def post_accept(code:str,request:Request):
    u=require_user(request); accept_invitation(code,u); return {"accepted":True}

@app.get("/api/workspaces/{slug}/invitations")
def get_invitations(slug:str,request:Request):
    u=require_user(request); w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    return {"invitations":[invitation_json(x) for x in list_invitations(w)]}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
def delete_invitation(slug:str,code:str,request:Request):
    u=require_user(request); w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403,"forbidden")
    revoke_invitation(w,code); return {"deleted":True}

@app.post("/api/workspaces/{slug}/transfer_ownership")
async def post_transfer(slug:str,request:Request):
    u=require_user(request); w=get_workspace(slug); b=await request.json()
    return {"workspace":workspace_to_json(transfer_ownership(w,u["id"],b.get("new_owner_id")))}


def get_channel(channel_id):
    db=get_db(); c=db.execute("SELECT * FROM channels WHERE id=?",(channel_id,)).fetchone(); db.close()
    if not c: raise HTTPException(404,"channel not found")
    return c

def set_topic(channel,user,topic):
    if not isinstance(topic,str) or len(topic)>250: raise HTTPException(400,"invalid topic")
    db=get_db()
    member=db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(channel["id"],user["id"])).fetchone()
    owner=db.execute("SELECT owner_id FROM workspaces WHERE id=?",(channel["workspace_id"],)).fetchone()
    if not member and (not owner or owner["owner_id"]!=user["id"]): db.close(); raise HTTPException(403,"forbidden")
    db.execute("UPDATE channels SET topic=? WHERE id=?",(topic,channel["id"])); db.commit()
    out=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close(); emit_event(channel["id"],"channel.updated",{"channel":channel_to_json(out)}); return out

def archive_channel(channel,user,archived):
    db=get_db(); w=db.execute("SELECT owner_id FROM workspaces WHERE id=?",(channel["workspace_id"],)).fetchone()
    if not w or w["owner_id"]!=user["id"]: db.close(); raise HTTPException(403,"owner only")
    db.execute("UPDATE channels SET is_archived=? WHERE id=?",(int(archived),channel["id"])); db.commit()
    out=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close(); emit_event(channel["id"],"channel.archived" if archived else "channel.unarchived",{"channel":channel_to_json(out)}); return out

def leave_channel(channel,user):
    db=get_db(); db.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",(channel["id"],user["id"])); db.commit()
    out=db.execute("SELECT * FROM channels WHERE id=?",(channel["id"],)).fetchone(); db.close(); emit_event(channel["id"],"member.left",{"user_id":user["id"]}); return out

@app.patch("/api/channels/{cid}")
async def patch_channel(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid); b=await request.json()
    return {"channel":channel_to_json(set_topic(c,u,b.get("topic")))}

@app.post("/api/channels/{cid}/archive")
def post_archive(cid:int,request:Request):
    u=require_user(request); return {"channel":channel_to_json(archive_channel(get_channel(cid),u,True))}

@app.post("/api/channels/{cid}/unarchive")
def post_unarchive(cid:int,request:Request):
    u=require_user(request); return {"channel":channel_to_json(archive_channel(get_channel(cid),u,False))}

@app.post("/api/channels/{cid}/leave")
def post_leave(cid:int,request:Request):
    u=require_user(request); return {"channel":channel_to_json(leave_channel(get_channel(cid),u))}

@app.get("/api/channels/{cid}/members")
def channel_members(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid); db=get_db()
    if c["is_private"] and not db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(): db.close(); raise HTTPException(403,"forbidden")
    rows=db.execute("SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?",(cid,)).fetchall(); db.close()
    return {"members":[user_to_json(x) for x in rows]}

@app.post("/api/channels/{cid}/messages",status_code=201)
async def create_message(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid)
    if c["is_archived"]: raise HTTPException(423,"channel archived")
    db=get_db()
    if not db.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(): db.close(); raise HTTPException(403,"not a channel member")
    b=await request.json(); body=b.get("body")
    if not isinstance(body,str) or not body.strip(): db.close(); raise HTTPException(400,"empty message")
    parent=b.get("parent_id")
    if parent is not None and not db.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0",(parent,cid)).fetchone(): db.close(); raise HTTPException(404,"parent not found")
    ts=now(); cur=db.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,parent,ts)); db.commit()
    r=db.execute("SELECT * FROM messages WHERE id=?",(cur.lastrowid,)).fetchone(); out=message_to_json(r,db); out["mentions"]=resolve_mentions(body,c,u["id"]); db.close()
    eid=emit_event(cid,"message.reply" if parent is not None else "message.created",{"message":out})
    db2=get_db(); db2.execute("UPDATE messages SET event_id=? WHERE id=?",(eid,r["id"])); db2.commit(); db2.close()
    return {"message":out}


def message_to_json(message,db=None):
    own=db is None
    db=db or get_db()
    out=message_json(db,message)
    if own: db.close()
    return out

def encode_cursor(message_id):
    return base64.urlsafe_b64encode(str(int(message_id)).encode()).decode().rstrip("=")

def decode_cursor(cursor):
    try:
        if not cursor: return None
        raw=base64.urlsafe_b64decode(cursor+"="*((4-len(cursor)%4)%4)).decode()
        if not raw.isdigit() or int(raw)<=0: raise ValueError()
        return int(raw)
    except Exception: raise HTTPException(400,"bad cursor")

def read_limit(request):
    try: n=int(request.query_params.get("limit","50"))
    except: raise HTTPException(400,"bad limit")
    if n<1 or n>200: raise HTTPException(400,"bad limit")
    return n

def page_rows(rows,limit,reverse=False):
    more=len(rows)>limit; rows=rows[:limit]
    return rows,encode_cursor(rows[-1]["id"]) if more and rows else None

def list_messages(channel,limit,cursor):
    db=get_db(); q="SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0"
    args=[channel["id"]]
    if cursor is not None: q+=" AND id<?"; args.append(cursor)
    rows=db.execute(q+" ORDER BY id DESC LIMIT ?",args+[limit+1]).fetchall(); rows,nc=page_rows(rows,limit); out=[message_to_json(x,db) for x in rows]; db.close(); return out,nc

def list_replies(parent,limit,cursor):
    db=get_db(); q="SELECT * FROM messages WHERE parent_id=? AND deleted=0"; args=[parent["id"]]
    if cursor is not None: q+=" AND id>?"; args.append(cursor)
    rows=db.execute(q+" ORDER BY id ASC LIMIT ?",args+[limit+1]).fetchall()
    more=len(rows)>limit; rows=rows[:limit]; nc=encode_cursor(rows[-1]["id"]) if more and rows else None
    out=[message_to_json(x,db) for x in rows]; db.close(); return out,nc

def search_messages(user,text,workspace_id,limit,cursor):
    db=get_db(); q="SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ?"; args=[user["id"],"%"+text+"%"]
    if workspace_id is not None: q+=" AND c.workspace_id=?"; args.append(workspace_id)
    if cursor is not None: q+=" AND m.id<?"; args.append(cursor)
    rows=db.execute(q+" ORDER BY m.id DESC LIMIT ?",args+[limit+1]).fetchall(); rows,nc=page_rows(rows,limit); out=[message_to_json(x,db) for x in rows]; db.close(); return out,nc

@app.get("/api/channels/{cid}/messages")
def get_messages(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid); accessible(get_db(),cid,u["id"]); return {"messages":list_messages(c,read_limit(request),decode_cursor(request.query_params.get("cursor")))[0],"next_cursor":list_messages(c,read_limit(request),decode_cursor(request.query_params.get("cursor")))[1]}

@app.get("/api/messages/{mid}/replies")
def get_replies(mid:int,request:Request):
    u=require_user(request); db=get_db(); m=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(mid,)).fetchone(); db.close()
    if not m: raise HTTPException(404,"message not found")
    c=get_channel(m["channel_id"]); accessible(get_db(),c["id"],u["id"]); out,nc=list_replies(m,read_limit(request),decode_cursor(request.query_params.get("cursor"))); return {"replies":out,"next_cursor":nc}

@app.get("/api/search")
def do_search(request:Request,q:str="",workspace_id:Optional[int]=None):
    u=require_user(request); out,nc=search_messages(u,q,workspace_id,read_limit(request),decode_cursor(request.query_params.get("cursor"))); return {"results":out,"next_cursor":nc}

@app.post("/api/messages/{mid}/pin")
def pin(mid:int,request:Request):
    u=require_user(request); db=get_db(); m=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(mid,)).fetchone()
    if not m: db.close(); raise HTTPException(404,"message not found")
    accessible(db,m["channel_id"],u["id"]); ts=now(); db.execute("INSERT OR IGNORE INTO pins VALUES(?,?,?)",(mid,u["id"],ts)); r=db.execute("SELECT * FROM pins WHERE message_id=?",(mid,)).fetchone(); db.commit(); db.close()
    return {"pin":{"message_id":r["message_id"],"pinned_by":r["pinned_by"],"pinned_at":r["pinned_at"]}}

@app.delete("/api/messages/{mid}/pin")
def unpin(mid:int,request:Request):
    u=require_user(request); db=get_db(); m=db.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not m: db.close(); raise HTTPException(404,"message not found")
    accessible(db,m["channel_id"],u["id"]); db.execute("DELETE FROM pins WHERE message_id=?",(mid,)); db.commit(); db.close(); return {"unpinned":True}

@app.get("/api/channels/{cid}/pins")
def get_pins(cid:int,request:Request):
    u=require_user(request); db=get_db(); c=db.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not c: db.close(); raise HTTPException(404,"channel not found")
    accessible(db,cid,u["id"]); cur=decode_cursor(request.query_params.get("cursor")); n=read_limit(request)
    q="SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?"; args=[cid]
    if cur is not None: q+=" AND p.message_id<?"; args.append(cur)
    rows=db.execute(q+" ORDER BY p.message_id DESC LIMIT ?",args+[n+1]).fetchall(); more=len(rows)>n; rows=rows[:n]
    out=[{"message":message_to_json(x,db),"pinned_by":x["pinned_by"],"pinned_at":x["pinned_at"]} for x in rows]; nc=encode_cursor(rows[-1]["message_id"]) if more and rows else None; db.close(); return {"pins":out,"next_cursor":nc}


def get_read_state(user_id,channel_id):
    get_channel(channel_id)
    db=get_db(); r=db.execute("SELECT last_read_event_id FROM read_state WHERE user_id=? AND channel_id=?",(user_id,channel_id)).fetchone()
    last=int(r["last_read_event_id"]) if r else 0
    rows=db.execute("SELECT m.body,m.author_id,m.event_id FROM messages m WHERE m.channel_id=? AND m.parent_id IS NULL AND m.deleted=0 AND m.event_id>? AND m.author_id<>? ORDER BY m.event_id",(channel_id,last,user_id)).fetchall()
    mentions=0
    for m in rows:
        c=db.execute("SELECT * FROM channels WHERE id=?",(channel_id,)).fetchone()
        if user_id in resolve_mentions(m["body"],c,m["author_id"]): mentions+=1
    db.close()
    return {"channel_id":channel_id,"last_read_event_id":last,"unread_count":len(rows),"mention_count":mentions}

def mark_read(user_id,channel_id,event_id):
    get_channel(channel_id)
    try: event_id=max(0,int(event_id))
    except: raise HTTPException(400,"invalid event id")
    db=get_db(); old=db.execute("SELECT last_read_event_id FROM read_state WHERE user_id=? AND channel_id=?",(user_id,channel_id)).fetchone()
    prior=int(old["last_read_event_id"]) if old else 0; value=max(prior,event_id)
    db.execute("INSERT INTO read_state(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id",(user_id,channel_id,value)); db.commit(); db.close()
    return get_read_state(user_id,channel_id)

def get_message(message_id):
    db=get_db(); r=db.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(message_id,)).fetchone(); db.close()
    if not r: raise HTTPException(404,"message not found")
    return r

@app.get("/api/channels/{cid}/read")
def read_channel(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid); db=get_db(); accessible(db,cid,u["id"]); db.close()
    return {"read_state":get_read_state(u["id"],cid)}

@app.post("/api/channels/{cid}/read")
async def post_read(cid:int,request:Request):
    u=require_user(request); c=get_channel(cid); db=get_db(); accessible(db,cid,u["id"]); db.close()
    b=await request.json(); return {"read_state":mark_read(u["id"],cid,b.get("last_read_event_id",0))}
