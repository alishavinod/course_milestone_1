import os, re, json, uuid, base64, hashlib, sqlite3, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn

DB="/app/data/app.db"; PORT=int(os.getenv("PORT","8000")); NODE=int(os.getenv("NODE_ID","0"))
app=FastAPI()
conlock=asyncio.Lock()
clients=[]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def db():
    c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
def init():
    c=db()
    c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,target_id INTEGER,target_username TEXT,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
    """); c.commit(); c.close()
init()
def userobj(r):
    return {k:r[k] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]}
def auth(req):
    h=req.headers.get("authorization","")
    if not h.startswith("Bearer "): return None
    c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone(); c.close(); return r
def j(status,obj): return JSONResponse(obj,status_code=status)
def chobj(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def role(c,w,u): 
    r=c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w,u)).fetchone(); return r["role"] if r else None
def emit(c,cid,typ,payload):
    n=c.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
    c.execute("INSERT INTO events VALUES(?,?,?,?)",(cid,n,typ,json.dumps(payload))); return n
def msgobj(c,r):
    a=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    rr=c.execute("SELECT id FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchall()
    reacts=[]
    for x in c.execute("SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji",(r["id"],)):
        reacts.append({"emoji":x["emoji"],"count":x["n"],"user_ids":[int(v) for v in x["ids"].split(",")]})
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":userobj(a),"body":r["body"],"parent_id":r["parent_id"],"reply_count":len(rr),"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":reacts,"mentions":mentions(c,r["body"],r["channel_id"],r["author_id"])}
def mentions(c,body,cid,author):
    out=set()
    cm=[x["user_id"] for x in c.execute("SELECT user_id FROM channel_members WHERE channel_id=?",(cid,))]
    cr=c.execute("SELECT workspace_id FROM channels WHERE id=?",(cid,)).fetchone()
    if not cr:return []
    for m in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)",body):
        if m=="channel": out.update(x for x in cm if x!=author); continue
        if m=="here": continue
        x=c.execute("SELECT id FROM users WHERE username=?",(m,)).fetchone()
        if x and x["id"]!=author and (x["id"] in cm or role(c,cr["workspace_id"],x["id"])): out.add(x["id"])
    return sorted(out)
def require(req):
    u=auth(req)
    return u if u else None

@app.get("/api/health")
async def health(): return {"status":"ok","node_id":NODE}
@app.post("/api/auth/register")
async def register(req:Request):
    try:d=await req.json()
    except:return j(400,{"error":"bad input"})
    un=d.get("username",""); pw=d.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",un) or len(pw)<8:return j(400,{"error":"invalid"})
    c=db()
    try:
        c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(un,pw,d.get("display_name") or un)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid));c.commit()
    except sqlite3.IntegrityError:c.close();return j(409,{"error":"duplicate"})
    r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();c.close();return j(201,{"user":userobj(r),"token":t})
@app.post("/api/auth/login")
async def login(req:Request):
    d=await req.json();c=db();r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(d.get("username"),d.get("password"))).fetchone()
    if not r:c.close();return j(401,{"error":"invalid"})
    t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"]));c.commit();c.close();return {"user":userobj(r),"token":t}
@app.get("/api/auth/me")
async def me(req:Request):
    u=require(req);return j(200,{"user":userobj(u)}) if u else j(401,{"error":"unauthorized"})
@app.get("/api/users/{uid}")
async def getuser(uid:int,req:Request):
    if not require(req):return j(401,{})
    c=db();r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();c.close();return j(200,{"user":userobj(r)}) if r else j(404,{})
@app.patch("/api/users/me")
async def patchme(req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json(); allowed=["display_name","timezone","avatar_url","status_text","status_emoji"]
    if "timezone" in d and (not isinstance(d["timezone"],str) or len(d["timezone"])>64):return j(400,{})
    c=db(); vals=[d[k] for k in allowed if k in d]
    if vals:c.execute("UPDATE users SET "+",".join(k+"=?" for k in allowed if k in d)+" WHERE id=?",vals+[u["id"]]);c.commit()
    r=c.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone();c.close();return {"user":userobj(r)}
@app.post("/api/workspaces")
async def createws(req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json(); slug=d.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not d.get("name"):return j(400,{})
    c=db()
    try:
        c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,d["name"],u["id"]));wid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general"));cid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit()
    except sqlite3.IntegrityError:c.close();return j(409,{})
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone();ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();c.close();return j(201,{"workspace":dict(w),"general_channel":chobj(ch)})
@app.get("/api/workspaces")
async def listws(req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();rs=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall();c.close();return {"workspaces":[dict(x) for x in rs]}
@app.get("/api/workspaces/{slug}")
async def wsdetail(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w:c.close();return j(404,{})
    if not role(c,w["id"],u["id"]):c.close();return j(403,{})
    inc=req.query_params.get("include_archived") in ("1","true")
    rows=c.execute("SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0)",(w["id"],inc)).fetchall()
    rows=[x for x in rows if not x["is_private"] or c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(x["id"],u["id"])).fetchone()]
    rs=[]
    for x in rows:
        q=c.execute("SELECT COALESCE(last_read_event_id,0) n FROM reads WHERE user_id=? AND channel_id=?",(u["id"],x["id"])).fetchone()
        rs.append({"channel_id":x["id"],"last_read_event_id":q["n"] if q else 0,"unread_count":0,"mention_count":0})
    c.close();return {"workspace":dict(w),"channels":[chobj(x) for x in rows],"read_state":rs}

@app.get("/api/workspaces/{slug}/members")
async def members(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w or not role(c,w["id"],u["id"]):c.close();return j(403,{})
    out=[dict(x) for x in c.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w["id"],))];c.close();return {"members":out}

@app.patch("/api/workspaces/{slug}")
async def patchws(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w:c.close();return j(404,{})
    if role(c,w["id"],u["id"]) not in ("owner","admin"):c.close();return j(403,{})
    if "join_mode" in d and d["join_mode"] not in ("open","invite_only"):c.close();return j(400,{})
    vals=[];sets=[]
    for k in ("name","join_mode"):
        if k in d:sets.append(k+"=?");vals.append(d[k])
    if sets:c.execute("UPDATE workspaces SET "+",".join(sets)+" WHERE id=?",vals+[w["id"]]);c.commit()
    r=c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone();c.close();return {"workspace":dict(r)}

@app.patch("/api/workspaces/{slug}/members/{uid}")
async def patchrole(slug:str,uid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();new=d.get("role");c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();actor=role(c,w["id"],u["id"]) if w else None
    old=role(c,w["id"],uid) if w else None
    if new not in ("owner","admin","member","guest"):c.close();return j(400,{})
    if not w or not actor or actor not in ("owner","admin") or new=="owner" or old=="owner" or (actor=="admin" and old=="admin"):c.close();return j(403 if new!="owner" else 400,{})
    c.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",(new,w["id"],uid));c.commit();c.close();return {"member":{"user_id":uid,"role":new}}

@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w or w["owner_id"]!=u["id"]:c.close();return j(403,{})
    target=role(c,w["id"],d.get("new_owner_id"))
    if target!="admin":c.close();return j(400,{})
    c.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(w["id"],u["id"]));c.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(w["id"],d["new_owner_id"]));c.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(d["new_owner_id"],w["id"]));c.commit();c.close();return {"workspace":dict(c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone()) if False else {"id":w["id"],"owner_id":d["new_owner_id"]}}

@app.post("/api/workspaces/{slug}/invitations")
async def invite(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w or role(c,w["id"],u["id"]) not in ("owner","admin"):c.close();return j(403,{})
    target=None
    if d.get("invited_user_id"):target=d["invited_user_id"]
    elif d.get("invited_username"):
        z=c.execute("SELECT id FROM users WHERE username=?",(d["invited_username"],)).fetchone();target=z["id"] if z else None
    code=uuid.uuid4().hex; exp=None
    if d.get("expires_in_seconds"): exp=str(datetime.now(timezone.utc).timestamp()+int(d["expires_in_seconds"]))
    maxuses=int(d.get("max_uses",1));c.execute("INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,?)",(code,w["id"],u["id"],exp,maxuses,0,target,d.get("invited_username"),0));c.commit();c.close()
    return j(201,{"invitation":{"code":code,"workspace_id":w["id"],"max_uses":maxuses,"uses":0}})
@app.get("/api/workspaces/{slug}/invitations")
async def listinv(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w or role(c,w["id"],u["id"]) not in ("owner","admin"):c.close();return j(403,{})
    rs=c.execute("SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses",(w["id"],)).fetchall();c.close();return {"invitations":[dict(x) for x in rs]}
@app.post("/api/invitations/{code}/accept")
async def accept(code:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();x=c.execute("SELECT * FROM invitations WHERE code=? AND revoked=0",(code,)).fetchone()
    if not x or x["uses"] >= (1 if (x["target_id"] or x["target_username"]) else x["max_uses"]) or (x["expires_at"] and float(x["expires_at"])<datetime.now(timezone.utc).timestamp()):c.close();return j(404,{})
    if (x["target_id"] and x["target_id"]!=u["id"]) or (x["target_username"] and x["target_username"]!=u["username"]):c.close();return j(403,{})
    c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(x["workspace_id"],u["id"],"member"));c.execute("UPDATE invitations SET uses=uses+1 WHERE code=?",(code,));c.commit();c.close();return {"accepted":True}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
async def revoke(slug:str,code:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w or role(c,w["id"],u["id"]) not in ("owner","admin"):c.close();return j(403,{})
    c.execute("UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?",(code,w["id"]));c.commit();c.close();return {"deleted":True}

@app.post("/api/workspaces/{slug}/channels")
async def createch(slug:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();name=d.get("name","");c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();r=role(c,w["id"],u["id"]) if w else None
    if not w or not r or r=="guest" or (d.get("is_private",False) and r not in ("owner","admin")):c.close();return j(403,{})
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name) or len(d.get("topic",""))>250:c.close();return j(400,{})
    if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone():c.close();return j(409,{})
    c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(d.get("is_private"))),d.get("topic","")));cid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();c.close();return j(201,{"channel":chobj(x)})

@app.post("/api/channels/{cid}/join")
async def join(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not x:c.close();return j(404,{})
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(x["workspace_id"],)).fetchone();r=role(c,w["id"],u["id"])
    if x["is_private"] or (r=="guest") or (not r and w["join_mode"]!="open"):c.close();return j(403,{})
    if not r:c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
    c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"]))
    emit(c,cid,"member.joined",{"user_id":u["id"]}); c.commit(); c.close(); return {"channel":chobj(x)}
@app.post("/api/channels/{cid}/leave")
async def leave(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not x:c.close();return j(404,{})
    c.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"]))
    emit(c,cid,"member.left",{"user_id":u["id"]}); c.commit(); c.close(); return {"channel":chobj(x)}
@app.get("/api/channels/{cid}/members")
async def chmembers(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();rs=c.execute("SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?",(cid,)).fetchall();c.close();return {"members":[userobj(x) for x in rs]}
@app.patch("/api/channels/{cid}")
async def patchch(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not x: c.close(); return j(404,{})
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(x["workspace_id"],)).fetchone()
    if len(d.get("topic",""))>250:c.close();return j(400,{})
    if role(c,w["id"],u["id"])!="owner" and not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():c.close();return j(403,{})
    if x["is_archived"]:c.close();return j(423,{})
    c.execute("UPDATE channels SET topic=? WHERE id=?",(d.get("topic",""),cid))
    x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    emit(c,cid,"channel.updated",chobj(x)); c.commit(); c.close(); return {"channel":chobj(x)}
@app.post("/api/channels/{cid}/archive")
async def archive(cid:int,req:Request):
    return await arch(cid,req,1)
@app.post("/api/channels/{cid}/unarchive")
async def unarchive(cid:int,req:Request):
    return await arch(cid,req,0)
async def arch(cid,req,val):
    u=require(req)
    if not u:return j(401,{})
    c=db();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not x:c.close();return j(404,{})
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(x["workspace_id"],)).fetchone()
    if w["owner_id"]!=u["id"]:c.close();return j(403,{})
    c.execute("UPDATE channels SET is_archived=? WHERE id=?",(val,cid))
    x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    emit(c,cid,"channel.archived" if val else "channel.unarchived",chobj(x)); c.commit(); c.close()
    return {"channel":chobj(x)}

def cursorval(s):
    try:return int(base64.urlsafe_b64decode(s+"==").decode())
    except:raise ValueError()
def makecursor(i):return base64.urlsafe_b64encode(str(i).encode()).decode().rstrip("=")
def read_counts(c,uid,cid,last):
    rows=c.execute("SELECT type,payload FROM events WHERE channel_id=? AND event_id>?",(cid,last)).fetchall()
    unread=0; mention=0
    for row in rows:
        if row["type"]!="message.created": continue
        unread += 1
        try:
            msg=json.loads(row["payload"])
            if int(uid) in [int(x) for x in msg.get("mentions",[])]: mention += 1
        except Exception: pass
    return unread,mention
@app.post("/api/channels/{cid}/messages")
async def postmsg(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();body=d.get("body","")
    if not isinstance(body,str) or not body.strip():return j(400,{})
    if body.startswith("/shrug"):body=(body[6:].strip()+" ¯\\_(ツ)_/¯").strip()
    c=db();x=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not x:return j(404,{})
    if x["is_archived"]:c.close();return j(423,{})
    if not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone() and c.execute("SELECT 1 FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE w.id=? AND m.user_id=?",(x["workspace_id"],u["id"])).fetchone() is None:c.close();return j(403,{})
    parent=d.get("parent_id")
    if parent and not c.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0",(parent,cid)).fetchone():c.close();return j(400,{})
    t=now();c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,parent,t));mid=c.execute("SELECT last_insert_rowid()").fetchone()[0];r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();m=msgobj(c,r);eid=emit(c,cid,"message.reply" if parent else "message.created",m);c.commit();c.close();return j(201,{"message":m})
@app.get("/api/channels/{cid}/messages")
async def listmsg(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    try:lim=int(req.query_params.get("limit","50")); start=cursorval(req.query_params["cursor"]) if "cursor" in req.query_params else 9223372036854775807
    except: return j(400,{})
    if lim<1 or lim>200:return j(400,{})
    c=db();
    if not c.execute("SELECT 1 FROM channels WHERE id=?",(cid,)).fetchone(): c.close(); return j(404,{})
    rs=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?",(cid,start,lim+1)).fetchall();more=len(rs)>lim;rs=rs[:lim];out=[msgobj(c,x) for x in rs];nc=makecursor(rs[-1]["id"]) if more and rs else None;c.close();return {"messages":out,"next_cursor":nc}
@app.patch("/api/messages/{mid}")
async def editmsg(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r or r["deleted"]:c.close();return j(409 if r else 404,{})
    if r["author_id"]!=u["id"]:c.close();return j(403,{})
    if not d.get("body","").strip():c.close();return j(400,{})
    c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(d["body"],now(),mid));c.commit();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();m=msgobj(c,r);c.close();return {"message":m}
@app.delete("/api/messages/{mid}")
async def delmsg(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r:c.close();return j(404,{})
    w=c.execute("SELECT owner_id FROM workspaces w JOIN channels x ON x.workspace_id=w.id WHERE x.id=?",(r["channel_id"],)).fetchone()
    if r["author_id"]!=u["id"] and w["owner_id"]!=u["id"]:c.close();return j(403,{})
    c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,));c.commit();c.close();return {"deleted":True}
@app.get("/api/messages/{mid}/replies")
async def replies(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    try:lim=int(req.query_params.get("limit","50"));start=cursorval(req.query_params["cursor"]) if "cursor" in req.query_params else 0
    except:return j(400,{})
    if lim<1 or lim>200:return j(400,{})
    c=db();
    if not c.execute("SELECT 1 FROM messages WHERE id=?",(mid,)).fetchone(): c.close(); return j(404,{})
    rs=c.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?",(mid,start,lim+1)).fetchall();more=len(rs)>lim;rs=rs[:lim];out=[msgobj(c,x) for x in rs];nc=makecursor(rs[-1]["id"]) if more and rs else None;c.close();return {"replies":out,"next_cursor":nc}
@app.get("/api/search")
async def search(req:Request):
    u=require(req)
    if not u:return j(401,{})
    q=req.query_params.get("q","")
    try: lim=int(req.query_params.get("limit","50")); cur=cursorval(req.query_params["cursor"]) if "cursor" in req.query_params else 9223372036854775807
    except: return j(400,{})
    if lim<1 or lim>200:return j(400,{})
    c=db(); wid=req.query_params.get("workspace_id")
    sql="SELECT m.* FROM messages m JOIN channel_members cm ON cm.channel_id=m.channel_id JOIN channels ch ON ch.id=m.channel_id WHERE cm.user_id=? AND m.deleted=0 AND m.body LIKE ? AND m.id<?"
    args=[u["id"],"%"+q+"%",cur]
    if wid: sql+=" AND ch.workspace_id=?"; args.append(int(wid))
    sql+=" ORDER BY m.id DESC LIMIT ?"; args.append(lim+1)
    rs=c.execute(sql,args).fetchall(); more=len(rs)>lim; rs=rs[:lim]
    out=[{"message":msgobj(c,x)} for x in rs]
    nc=makecursor(rs[-1]["id"]) if more and rs else None
    c.close(); return {"results":out,"next_cursor":nc}
@app.post("/api/messages/{mid}/reactions")
async def react(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db();c.execute("INSERT OR IGNORE INTO reactions VALUES(?,?,?)",(mid,u["id"],d.get("emoji","")));c.commit();r=c.execute("SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji",(mid,)).fetchall();c.close();return {"reactions":[{"emoji":x["emoji"],"count":x["n"],"user_ids":[int(v) for v in x["ids"].split(",")]} for x in r]}
@app.delete("/api/messages/{mid}/reactions/{emoji}")
async def unreact(mid:int,emoji:str,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();c.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],emoji));c.commit();r=c.execute("SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji",(mid,)).fetchall();c.close();return {"reactions":[{"emoji":x["emoji"],"count":x["n"],"user_ids":[int(v) for v in x["ids"].split(",")]} for x in r]}
@app.get("/api/channels/{cid}/read")
async def read(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db()
    if not c.execute("SELECT 1 FROM channels WHERE id=?",(cid,)).fetchone(): c.close(); return j(404,{})
    r=c.execute("SELECT COALESCE(last_read_event_id,0) n FROM reads WHERE user_id=? AND channel_id=?",(u["id"],cid)).fetchone()
    last=r["n"] if r else 0
    unread,mention=read_counts(c,u["id"],cid,last)
    c.close()
    return {"read_state":{"channel_id":cid,"last_read_event_id":last,"unread_count":unread,"mention_count":mention}}
@app.post("/api/channels/{cid}/read")
async def markread(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    d=await req.json();c=db()
    if not c.execute("SELECT 1 FROM channels WHERE id=?",(cid,)).fetchone(): c.close(); return j(404,{})
    old=c.execute("SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?",(u["id"],cid)).fetchone()
    v=max(int(d.get("last_read_event_id",0)),old["last_read_event_id"] if old else 0)
    c.execute("INSERT INTO reads VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id",(u["id"],cid,v));c.commit()
    unread,mention=read_counts(c,u["id"],cid,v);c.close()
    return {"read_state":{"channel_id":cid,"last_read_event_id":v,"unread_count":unread,"mention_count":mention}}
@app.post("/api/messages/{mid}/pin")
async def pin(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();r=c.execute("SELECT m.*,x.is_archived FROM messages m JOIN channels x ON x.id=m.channel_id WHERE m.id=?",(mid,)).fetchone()
    if not r:c.close();return j(404,{})
    if r["is_archived"]:c.close();return j(423,{})
    c.execute("INSERT OR IGNORE INTO pins VALUES(?,?,?)",(mid,u["id"],now()));c.commit();p=c.execute("SELECT * FROM pins WHERE message_id=?",(mid,)).fetchone();c.close();return {"pin":{"message_id":mid,"pinned_by":p["pinned_by"],"pinned_at":p["pinned_at"]}}
@app.delete("/api/messages/{mid}/pin")
async def unpin(mid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    c=db();c.execute("DELETE FROM pins WHERE message_id=?",(mid,));c.commit();c.close();return {"unpinned":True}
@app.get("/api/channels/{cid}/pins")
async def pins(cid:int,req:Request):
    u=require(req)
    if not u:return j(401,{})
    try:
        lim=int(req.query_params.get("limit","50"))
        cur=cursorval(req.query_params["cursor"]) if "cursor" in req.query_params else 9223372036854775807
    except: return j(400,{})
    if lim<1 or lim>200:return j(400,{})
    c=db()
    ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch:c.close();return j(404,{})
    w=c.execute("SELECT workspace_id FROM channels WHERE id=?",(cid,)).fetchone()
    if ch["is_private"] and not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():c.close();return j(403,{})
    rs=c.execute("SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND p.message_id<? ORDER BY p.message_id DESC LIMIT ?",(cid,cur,lim+1)).fetchall()
    more=len(rs)>lim;rs=rs[:lim];out=[{"message":msgobj(c,x),"pinned_by":x["pinned_by"],"pinned_at":x["pinned_at"]} for x in rs]
    nc=makecursor(rs[-1]["message_id"]) if more and rs else None
    c.close();return {"pins":out,"next_cursor":nc}
@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
    tok=ws.query_params.get("token",""); c=db(); u=c.execute("SELECT user_id FROM tokens WHERE token=?",(tok,)).fetchone(); c.close()
    if not u: await ws.close(code=1008); return
    await ws.accept(); subs=set(); last={}
    try:
        while True:
            try:
                data=await asyncio.wait_for(ws.receive_json(),.15)
                if data.get("type") in ("subscribe","resume"):
                    cid=int(data["channel_id"]); subs.add(cid); c=db()
                    head=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
                    since=int(data.get("since_event_id",0))
                    if data["type"]=="resume":
                        evs=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall()
                        for ev in evs:
                            z={"type":ev["type"],"event_id":ev["event_id"],"channel_id":cid}
                            z["message" if ev["type"] in ("message.created","message.reply") else "data"]=json.loads(ev["payload"])
                            await ws.send_json(z)
                        last[cid]=evs[-1]["event_id"] if evs else since
                    else: last[cid]=head
                    c.close()
                    await ws.send_json({"type":"subscribed" if data["type"]=="subscribe" else "resumed","channel_id":cid,"head_event_id":head})
            except asyncio.TimeoutError: pass
            c=db()
            for cid in list(subs):
                rs=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last.get(cid,0))).fetchall()
                for ev in rs:
                    z={"type":ev["type"],"event_id":ev["event_id"],"channel_id":cid}
                    z["message" if ev["type"] in ("message.created","message.reply") else "data"]=json.loads(ev["payload"])
                    await ws.send_json(z); last[cid]=ev["event_id"]
            c.close()
    except (WebSocketDisconnect,Exception): return

@app.get("/")
async def root(): return HTMLResponse("<!doctype html><html><body><h1>Huddle</h1><div id='app'>Huddle Chat</div></body></html>")
if __name__=="__main__": uvicorn.run(app,host="127.0.0.1",port=PORT)
