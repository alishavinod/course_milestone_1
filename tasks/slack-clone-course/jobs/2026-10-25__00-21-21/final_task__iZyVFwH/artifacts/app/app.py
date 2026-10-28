import os, re, json, sqlite3, hashlib, secrets, time, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

DB="/app/data.db"; os.makedirs("/app",exist_ok=True)
app=FastAPI()
NODE=int(os.getenv("NODE_ID","0"))

def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def db():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c
def init():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,target INTEGER,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    """); c.commit(); c.close()
init()

def err(code,msg="error"): return JSONResponse({"error":msg},status_code=code)
def userobj(r):
    return {k:r[k] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]}
def auth(req):
    h=req.headers.get("authorization","")
    if not h.lower().startswith("bearer "): return None
    c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h.split(None,1)[1],)).fetchone(); c.close(); return r
def member(c,w,u): return c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w,u)).fetchone()
def channel(c,i): return c.execute("SELECT * FROM channels WHERE id=?",(i,)).fetchone()
def chobj(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def wsobj(r): return {k:r[k] for k in ["id","slug","name","owner_id","join_mode"]}
def cursor(v): return str(v)
def page_params(req):
    try: lim=int(req.query_params.get("limit","50"))
    except: return None,None
    if lim<1 or lim>200:return None,None
    cur=req.query_params.get("cursor")
    if cur is not None and (not cur.isdigit()): return None,None
    return lim, int(cur) if cur else None
def emit(c,cid,kind,payload):
    n=c.execute("SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
    c.execute("INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)",(cid,n,kind,json.dumps(payload))); return n
def reactions(c,mid):
    rows=c.execute("SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id",(mid,)).fetchall()
    d={}
    for r in rows:d.setdefault(r["emoji"],[]).append(r["user_id"])
    return [{"emoji":e,"count":len(ids),"user_ids":ids} for e,ids in d.items()]
def msgobj(c,r):
    au=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    rc=c.execute("SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()[0]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":userobj(au),"body":r["body"],"parent_id":r["parent_id"],"reply_count":rc,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":reactions(c,r["id"]),"mentions":[]}

@app.get("/api/health")
async def health(): return {"status":"ok","node_id":NODE}

@app.post("/api/auth/register")
async def register(req:Request):
    try:b=await req.json()
    except:return err(400)
    u=b.get("username",""); p=b.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8:return err(400)
    c=db()
    try:
        cur=c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(u,p,b.get("display_name") or u)); uid=cur.lastrowid
    except sqlite3.IntegrityError:c.close();return err(409)
    t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid));c.commit(); r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();c.close()
    return JSONResponse({"user":userobj(r),"token":t},201)
@app.post("/api/auth/login")
async def login(req:Request):
    try:b=await req.json()
    except:return err(400)
    c=db();r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(b.get("username"),b.get("password"))).fetchone()
    if not r:c.close();return err(401)
    t=secrets.token_urlsafe(24);c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"]));c.commit();c.close();return {"user":userobj(r),"token":t}
@app.get("/api/auth/me")
async def me(req:Request):
    u=auth(req)
    return {"user":userobj(u)} if u else err(401)

@app.get("/api/users/{uid}")
async def getuser(uid:int,req:Request):
    if not auth(req):return err(401)
    c=db();r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();c.close();return {"user":userobj(r)} if r else err(404)
@app.patch("/api/users/me")
async def patchme(req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json(); allowed=["display_name","timezone","avatar_url","status_text","status_emoji"]; sets=[];vals=[]
    for k in allowed:
        if k in b:
            if len(str(b[k]))>250:return err(400)
            sets.append(k+"=?");vals.append(b[k])
    if sets:
        c=db();c.execute("UPDATE users SET "+",".join(sets)+" WHERE id=?",(*vals,u["id"]));c.commit();r=c.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone();c.close()
    else:r=u
    return {"user":userobj(r)}

@app.post("/api/workspaces")
async def createws(req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json(); slug=b.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug):return err(400)
    c=db()
    try:
        wid=c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,b.get("name",""),u["id"])).lastrowid
    except sqlite3.IntegrityError:c.close();return err(409)
    c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"));cid=c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")).lastrowid;c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit()
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=channel(c,cid);c.close();return JSONResponse({"workspace":wsobj(w),"general_channel":chobj(ch)},201)
@app.get("/api/workspaces")
async def listws(req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();rs=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall();c.close();return {"workspaces":[wsobj(x) for x in rs]}
@app.get("/api/workspaces/{slug}")
async def getws(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w:return err(404)
    m=member(c,w["id"],u["id"]); chans=c.execute("SELECT * FROM channels WHERE workspace_id=? AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?)) AND (is_archived=0 OR ?)",(w["id"],u["id"],req.query_params.get("include_archived") in ("1","true"))).fetchall()
    reads=[] 
    for x in chans: reads.append(readstate(c,u["id"],x["id"]))
    c.close();return {"workspace":wsobj(w),"channels":[chobj(x) for x in chans],"read_state":reads}
@app.get("/api/workspaces/{slug}/members")
async def members(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w:return err(404)
    if not member(c,w["id"],u["id"]):return err(403)
    rs=c.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w["id"],)).fetchall();c.close();return {"members":[dict(x) for x in rs]}

@app.patch("/api/workspaces/{slug}")
async def patchws(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w:return err(404)
    m=member(c,w["id"],u["id"])
    if not m or m["role"] not in ("owner","admin"):return err(403)
    if "join_mode" in b and b["join_mode"] not in ("open","invite_only"):return err(400)
    sets=[];vals=[]
    for k in ("name","join_mode"):
        if k in b:sets.append(k+"=?");vals.append(b[k])
    if sets:c.execute("UPDATE workspaces SET "+",".join(sets)+" WHERE id=?",(*vals,w["id"]));c.commit();w=c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone()
    c.close();return {"workspace":wsobj(w)}
@app.patch("/api/workspaces/{slug}/members/{uid}")
async def changerole(slug:str,uid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json(); role=b.get("role")
    if role=="owner" or role not in ("admin","member","guest"):return err(400)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone(); actor=member(c,w["id"],u["id"]) if w else None; target=member(c,w["id"],uid) if w else None
    if not w or not actor or not target:return err(404 if not w else 403)
    if uid==w["owner_id"] or actor["role"] not in ("owner","admin") or (actor["role"]=="admin" and target["role"]=="admin"):return err(403)
    c.execute("UPDATE members SET role=? WHERE workspace_id=? AND user_id=?",(role,w["id"],uid));c.commit();c.close();return {"member":{"user_id":uid,"role":role}}
@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();t=member(c,w["id"],b.get("new_owner_id")) if w else None
    if not w or u["id"]!=w["owner_id"]:return err(403)
    if not t or t["role"]!="admin":return err(400)
    c.execute("UPDATE members SET role='admin' WHERE workspace_id=? AND user_id=?",(w["id"],u["id"]));c.execute("UPDATE members SET role='owner' WHERE workspace_id=? AND user_id=?",(w["id"],b["new_owner_id"]));c.execute("UPDATE workspaces SET owner_id=? WHERE id=?",(b["new_owner_id"],w["id"]));c.commit();c.close();return {"workspace":wsobj(c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone()) if False else {"id":w["id"]}}

@app.post("/api/workspaces/{slug}/channels")
async def createch(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name) or len(b.get("topic",""))>250:return err(400)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();m=member(c,w["id"],u["id"]) if w else None
    if not w:return err(404)
    if not m or m["role"]=="guest" or (b.get("is_private",False) and m["role"] not in ("owner","admin")):return err(403)
    if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone():return err(409)
    cid=c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(b.get("is_private"))),b.get("topic",""))).lastrowid;c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.commit();r=channel(c,cid);c.close();return JSONResponse({"channel":chobj(r)},201)
@app.post("/api/channels/{cid}/join")
async def join(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();r=channel(c,cid)
    if not r:return err(404)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone();m=member(c,w["id"],u["id"])
    if r["is_private"] and not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():return err(403)
    if m and m["role"]=="guest":return err(403)
    if not m:
        if w["join_mode"]!="open":return err(403)
        c.execute("INSERT INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
    c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"]));emit(c,cid,"member.joined",{"user_id":u["id"]});c.commit();c.close();return {"channel":chobj(r)}
@app.post("/api/channels/{cid}/leave")
async def leave(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();c.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"]));emit(c,cid,"member.left",{"user_id":u["id"]});c.commit();r=channel(c,cid);c.close();return {"channel":chobj(r)}
@app.patch("/api/channels/{cid}")
async def patchch(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();r=channel(c,cid)
    if not r:return err(404)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone();m=member(c,w["id"],u["id"])
    if not m or (not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone() and u["id"]!=w["owner_id"]):return err(403)
    if len(b.get("topic",""))>250:return err(400)
    c.execute("UPDATE channels SET topic=? WHERE id=?",(b.get("topic",r["topic"]),cid));emit(c,cid,"channel.updated",{});c.commit();r=channel(c,cid);c.close();return {"channel":chobj(r)}
@app.get("/api/channels/{cid}/members")
async def chmembers(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();rs=c.execute("SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?",(cid,)).fetchall();c.close();return {"members":[userobj(x) for x in rs]}

@app.post("/api/channels/{cid}/archive")
async def archive(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();r=channel(c,cid)
    if not r:return err(404)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone()
    if u["id"]!=w["owner_id"]:return err(403)
    c.execute("UPDATE channels SET is_archived=1 WHERE id=?", (cid,)); emit(c,cid,"channel.archived",{}); c.commit()
    r=channel(c,cid);c.close();return {"channel":chobj(r)}
@app.post("/api/channels/{cid}/unarchive")
async def unarchive(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();r=channel(c,cid)
    if not r:return err(404)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone()
    if u["id"]!=w["owner_id"]:return err(403)
    c.execute("UPDATE channels SET is_archived=0 WHERE id=?", (cid,)); emit(c,cid,"channel.unarchived",{}); c.commit()
    r=channel(c,cid);c.close();return {"channel":chobj(r)}

@app.post("/api/channels/{cid}/messages")
async def postmsg(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json(); body=b.get("body","")
    if not isinstance(body,str) or not body:return err(400)
    c=db();r=channel(c,cid)
    if not r:return err(404)
    if r["is_archived"]:return err(423)
    if not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():return err(403)
    # resolve mentions
    mentions=[]; workspace=r["workspace_id"]
    for name in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)",body):
        if name=="channel":
            mentions += [x[0] for x in c.execute("SELECT user_id FROM channel_members WHERE channel_id=? AND user_id<>?",(cid,u["id"])).fetchall()]
        else:
            x=c.execute("SELECT id FROM users WHERE username=?",(name,)).fetchone()
            if x and x[0]!=u["id"] and c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(workspace,x[0])).fetchone() and c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,x[0])).fetchone():mentions.append(x[0])
    mentions=list(dict.fromkeys(mentions)); ts=now();mid=c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,b.get("parent_id"),ts)).lastrowid
    m=msgobj(c,c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone());m["mentions"]=mentions
    ev=emit(c,cid,"message.reply" if b.get("parent_id") else "message.created",m);c.commit();c.close();return JSONResponse({"message":m},201)
@app.get("/api/channels/{cid}/messages")
async def messages(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    lim,cur=page_params(req)
    if lim is None:return err(400)
    c=db();r=channel(c,cid)
    if not r:return err(404)
    if not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():return err(403)
    q="SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL"; args=[cid]
    if cur is not None:q+=" AND id<?";args.append(cur)
    q+=" ORDER BY id DESC LIMIT ?";args.append(lim+1);rs=c.execute(q,args).fetchall(); out=[msgobj(c,x) for x in rs[:lim]]; nxt=str(rs[lim-1]["id"]) if len(rs)>lim else None;c.close();return {"messages":out,"next_cursor":nxt}
@app.patch("/api/messages/{mid}")
async def edit(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r:return err(404)
    if r["deleted"]:return err(409)
    if r["author_id"]!=u["id"]:return err(403)
    c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(b.get("body",""),now(),mid));c.commit();x=msgobj(c,c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone());c.close();return {"message":x}
@app.delete("/api/messages/{mid}")
async def delete(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r:return {"deleted":True}
    w=c.execute("SELECT * FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)",(r["channel_id"],)).fetchone()
    if r["author_id"]!=u["id"] and w["owner_id"]!=u["id"]:return err(403)
    c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,));c.commit();c.close();return {"deleted":True}
@app.get("/api/messages/{mid}/replies")
async def replies(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    lim,cur=page_params(req)
    if lim is None:return err(400)
    c=db();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r:return err(404)
    q="SELECT * FROM messages WHERE parent_id=? AND deleted=0";args=[mid]
    if cur is not None:q+=" AND id>?";args.append(cur)
    q+=" ORDER BY id LIMIT ?";args.append(lim+1);rs=c.execute(q,args);rs=list(rs);out=[msgobj(c,x) for x in rs[:lim]];n=str(rs[lim-1]["id"]) if len(rs)>lim else None;c.close();return {"replies":out,"next_cursor":n}
@app.get("/api/search")
async def search(req:Request):
    u=auth(req)
    if not u:return err(401)
    q=req.query_params.get("q","");lim,cur=page_params(req)
    if lim is None:return err(400)
    c=db();rows=c.execute("SELECT m.*,ch.is_private,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.body LIKE ? AND m.deleted=0 ORDER BY m.id DESC",(u["id"],"%"+q+"%")).fetchall()
    if req.query_params.get("workspace_id"):rows=[x for x in rows if str(x["workspace_id"])==req.query_params["workspace_id"]]
    out=[{"message":msgobj(c,x)} for x in rows];c.close();return {"results":out[:lim],"next_cursor":None}

@app.post("/api/messages/{mid}/reactions")
async def react(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();c.execute("INSERT OR IGNORE INTO reactions VALUES(?,?,?)",(mid,u["id"],b.get("emoji","")));c.commit();x=reactions(c,mid);c.close();return {"reactions":x}
@app.delete("/api/messages/{mid}/reactions/{emoji}")
async def unreact(mid:int,emoji:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();c.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],emoji));c.commit();x=reactions(c,mid);c.close();return {"reactions":x}
@app.post("/api/messages/{mid}/pin")
async def pin(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();c.execute("INSERT OR IGNORE INTO pins VALUES(?,?,?)",(mid,u["id"],now()));c.commit();r=c.execute("SELECT * FROM pins WHERE message_id=?",(mid,)).fetchone();c.close();return {"pin":{"message_id":mid,"pinned_by":r["pinned_by"],"pinned_at":r["pinned_at"]}}
@app.delete("/api/messages/{mid}/pin")
async def unpin(mid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();c.execute("DELETE FROM pins WHERE message_id=?",(mid,));c.commit();c.close();return {"unpinned":True}
@app.get("/api/channels/{cid}/pins")
async def pins(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();r=channel(c,cid)
    if r["is_private"] and not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():return err(403)
    rows=c.execute("SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC",(cid,)).fetchall();out=[{"message":msgobj(c,x),"pinned_by":x["pinned_by"],"pinned_at":x["pinned_at"]} for x in rows];c.close();return {"pins":out,"next_cursor":None}

def readstate(c,uid,cid):
    x=c.execute("SELECT last_read FROM reads WHERE user_id=? AND channel_id=?",(uid,cid)).fetchone();last=x[0] if x else 0
    head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
    rows=c.execute("SELECT e.payload,m.author_id,m.parent_id FROM events e JOIN messages m ON m.id=json_extract(e.payload,'$.id') WHERE e.channel_id=? AND e.event_id>? AND m.author_id<>? AND m.parent_id IS NULL",(cid,last,uid)).fetchall()
    unread=len(rows); mention=0
    if last >= head: rows=[]; unread=0
    for x in rows:
        try:
            if uid in json.loads(x["payload"]).get("mentions",[]): mention+=1
        except Exception: pass
    return {"channel_id":cid,"last_read_event_id":last,"unread_count":unread,"mention_count":mention}
@app.get("/api/channels/{cid}/read")
async def getread(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();x=readstate(c,u["id"],cid);c.close();return {"read_state":x}
@app.post("/api/channels/{cid}/read")
async def postread(cid:int,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();old=readstate(c,u["id"],cid)["last_read_event_id"];n=max(old,int(b.get("last_read_event_id",0)));c.execute("INSERT OR REPLACE INTO reads VALUES(?,?,?)",(u["id"],cid,n));c.commit();x=readstate(c,u["id"],cid);x["unread_count"]=0;x["mention_count"]=0;c.close();return {"read_state":x}

@app.post("/api/workspaces/{slug}/invitations")
async def invite(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    b=await req.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();m=member(c,w["id"],u["id"]) if w else None
    if not w:return err(404)
    if not m or m["role"] not in ("owner","admin"):return err(403)
    code=secrets.token_urlsafe(12);exp=time.time()+b.get("expires_in_seconds",86400);maxu=b.get("max_uses",1);target=b.get("invited_user_id")
    c.execute("INSERT INTO invites VALUES(?,?,?,?,?,?,?,?)",(code,w["id"],u["id"],exp,maxu,0,target,0));c.commit();c.close();return JSONResponse({"invitation":{"code":code,"workspace_id":w["id"],"max_uses":maxu}},201)
@app.post("/api/invitations/{code}/accept")
async def accept(code:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();i=c.execute("SELECT * FROM invites WHERE code=?",(code,)).fetchone()
    if not i or i["revoked"] or i["uses"]>=i["max_uses"] or i["expires"]<time.time():return err(404)
    if i["target"] and i["target"]!=u["id"]:return err(403)
    c.execute("INSERT OR REPLACE INTO members VALUES(?,?,?)",(i["workspace_id"],u["id"],"member"));c.execute("UPDATE invites SET uses=uses+1 WHERE code=?",(code,));c.commit();c.close();return {"accepted":True}
@app.get("/api/workspaces/{slug}/invitations")
async def listinv(slug:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();m=member(c,w["id"],u["id"]) if w else None
    if not w:return err(404)
    if not m or m["role"] not in ("owner","admin"):return err(403)
    rs=c.execute("SELECT * FROM invites WHERE workspace_id=? AND revoked=0 AND expires>?",(w["id"],time.time())).fetchall();c.close();return {"invitations":[dict(x) for x in rs]}
@app.delete("/api/workspaces/{slug}/invitations/{code}")
async def revoke(slug:str,code:str,req:Request):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();m=member(c,w["id"],u["id"]) if w else None
    if not m or m["role"] not in ("owner","admin"):return err(403)
    c.execute("UPDATE invites SET revoked=1 WHERE code=?",(code,));c.commit();c.close();return {"deleted":True}

@app.websocket("/api/ws")
async def ws(sock:WebSocket):
    token=sock.query_params.get("token","").removeprefix("Bearer ")
    c=db();u=c.execute("SELECT user_id FROM tokens WHERE token=?",(token,)).fetchone();c.close()
    if not u: await sock.close(code=1008); return
    await sock.accept(); subscriptions={}
    try:
        while True:
            try:
                data=await asyncio.wait_for(sock.receive_json(),0.2)
                typ=data.get("type");cid=int(data.get("channel_id"));subscriptions[cid]=data.get("since_event_id",0) if typ=="resume" else subscriptions.get(cid,0)
                c=db();head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
                if typ=="subscribe": await sock.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
                elif typ=="resume":
                    for e in c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,subscriptions[cid])).fetchall():
                        p=json.loads(e["payload"]);await sock.send_json({"type":e["kind"],"event_id":e["event_id"],"channel_id":cid,**p});subscriptions[cid]=e["event_id"]
                    await sock.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
                c.close()
            except asyncio.TimeoutError: pass
            c=db()
            for cid,last in list(subscriptions.items()):
                for e in c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last)).fetchall():
                    p=json.loads(e["payload"]);await sock.send_json({"type":e["kind"],"event_id":e["event_id"],"channel_id":cid,**p});subscriptions[cid]=e["event_id"]
            c.close()
    except (WebSocketDisconnect,Exception): return
