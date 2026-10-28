import os, re, json, time, uuid, sqlite3, hashlib, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

PORT=int(os.getenv("PORT","8000")); NODE=(PORT-8000)//1
DB="/app/data/app.db"; os.makedirs("/app/data",exist_ok=True)
app=FastAPI()
def now(): return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA journal_mode=WAL"); return c
def init():
 c=db()
 c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,created_at TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1,expires REAL,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));"""); c.commit(); c.close()
init()
def userobj(r): return {k:r[k] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]}
def auth(req):
 h=req.headers.get("authorization","")
 if not h.lower().startswith("bearer "): raise HTTPException(401)
 c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def get_token_ws(t):
 c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone(); c.close(); return r
def bodyreq(req): return req.json()
def err(status,msg="error"): raise HTTPException(status,detail=msg)
def wsrow(slug):
 c=db(); r=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone(); c.close()
 if not r: err(404)
 return r
def role(uid,wid):
 c=db(); r=c.execute("SELECT role FROM wm WHERE user_id=? AND workspace_id=?",(uid,wid)).fetchone(); c.close(); return r[0] if r else None
def require_member(uid,wid):
 r=role(uid,wid)
 if not r: err(403)
 return r
def channel(cid):
 c=db(); r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
 if not r: err(404)
 return r
def channelobj(r):
 return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def emit(c,cid,kind,mid=None):
 n=c.execute("SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
 c.execute("INSERT INTO events VALUES(?,?,?,?,?)",(cid,n,kind,mid,now())); return n
def reactions(c,mid):
 out=[]
 for x in c.execute("SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji",(mid,)):
  ids=[z[0] for z in c.execute("SELECT user_id FROM reactions WHERE message_id=? AND emoji=?",(mid,x["emoji"]))]
  out.append({"emoji":x["emoji"],"count":x["n"],"user_ids":ids})
 return out
def msgobj(c,r):
 a=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
 replies=c.execute("SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()[0]
 return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":userobj(a),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":reactions(c,r["id"]),"mentions":mentions(c,r["body"],r["author_id"],r["channel_id"])}
def mentions(c,text,author,cid):
 ch=c.execute("SELECT workspace_id FROM channels WHERE id=?",(cid,)).fetchone()[0]
 members={x[0] for x in c.execute("SELECT user_id FROM cm WHERE channel_id=?",(cid,))}
 out=set()
 if re.search(r"(?<![\w@])@channel(?!\w)",text):
  out.update(members-{author})
 for m in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)",text):
  u=c.execute("SELECT id FROM users WHERE username=?",(m,)).fetchone()
  if u and u[0]!=author and role(u[0],ch): out.add(u[0])
 return sorted(out)
def page_params(req):
 try: lim=int(req.query_params.get("limit","50"))
 except: err(400)
 if lim<1 or lim>200: err(400)
 cur=req.query_params.get("cursor"); pos=0
 if cur:
  try: pos=int(cur)
  except: pos=0
 return lim,pos
@app.get("/api/health")
def health(): return {"status":"ok","node_id":NODE}
@app.post("/api/auth/register",status_code=201)
async def register(req:Request):
 d=await req.json(); u=d.get("username",""); p=d.get("password","")
 if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: err(400)
 c=db()
 try:
  c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(u,p,d.get("display_name") or u)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; tok=uuid.uuid4().hex+uuid.uuid4().hex; c.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close(); return {"user":userobj(r),"token":tok}
@app.post("/api/auth/login")
async def login(req:Request):
 d=await req.json(); c=db(); r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(d.get("username"),d.get("password"))).fetchone()
 if not r: c.close(); err(401)
 tok=uuid.uuid4().hex+uuid.uuid4().hex; c.execute("INSERT INTO tokens VALUES(?,?)",(tok,r["id"])); c.commit(); c.close(); return {"user":userobj(r),"token":tok}
@app.get("/api/auth/me")
def me(req:Request): return {"user":userobj(auth(req))}
@app.get("/api/users/{uid}")
def getuser(uid:int,req:Request):
 c=db(); r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close()
 if not r: err(404)
 return {"user":userobj(r)}
@app.get("/api/workspaces")
def workspaces(req:Request):
 u=auth(req); c=db(); rs=c.execute("SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
 return {"workspaces":[dict(r) for r in rs]}
@app.post("/api/workspaces",status_code=201)
async def createws(req:Request):
 u=auth(req); d=await req.json()
 if not re.fullmatch(r"[a-z0-9-]{2,32}",d.get("slug","")): err(400)
 c=db()
 try:
  c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(d["slug"],d["name"],u["id"])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO wm VALUES(?,?,?)",(wid,u["id"],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO cm VALUES(?,?)",(cid,u["id"])); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
 return {"workspace":dict(w),"general_channel":channelobj(ch)}
@app.get("/api/workspaces/{slug}")
def detail(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); require_member(u["id"],w["id"]); c=db(); inc=req.query_params.get("include_archived") in ("1","true"); q="SELECT * FROM channels WHERE workspace_id=?"+("" if inc else " AND is_archived=0"); cs=c.execute(q,(w["id"],)).fetchall(); cs=[x for x in cs if not x["is_private"] or c.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(x["id"],u["id"])).fetchone()]; rs=c.execute("SELECT channel_id,last_read FROM reads WHERE user_id=?",(u["id"],)).fetchall(); c.close()
 return {"workspace":dict(w),"channels":[channelobj(x) for x in cs],"read_state":[{"channel_id":x["channel_id"],"last_read_event_id":x["last_read"],"unread_count":0,"mention_count":0} for x in rs]}
@app.get("/api/workspaces/{slug}/members")
def members(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); require_member(u["id"],w["id"]); c=db(); x=c.execute("SELECT user_id,role FROM wm WHERE workspace_id=?",(w["id"],)).fetchall(); c.close(); return {"members":[dict(z) for z in x]}
@app.patch("/api/workspaces/{slug}")
async def patchws(slug:str,req:Request):
 u=auth(req); w=wsrow(slug)
 if role(u["id"],w["id"]) not in ("owner","admin"): err(403)
 d=await req.json()
 if "join_mode" in d and d["join_mode"] not in ("open","invite_only"): err(400)
 c=db(); c.execute("UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?",(d.get("name"),d.get("join_mode"),w["id"])); c.commit(); r=c.execute("SELECT * FROM workspaces WHERE id=?",(w["id"],)).fetchone(); c.close(); return {"workspace":dict(r)}
@app.patch("/api/workspaces/{slug}/members/{uid}")
async def patchrole(slug:str,uid:int,req:Request):
 u=auth(req); w=wsrow(slug); rr=role(u["id"],w["id"]); d=await req.json(); new=d.get("role")
 if new=="owner" or new not in ("admin","member","guest"): err(400)
 old=role(uid,w["id"])
 if rr not in ("owner","admin") or old=="owner" or (rr=="admin" and old=="admin") or uid==u["id"]: err(403)
 c=db(); c.execute("UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?",(new,w["id"],uid)); c.commit(); c.close(); return {"member":{"user_id":uid,"role":new}}
@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); d=await req.json()
 if role(u["id"],w["id"])!="owner": err(403)
 if role(d.get("new_owner_id"),w["id"])!="admin": err(400)
 c=db(); c.execute("UPDATE wm SET role='admin' WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])); c.execute("UPDATE wm SET role='owner' WHERE workspace_id=? AND user_id=?",(w["id"],d["new_owner_id"])); c.commit(); c.close(); return {"workspace":dict(wsrow(slug))}
@app.post("/api/workspaces/{slug}/channels",status_code=201)
async def createch(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); rr=role(u["id"],w["id"]); d=await req.json(); name=d.get("name","")
 if not re.fullmatch(r"[a-z0-9-]{1,32}",name) or len(name)>32 or len(d.get("topic",""))>250: err(400)
 if rr not in ("owner","admin","member") or (d.get("is_private",False) and rr not in ("owner","admin")): err(403)
 c=db()
 try: c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(d.get("is_private"))),d.get("topic",""))); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO cm VALUES(?,?)",(cid,u["id"])); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close(); return {"channel":channelobj(r)}
@app.post("/api/channels/{cid}/join")
def join(cid:int,req:Request):
 u=auth(req); ch=channel(cid); w=wsrow_by_id(ch["workspace_id"]); rr=role(u["id"],ch["workspace_id"])
 if ch["is_private"]:
  c0=db(); in_channel=c0.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(); c0.close()
  if not in_channel: err(403)
 if rr=="guest": err(403)
 c=db()
 if not rr:
  if w["join_mode"]!="open": c.close(); err(403)
  c.execute("INSERT OR IGNORE INTO wm VALUES(?,?,?)",(w["id"],u["id"],"member"))
 before=c.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(); c.execute("INSERT OR IGNORE INTO cm VALUES(?,?)",(cid,u["id"]));
 if not before: emit(c,cid,"member.joined")
 c.commit()
 r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
 return {"channel":channelobj(r)}

def wsrow_by_id(i):
 c=db(); r=c.execute("SELECT * FROM workspaces WHERE id=?",(i,)).fetchone(); c.close(); return r
@app.post("/api/channels/{cid}/leave")
def leave(cid:int,req:Request):
 u=auth(req); ch=channel(cid); c=db(); before=c.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(); c.execute("DELETE FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"]));
 if before: emit(c,cid,"member.left")
 c.commit(); r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close(); return {"channel":channelobj(r)}
@app.get("/api/channels/{cid}/members")
def chmembers(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); c=db(); rs=c.execute("SELECT u.* FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?",(cid,)).fetchall(); c.close(); return {"members":[userobj(x) for x in rs]}
@app.patch("/api/channels/{cid}")
async def patchch(cid:int,req:Request):
 u=auth(req); ch=channel(cid); rr=role(u["id"],ch["workspace_id"]); c=db()
 if not (rr in ("owner","admin") or c.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone()): c.close(); err(403)
 if ch["is_archived"]: c.close(); err(423)
 d=await req.json()
 if len(d.get("topic",""))>250: c.close(); err(400)
 c.execute("UPDATE channels SET topic=? WHERE id=?",(d.get("topic",ch["topic"]),cid)); emit(c,cid,"channel.updated"); c.commit(); r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close(); return {"channel":channelobj(r)}
@app.post("/api/channels/{cid}/archive")
def archive(cid:int,req:Request): return arch(cid,req,1)
@app.post("/api/channels/{cid}/unarchive")
def unarchive(cid:int,req:Request): return arch(cid,req,0)
def arch(cid,req,val):
 u=auth(req); ch=channel(cid)
 if role(u["id"],ch["workspace_id"])!="owner": err(403)
 c=db(); c.execute("UPDATE channels SET is_archived=? WHERE id=?",(val,cid)); emit(c,cid,"channel.archived" if val else "channel.unarchived"); c.commit(); r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close(); return {"channel":channelobj(r)}
@app.post("/api/channels/{cid}/messages",status_code=201)
async def postmsg(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); d=await req.json(); body=d.get("body","")
 if not body: err(400)
 if ch["is_archived"]: err(423)
 c=db(); parent=d.get("parent_id")
 if parent and not c.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0",(parent,cid)).fetchone(): c.close(); err(404)
 t=now(); c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,parent,t)); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; kind="message.reply" if parent else "message.created"; eid=emit(c,cid,kind,mid); c.commit(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); out=msgobj(c,r); c.close(); return {"message":out}
@app.get("/api/channels/{cid}/messages")
def listmsg(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); lim,pos=page_params(req); cur=req.query_params.get("cursor")
 c=db()
 if cur:
  try:
   bound=int(cur[1:] if cur.startswith("b") else cur); rs=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND id<? ORDER BY id DESC",(cid,bound)).fetchall()
  except: c.close(); err(400)
 else: rs=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC",(cid,)).fetchall()
 items=rs[:lim]; nxt=("b"+str(items[-1]["id"])) if len(rs)>lim and items else None
 out=[msgobj(c,x) for x in items]; c.close(); return {"messages":out,"next_cursor":nxt}

@app.patch("/api/messages/{mid}")
async def edit(mid:int,req:Request):
 u=auth(req); c=db(); r=c.execute("SELECT m.*,c.workspace_id,c.is_archived FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=?",(mid,)).fetchone()
 if not r: c.close(); err(404)
 if r["is_archived"]: c.close(); err(423)
 if r["author_id"]!=u["id"]: c.close(); err(403)
 d=await req.json(); 
 if not d.get("body"): c.close(); err(400)
 c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(d["body"],now(),mid)); emit(c,r["channel_id"],"message.updated",mid); c.commit(); x=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); out=msgobj(c,x); c.close(); return {"message":out}
@app.get("/api/messages/{mid}/replies")
def replies(mid:int,req:Request):
 u=auth(req); c=db(); p=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
 if not p: c.close(); err(404)
 require_member(u["id"],c.execute("SELECT workspace_id FROM channels WHERE id=?",(p["channel_id"],)).fetchone()[0]); lim,pos=page_params(req); rs=c.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id",(mid,)).fetchall(); out=[msgobj(c,x) for x in rs[pos:pos+lim]]; nxt=str(pos+lim) if pos+lim<len(rs) else None; c.close(); return {"replies":out,"next_cursor":nxt}
@app.delete("/api/messages/{mid}")
def delete(mid:int,req:Request):
 u=auth(req); c=db(); r=c.execute("SELECT m.*,c.workspace_id FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=?",(mid,)).fetchone()
 if not r: c.close(); err(404)
 if r["author_id"]!=u["id"] and role(u["id"],r["workspace_id"])!="owner": c.close(); err(403)
 c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,)); emit(c,r["channel_id"],"message.deleted",mid); c.commit(); c.close(); return {"deleted":True}
@app.get("/api/search")
def search(req:Request):
 u=auth(req); q=req.query_params.get("q",""); lim,pos=page_params(req); c=db(); wid=req.query_params.get("workspace_id"); extra=" AND ch.workspace_id="+str(int(wid)) if wid and wid.isdigit() else ""; rs=c.execute("SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ?"+extra+" ORDER BY m.id DESC",(u["id"],"%"+q+"%")).fetchall(); out=[{"message":msgobj(c,x)} for x in rs[pos:pos+lim]]; nxt=str(pos+lim) if pos+lim<len(rs) else None; c.close(); return {"results":out,"next_cursor":nxt}
@app.post("/api/messages/{mid}/reactions")
async def addreact(mid:int,req:Request):
 u=auth(req); d=await req.json(); c=db(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
 if not r: c.close(); err(404)
 c.execute("INSERT OR IGNORE INTO reactions VALUES(?,?,?)",(mid,u["id"],d.get("emoji",""))); c.commit(); x=reactions(c,mid); c.close(); return {"reactions":x}
@app.delete("/api/messages/{mid}/reactions/{emoji}")
def delreact(mid:int,emoji:str,req:Request):
 u=auth(req); c=db(); c.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],emoji)); c.commit(); x=reactions(c,mid); c.close(); return {"reactions":x}
@app.post("/api/messages/{mid}/pin")
def pin(mid:int,req:Request):
 u=auth(req); c=db(); r=c.execute("SELECT m.*,ch.is_archived FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?",(mid,)).fetchone()
 if not r: c.close(); err(404)
 if r["is_archived"]: c.close(); err(423)
 c.execute("INSERT OR IGNORE INTO pins VALUES(?,?,?)",(mid,u["id"],now())); c.commit(); x=c.execute("SELECT * FROM pins WHERE message_id=?",(mid,)).fetchone(); c.close(); return {"pin":{"message_id":mid,"pinned_by":x["user_id"],"pinned_at":x["pinned_at"]}}
@app.delete("/api/messages/{mid}/pin")
def unpin(mid:int,req:Request):
 auth(req); c=db(); c.execute("DELETE FROM pins WHERE message_id=?",(mid,)); c.commit(); c.close(); return {"unpinned":True}
@app.get("/api/channels/{cid}/pins")
def getpins(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); c0=db(); okm=c0.execute("SELECT 1 FROM cm WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(); c0.close();
 if not okm: err(403)
 lim,pos=page_params(req); c=db(); rs=c.execute("SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC",(cid,)).fetchall(); out=[{"message":msgobj(c,x),"pinned_by":x["user_id"],"pinned_at":x["pinned_at"]} for x in rs[pos:pos+lim]]; nxt=str(pos+lim) if pos+lim<len(rs) else None; c.close(); return {"pins":out,"next_cursor":nxt}
@app.get("/api/channels/{cid}/read")
def readget(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); c=db(); x=c.execute("SELECT last_read FROM reads WHERE user_id=? AND channel_id=?",(u["id"],cid)).fetchone(); last=x[0] if x else 0
 ev=c.execute("SELECT e.event_id,m.author_id,m.body FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? AND e.kind='message.created' ORDER BY e.event_id",(cid,last)).fetchall()
 unread=sum(1 for e in ev if e["author_id"]!=u["id"]); mention=sum(1 for e in ev if e["author_id"]!=u["id"] and u["id"] in mentions(c,e["body"],e["author_id"],cid)); c.close()
 return {"read_state":{"channel_id":cid,"last_read_event_id":last,"unread_count":unread,"mention_count":mention}}

@app.post("/api/channels/{cid}/read")
async def readpost(cid:int,req:Request):
 u=auth(req); ch=channel(cid); require_member(u["id"],ch["workspace_id"]); d=await req.json(); c=db(); old=c.execute("SELECT last_read FROM reads WHERE user_id=? AND channel_id=?",(u["id"],cid)).fetchone(); last=max(old[0] if old else 0,int(d.get("last_read_event_id",0))); c.execute("INSERT INTO reads VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=excluded.last_read",(u["id"],cid,last)); c.commit(); c.close(); return readget(cid,req)

@app.post("/api/workspaces/{slug}/invitations",status_code=201)
async def create_invite(slug:str,req:Request):
 u=auth(req); w=wsrow(slug)
 if role(u["id"],w["id"]) not in ("owner","admin"): err(403)
 d=await req.json(); target=d.get("invited_user_id")
 if target is None and d.get("invited_username"):
  c=db(); z=c.execute("SELECT id FROM users WHERE username=?",(d["invited_username"],)).fetchone(); c.close()
  if z: target=z[0]
 try: uses=int(d.get("max_uses",1))
 except: err(400)
 if uses<1: err(400)
 exp=None
 if d.get("expires_in_seconds") is not None:
  try: exp=time.time()+float(d["expires_in_seconds"])
  except: err(400)
 code=uuid.uuid4().hex
 c=db(); c.execute("INSERT INTO invites VALUES(?,?,?,?,?,?,?)",(code,w["id"],target,0,uses,exp,0)); c.commit()
 r=c.execute("SELECT * FROM invites WHERE code=?",(code,)).fetchone(); c.close()
 return {"invitation":{"code":r["code"],"workspace_id":r["workspace_id"],"invited_user_id":r["invited_user_id"],"uses":r["uses"],"max_uses":r["max_uses"],"expires_at":r["expires"]}}

@app.get("/api/workspaces/{slug}/invitations")
def list_invites(slug:str,req:Request):
 u=auth(req); w=wsrow(slug)
 if role(u["id"],w["id"]) not in ("owner","admin"): err(403)
 c=db(); rs=c.execute("SELECT * FROM invites WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires IS NULL OR expires>?)",(w["id"],time.time())).fetchall(); c.close()
 return {"invitations":[{"code":r["code"],"workspace_id":r["workspace_id"],"invited_user_id":r["invited_user_id"],"uses":r["uses"],"max_uses":r["max_uses"],"expires_at":r["expires"]} for r in rs]}

@app.post("/api/invitations/{code}/accept")
async def accept_invite(code:str,req:Request):
 u=auth(req); c=db(); r=c.execute("SELECT * FROM invites WHERE code=?",(code,)).fetchone()
 if not r or r["revoked"] or r["uses"]>= (1 if r["invited_user_id"] is not None else r["max_uses"]) or (r["expires"] is not None and r["expires"]<=time.time()): c.close(); err(404)
 if r["invited_user_id"] is not None and r["invited_user_id"]!=u["id"]: c.close(); err(403)
 c.execute("INSERT OR IGNORE INTO wm VALUES(?,?,?)",(r["workspace_id"],u["id"],"member"))
 c.execute("UPDATE invites SET uses=uses+1 WHERE code=?",(code,)); c.commit(); c.close()
 return {"workspace":dict(wsrow_by_id(r["workspace_id"]))}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
def revoke_invite(slug:str,code:str,req:Request):
 u=auth(req); w=wsrow(slug)
 if role(u["id"],w["id"]) not in ("owner","admin"): err(403)
 c=db(); r=c.execute("SELECT * FROM invites WHERE code=? AND workspace_id=?",(code,w["id"])).fetchone()
 if not r: c.close(); err(404)
 c.execute("UPDATE invites SET revoked=1 WHERE code=?",(code,)); c.commit(); c.close(); return {"deleted":True}

@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
 t=ws.query_params.get("token",""); u=get_token_ws(t)
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs={}
 try:
  while True:
   try:
    data=await asyncio.wait_for(ws.receive_json(),timeout=.25)
    cid=int(data.get("channel_id")); typ=data.get("type")
    if typ=="subscribe":
     c=db(); head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]; c.close(); subs[cid]=head; await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
    elif typ=="resume":
     since=int(data.get("since_event_id",0)); subs[cid]=since
     c=db(); ev=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall()
     for e in ev:
      payload={"type":e["kind"],"event_id":e["event_id"],"channel_id":cid}
      if e["message_id"]:
       r=c.execute("SELECT * FROM messages WHERE id=?",(e["message_id"],)).fetchone()
       if r and not r["deleted"]: payload["message"]=msgobj(c,r)
       else: continue
      await ws.send_json(payload)
     head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]; c.close(); await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head}); subs[cid]=head
   except asyncio.TimeoutError: pass
   c=db()
   for cid,last in list(subs.items()):
    ev=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last)).fetchall()
    for e in ev:
     payload={"type":e["kind"],"event_id":e["event_id"],"channel_id":cid}
     if e["message_id"]:
      r=c.execute("SELECT * FROM messages WHERE id=?",(e["message_id"],)).fetchone()
      if r and not r["deleted"]: payload["message"]=msgobj(c,r)
     await ws.send_json(payload); subs[cid]=e["event_id"]
   c.close()
 except (WebSocketDisconnect,Exception): return
