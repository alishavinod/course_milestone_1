import os, re, sqlite3, secrets, hashlib, time, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn

DB = "/app/data/app.db"
NODE = int(os.getenv("NODE_ID", "0"))
app = FastAPI()

def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def conn():
    os.makedirs("/app/data", exist_ok=True)
    c=sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c
def init():
    c=conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,invited_username TEXT,revoked INTEGER DEFAULT 0);
    """)
    c.commit(); c.close()
init()

def response(data,status=200): return JSONResponse(data,status_code=status)
def body_error(status,msg="invalid request"): return response({"error":msg},status)
def user_obj(r):
    return {"id":r["id"],"username":r["username"],"display_name":r["display_name"] or r["username"],
            "timezone":"UTC","avatar_url":"","status_text":"","status_emoji":""}
def get_user_token(req):
    h=req.headers.get("authorization","")
    if not h.lower().startswith("bearer "): return None
    t=h[7:].strip()
    c=conn(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone(); c.close()
    return r
async def auth(req):
    u=get_user_token(req)
    if not u: return None
    return u
def member(c,wid,uid):
    return c.execute("SELECT * FROM members WHERE workspace_id=? AND user_id=?",(wid,uid)).fetchone()
def workspace(c,slug):
    return c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
def ws_obj(r): return {"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":r["join_mode"]}
def channel_obj(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"] or "","is_archived":bool(r["is_archived"])}
def parse_json(req):
    return req.json()
def role_allowed(role, vals): return role in vals

@app.get("/api/health")
async def health(): return {"status":"ok","node_id":NODE}

@app.post("/api/auth/register")
async def register(req:Request):
    try: d=await req.json()
    except: return body_error(400)
    un=d.get("username"); pw=d.get("password")
    if not isinstance(un,str) or not re.fullmatch(r"[A-Za-z0-9_]+",un) or not isinstance(pw,str) or len(pw)<8: return body_error(400)
    c=conn()
    try:
        cur=c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(un,hashlib.sha256(pw.encode()).hexdigest(),d.get("display_name") or un)); uid=cur.lastrowid
        tok=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); c.commit()
        r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); return response({"user":user_obj(r),"token":tok},201)
    except sqlite3.IntegrityError: return body_error(409,"duplicate username")
    finally: c.close()

@app.post("/api/auth/login")
async def login(req:Request):
    try: d=await req.json()
    except: return body_error(400)
    c=conn(); r=c.execute("SELECT * FROM users WHERE username=?",(d.get("username"),)).fetchone()
    if not r or hashlib.sha256(str(d.get("password","")).encode()).hexdigest()!=r["password"]: c.close(); return body_error(401,"invalid credentials")
    tok=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(tok,r["id"])); c.commit(); c.close()
    return {"user":user_obj(r),"token":tok}

@app.get("/api/auth/me")
async def me(req:Request):
    u=await auth(req)
    return {"user":user_obj(u)} if u else body_error(401)

@app.get("/api/workspaces")
async def list_ws(req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn(); rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
    return {"workspaces":[ws_obj(x) for x in rows]}

@app.post("/api/workspaces")
async def create_ws(req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    slug=d.get("slug"); name=d.get("name")
    if not isinstance(slug,str) or not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not isinstance(name,str) or not name:return body_error(400)
    c=conn()
    try:
        cur=c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,name,u["id"])); wid=cur.lastrowid
        c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner")); cur=c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=cur.lastrowid; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
        w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); return response({"workspace":ws_obj(w),"general_channel":channel_obj(ch)},201)
    except sqlite3.IntegrityError:return body_error(409)
    finally:c.close()

@app.get("/api/workspaces/{slug}/members")
async def members(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn(); w=workspace(c,slug)
    if not w:return body_error(404)
    if not member(c,w["id"],u["id"]):return body_error(403)
    rows=c.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w["id"],)).fetchall(); c.close()
    return {"members":[dict(x) for x in rows]}

@app.get("/api/workspaces/{slug}")
async def workspace_detail(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn(); w=workspace(c,slug)
    if not w:return body_error(404)
    if not member(c,w["id"],u["id"]):return body_error(403)
    rows=c.execute("SELECT * FROM channels WHERE workspace_id=? AND is_archived=0",(w["id"],)).fetchall(); c.close()
    return {"workspace":ws_obj(w),"channels":[channel_obj(x) for x in rows],"read_state":{}}

@app.patch("/api/workspaces/{slug}")
async def patch_ws(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    c=conn(); w=workspace(c,slug)
    if not w:return body_error(404)
    m=member(c,w["id"],u["id"])
    if not m or m["role"] not in ("owner","admin"):return body_error(403)
    if "join_mode" in d and d["join_mode"] not in ("open","invite_only"):return body_error(400)
    fields=[]; vals=[]
    for k in ("name","join_mode"):
        if k in d: fields.append(k+"=?"); vals.append(d[k])
    if fields:c.execute("UPDATE workspaces SET "+",".join(fields)+" WHERE id=?",(*vals,w["id"]));c.commit()
    r=c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone();c.close();return {"workspace":ws_obj(r)}

@app.post("/api/workspaces/{slug}/channels")
async def create_channel(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    c=conn();w=workspace(c,slug)
    if not w:return body_error(404)
    m=member(c,w["id"],u["id"])
    if not m:return body_error(403)
    private=bool(d.get("is_private",False))
    if m["role"]=="guest" or (private and m["role"] not in ("owner","admin")):return body_error(403)
    name=d.get("name")
    if not isinstance(name,str) or not re.fullmatch(r"[a-z0-9-]{1,32}",name):return body_error(400)
    if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone():return body_error(409)
    try:
        cur=c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(private),d.get("topic","")));cid=cur.lastrowid;c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit();r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();return response({"channel":channel_obj(r)},201)
    finally:c.close()

@app.post("/api/channels/{cid}/join")
async def join(cid:int,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn();ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch:return body_error(404)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(ch["workspace_id"],)).fetchone();m=member(c,w["id"],u["id"])
    if ch["is_private"] or (m and m["role"]=="guest") or (not m and w["join_mode"]!="open"):return body_error(403)
    if not m:c.execute("INSERT INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
    c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit();r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();c.close();return {"channel":channel_obj(r)}

@app.patch("/api/workspaces/{slug}/members/{uid}")
async def patch_member(slug:str,uid:int,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    role=d.get("role")
    if role not in ("owner","admin","member","guest"):return body_error(400)
    if role=="owner":return body_error(400)
    c=conn();w=workspace(c,slug);actor=member(c,w["id"],u["id"]) if w else None; target=member(c,w["id"],uid) if w else None
    if not w or not target:return body_error(404)
    if not actor or actor["role"] not in ("owner","admin") or target["role"]=="owner" or (actor["role"]=="admin" and target["role"]=="admin"):return body_error(403)
    c.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",(role,w["id"],uid));c.commit();c.close();return {"member":{"user_id":uid,"role":role}}

@app.post("/api/workspaces/{slug}/invitations")
async def create_inv(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    c=conn();w=workspace(c,slug);m=member(c,w["id"],u["id"]) if w else None
    if not w:return body_error(404)
    if not m or m["role"] not in ("owner","admin"):return body_error(403)
    code=secrets.token_urlsafe(16); exp=d.get("expires_in_seconds"); exp=time.time()+int(exp) if exp is not None else None; maxu=int(d.get("max_uses",1))
    iid=d.get("invited_user_id"); iu=d.get("invited_username")
    c.execute("INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,?)",(code,w["id"],u["id"],exp,maxu,0,iid,iu,0));c.commit()
    r=c.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone();c.close();return response({"invitation":dict(r)},201)

@app.post("/api/invitations/{code}/accept")
async def accept(code:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn();i=c.execute("SELECT * FROM invitations WHERE code=?",(code,)).fetchone()
    if not i or i["revoked"] or i["uses"]>=i["max_uses"] or (i["expires_at"] and time.time()>i["expires_at"]):return body_error(404)
    if (i["invited_user_id"] and i["invited_user_id"]!=u["id"]) or (i["invited_username"] and i["invited_username"]!=u["username"]):return body_error(403)
    c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(i["workspace_id"],u["id"],"member"));c.execute("UPDATE invitations SET uses=uses+1 WHERE code=?",(code,));c.commit();c.close();return {"accepted":True}

@app.get("/api/workspaces/{slug}/invitations")
async def list_inv(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn();w=workspace(c,slug);m=member(c,w["id"],u["id"]) if w else None
    if not w:return body_error(404)
    if not m or m["role"] not in ("owner","admin"):return body_error(403)
    rows=c.execute("SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at>?)",(w["id"],time.time())).fetchall();c.close();return {"invitations":[dict(x) for x in rows]}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
async def revoke(slug:str,code:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    c=conn();w=workspace(c,slug);m=member(c,w["id"],u["id"]) if w else None
    if not w:return body_error(404)
    if not m or m["role"] not in ("owner","admin"):return body_error(403)
    c.execute("UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?",(code,w["id"]));c.commit();c.close();return {"deleted":True}

@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,req:Request):
    u=await auth(req)
    if not u:return body_error(401)
    try:d=await req.json()
    except:return body_error(400)
    c=conn();w=workspace(c,slug);a=member(c,w["id"],u["id"]) if w else None;t=member(c,w["id"],d.get("new_owner_id")) if w else None
    if not w:return body_error(404)
    if not a or a["role"]!="owner":return body_error(403)
    if not t or t["role"]!="admin":return body_error(400)
    c.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(w["id"],u["id"]));c.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(w["id"],d["new_owner_id"]));c.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(d["new_owner_id"],w["id"]));c.commit();c.close();return {"workspace":ws_obj(dict(w))}

@app.websocket("/api/ws")
async def ws(sock:WebSocket):
    tok=sock.query_params.get("token","")
    c=conn();r=c.execute("SELECT 1 FROM tokens WHERE token=?",(tok,)).fetchone();c.close()
    if not r: await sock.close(code=1008); return
    await sock.accept()
    try:
        while True: await sock.receive_text()
    except: pass

if __name__=="__main__":
    init();uvicorn.run(app,host="127.0.0.1",port=int(os.getenv("PORT","8000")),log_level="warning")
