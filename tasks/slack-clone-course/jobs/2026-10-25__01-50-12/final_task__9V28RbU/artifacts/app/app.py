import os, re, sqlite3, hashlib, secrets, time, json
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from contextlib import contextmanager

DB="/app/data/app.db"
os.makedirs("/app/data", exist_ok=True)
app=FastAPI()

@contextmanager
def get_db():
    c=sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    try: yield c
    finally: c.close()

def init():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
        """); c.commit()
init()

def err(status, detail): raise HTTPException(status, detail)
def hash_password(password): return hashlib.sha256(("huddle:"+password).encode()).hexdigest()
def rowdict(r): return dict(r) if r else None
def user_to_json(u):
    u=rowdict(u)
    if not u:return None
    return {k:u.get(k) for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def create_user(username,password,display_name=None):
    if not isinstance(username,str) or not re.fullmatch(r"[A-Za-z0-9_]+",username) or not isinstance(password,str) or len(password)<8: err(400,"invalid input")
    with get_db() as c:
        try:
            cur=c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(username,hash_password(password),display_name or username)); c.commit()
        except sqlite3.IntegrityError: err(409,"username exists")
        return c.execute("SELECT * FROM users WHERE id=?",(cur.lastrowid,)).fetchone()
def issue_token(uid):
    t=secrets.token_urlsafe(32)
    with get_db() as c:c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid));c.commit()
    return t
async def body(request):
    try:return await request.json()
    except: return {}
def require_user(request):
    h=request.headers.get("authorization","")
    if not h.startswith("Bearer "): err(401,"unauthorized")
    with get_db() as c:
        u=c.execute("SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",(h[7:],)).fetchone()
    if not u:err(401,"unauthorized")
    return u
def get_user(uid):
    with get_db() as c:return c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
def get_workspace(slug):
    with get_db() as c:r=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not r:err(404,"workspace not found")
    return r
def get_role(wid,uid):
    with get_db() as c:r=c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(wid,uid)).fetchone()
    return r["role"] if r else None
def add_member(wid,uid,role="member"):
    with get_db() as c:c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(wid,uid,role));c.commit()
def workspace_to_json(w): return {k:w[k] for k in ("id","slug","name","owner_id","join_mode")}
def channel_to_json(ch):
    return {k:(bool(ch[k]) if k in ("is_private","is_dm","is_archived") else ch[k]) for k in ("id","workspace_id","name","is_private","is_dm","topic","is_archived")}
def create_channel(wid,name,is_private,creator_id,topic=""):
    if not isinstance(name,str) or not re.fullmatch(r"[a-z0-9-]{1,32}",name):err(400,"invalid channel name")
    with get_db() as c:
        if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(wid,name)).fetchone():err(409,"channel exists")
        x=c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(wid,name,int(bool(is_private)),topic)).lastrowid
        c.execute("INSERT INTO channel_members VALUES(?,?)",(x,creator_id));c.commit()
        return c.execute("SELECT * FROM channels WHERE id=?",(x,)).fetchone()
def join_channel(ch,user):
    wid=ch["workspace_id"]; role=get_role(wid,user["id"])
    with get_db() as c:w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone()
    with get_db() as c:
        in_channel = c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch["id"],user["id"])).fetchone()
    if ch["is_private"] and not in_channel:err(403,"private channel")
    if not role:
        if w["join_mode"]!="open":err(403,"invite required")
        add_member(wid,user["id"],"member"); role="member"
    if role=="guest":err(403,"guest cannot join")
    with get_db() as c:c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch["id"],user["id"]));c.commit()
def set_role(workspace,actor_id,target_id,new_role):
    if new_role not in ("admin","member","guest"):err(400,"invalid role")
    ar=get_role(workspace["id"],actor_id); tr=get_role(workspace["id"],target_id)
    if new_role=="owner" or not tr:err(400,"invalid role") if new_role=="owner" else err(404,"member")
    if target_id==workspace["owner_id"] or actor_id==target_id:err(403,"protected")
    if ar=="owner": pass
    elif ar=="admin" and tr!="admin": pass
    else:err(403,"forbidden")
    with get_db() as c:c.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",(new_role,workspace["id"],target_id));c.commit()
    return {"user_id":target_id,"role":new_role}
def can_create_channel(role,is_private): return role in ("owner","admin") or (role=="member" and not is_private)
def can_join_channel(role): return role!="guest"

def update_workspace(w,fields):
    name=fields.get("name",w["name"]); jm=fields.get("join_mode",w["join_mode"])
    if jm not in ("open","invite_only"):err(400,"invalid join mode")
    with get_db() as c:c.execute("UPDATE workspaces SET name=?,join_mode=? WHERE id=?",(name,jm,w["id"]));c.commit()
    return get_workspace(w["slug"])
def create_invitation(wid,creator,expires,max_uses,target):
    if expires is None:expires=86400
    if max_uses is None:max_uses=1
    try: expires=float(expires);max_uses=int(max_uses)
    except:err(400,"invalid invitation")
    code=secrets.token_urlsafe(18)
    with get_db() as c:c.execute("INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)",(code,wid,creator,time.time()+expires,max_uses,0,target));c.commit()
    return invitation_json(code)
def invitation_json(code):
    with get_db() as c:r=c.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone()
    d=rowdict(r); return {"code":d["code"],"workspace_id":d["workspace_id"],"created_by":d["created_by"],"expires_at":d["expires_at"],"max_uses":d["max_uses"],"uses":d["uses"],"invited_user_id":d["invited_user_id"]}
def accept_invitation(code,user):
    with get_db() as c:
        r=c.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone()
        if not r or r["revoked"] or r["uses"]>=r["max_uses"] or r["expires_at"]<time.time():err(404,"invitation not found")
        if r["invited_user_id"] is not None and r["invited_user_id"]!=user["id"]:err(403,"targeted invitation")
        cur=c.execute("UPDATE invitations SET uses=uses+1 WHERE code=? AND uses<max_uses AND revoked=0 AND expires_at>?",(code,time.time()))
        if cur.rowcount!=1:err(404,"invitation not found")
        c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(r["workspace_id"],user["id"],"member"));c.commit()
    return invitation_json(code)
def list_invitations(w):
    with get_db() as c:rs=c.execute("SELECT code FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND expires_at>?",(w["id"],time.time())).fetchall()
    return [invitation_json(x["code"]) for x in rs]
def revoke_invitation(w,code):
    with get_db() as c:c.execute("UPDATE invitations SET revoked=1 WHERE workspace_id=? AND code=?",(w["id"],code));c.commit()
def transfer_ownership(w,actor,new):
    if actor!=w["owner_id"]:err(403,"owner only")
    if get_role(w["id"],new)!="admin":err(400,"target must be admin")
    with get_db() as c:
        c.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(new,w["id"]))
        c.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(w["id"],actor))
        c.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(w["id"],new));c.commit()
    return get_workspace(w["slug"])

@app.get("/api/health")
def health(): return {"status":"ok","node_id":int(os.getenv("NODE_ID","0"))}
@app.post("/api/auth/register")
async def register(r:Request):
    d=await body(r);u=create_user(d.get("username"),d.get("password"),d.get("display_name"));return JSONResponse({"user":user_to_json(u),"token":issue_token(u["id"])},201)
@app.post("/api/auth/login")
async def login(r:Request):
    d=await body(r)
    with get_db() as c:u=c.execute("SELECT * FROM users WHERE username=?",(d.get("username"),)).fetchone()
    if not u or hash_password(d.get("password",""))!=u["password"]:err(401,"invalid credentials")
    return {"user":user_to_json(u),"token":issue_token(u["id"])}
@app.get("/api/auth/me")
def me(r:Request):return {"user":user_to_json(require_user(r))}
@app.get("/api/workspaces")
def workspaces(r:Request):
    u=require_user(r)
    with get_db() as c:rs=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall()
    return {"workspaces":[workspace_to_json(x) for x in rs]}
@app.post("/api/workspaces")
async def create_ws(r:Request):
    u=require_user(r);d=await body(r);slug=d.get("slug")
    if not isinstance(slug,str) or not re.fullmatch(r"[a-z0-9-]{2,32}",slug):err(400,"invalid slug")
    with get_db() as c:
        if c.execute("SELECT 1 FROM workspaces WHERE slug=?",(slug,)).fetchone():err(409,"exists")
        wid=c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,d.get("name",""),u["id"])).lastrowid
        c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"));c.commit()
    ch=create_channel(wid,"general",False,u["id"])
    return JSONResponse({"workspace":workspace_to_json(get_workspace(slug)),"general_channel":channel_to_json(ch)},201)
@app.get("/api/workspaces/{slug}/members")
def members(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if not get_role(w["id"],u["id"]):err(403,"not member")
    with get_db() as c:rs=c.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w["id"],)).fetchall()
    return {"members":[dict(x) for x in rs]}
@app.get("/api/workspaces/{slug}")
def workspace_detail(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if not get_role(w["id"],u["id"]):err(403,"not member")
    with get_db() as c:cs=c.execute("SELECT * FROM channels WHERE workspace_id=? AND is_archived=0",(w["id"],)).fetchall()
    return {"workspace":workspace_to_json(w),"channels":[channel_to_json(x) for x in cs]}
@app.patch("/api/workspaces/{slug}")
async def patch_ws(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if u["id"]!=w["owner_id"]:err(403,"owner only")
    return {"workspace":workspace_to_json(update_workspace(w,await body(r)))}
@app.post("/api/workspaces/{slug}/channels")
async def add_channel(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug);d=await body(r);priv=bool(d.get("is_private",False));role=get_role(w["id"],u["id"])
    if not role:err(403,"not member")
    if not can_create_channel(role,priv):err(403,"forbidden")
    return JSONResponse({"channel":channel_to_json(create_channel(w["id"],d.get("name"),priv,u["id"],d.get("topic","")))},201)
@app.post("/api/channels/{cid}/join")
def join(cid:int,r:Request):
    u=require_user(r)
    with get_db() as c:ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch:err(404,"channel")
    join_channel(ch,u);return {"channel":channel_to_json(ch)}
@app.patch("/api/workspaces/{slug}/members/{uid}")
async def patch_member(slug:str,uid:int,r:Request):
    u=require_user(r);w=get_workspace(slug);d=await body(r);return {"member":set_role(w,u["id"],uid,d.get("role"))}
@app.post("/api/workspaces/{slug}/invitations")
async def invite(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"):err(403,"forbidden")
    d=await body(r);return JSONResponse({"invitation":create_invitation(w["id"],u["id"],d.get("expires_in_seconds"),d.get("max_uses"),d.get("invited_user_id"))},201)
@app.get("/api/workspaces/{slug}/invitations")
def invites(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"):err(403,"forbidden")
    return {"invitations":list_invitations(w)}
@app.delete("/api/workspaces/{slug}/invitations/{code}")
def del_inv(slug:str,code:str,r:Request):
    u=require_user(r);w=get_workspace(slug)
    if get_role(w["id"],u["id"]) not in ("owner","admin"):err(403,"forbidden")
    revoke_invitation(w,code);return {"deleted":True}
@app.post("/api/invitations/{code}/accept")
def accept(code:str,r:Request):
    u=require_user(r);return {"invitation":accept_invitation(code,u)}
@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,r:Request):
    u=require_user(r);w=get_workspace(slug);d=await body(r);return {"workspace":workspace_to_json(transfer_ownership(w,u["id"],d.get("new_owner_id")))}
@app.websocket("/api/ws")
async def ws(sock:WebSocket):
    tok=sock.query_params.get("token","")
    with get_db() as c:u=c.execute("SELECT user_id FROM tokens WHERE token=?",(tok,)).fetchone()
    if not u: await sock.close(code=1008); return
    await sock.accept()
    try:
        while True: await sock.receive_text()
    except WebSocketDisconnect: pass

@app.exception_handler(HTTPException)
async def http_exc(request,exc): return JSONResponse({"error":exc.detail},status_code=exc.status_code)
@app.exception_handler(Exception)
async def all_exc(request,exc): return JSONResponse({"error":"bad request"},status_code=400)
