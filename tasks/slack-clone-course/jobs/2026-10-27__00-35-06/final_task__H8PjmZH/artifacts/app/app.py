import os, re, json, sqlite3, secrets, hashlib, asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
DB="/app/data.db"; os.makedirs("/app",exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def db():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=30000"); return c
def init():
 c=db(); c.execute("PRAGMA journal_mode=WAL"); c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER,topic TEXT,is_archived INTEGER);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,uses INTEGER,max_uses INTEGER,expires REAL,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);"""); c.commit(); c.close()
init()
try:
 c=db(); c.execute("create unique index if not exists ux_channel_name on channels(workspace_id,name)"); c.commit(); c.close()
except: pass
def userobj(r): return {k:r[k] for k in ["id","username","display_name","timezone","avatar_url","status_text","status_emoji"]}
def auth(req):
 t=req.headers.get("authorization","")
 if not t.lower().startswith("bearer "): raise HTTPException(401)
 c=db(); r=c.execute("select u.* from tokens t join users u on u.id=t.user_id where t.token=?",(t[7:],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def juser(uid):
 c=db(); r=c.execute("select * from users where id=?",(uid,)).fetchone(); c.close(); return userobj(r)
def wsrow(slug):
 c=db(); r=c.execute("select * from workspaces where slug=?",(slug,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return r
def role(w,u):
 c=db(); r=c.execute("select role from members where workspace_id=? and user_id=?",(w,u)).fetchone(); c.close(); return r["role"] if r else None
def require_member(ch,u,write=False):
 c=db(); r=c.execute("select * from channels where id=?",(ch,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 rr=role(r["workspace_id"],u)
 cm=c.execute("select 1 from cmembers where channel_id=? and user_id=?",(ch,u)).fetchone(); c.close()
 if not cm and not (rr=="owner" and not write): raise HTTPException(403)
 if write and r["is_archived"]: raise HTTPException(423)
 return r
def channel(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def msg(r):
 d=dict(r); d["author"]=juser(d["author_id"]); d["parent_id"]=d["parent_id"]; d["reply_count"]=count_replies(d["id"]); d["files"]=[]; d["reactions"]=[]; d["mentions"]=mentions(d["body"],d["channel_id"],d["author_id"]); return {k:d[k] for k in ["id","channel_id","author_id","author","body","parent_id","reply_count","created_at","edited_at","files","reactions","mentions"]}
def count_replies(i):
 c=db(); n=c.execute("select count(*) n from messages where parent_id=? and deleted=0",(i,)).fetchone()["n"]; c.close(); return n
def mentions(body,ch,author):
 c=db(); cr=c.execute("select workspace_id from channels where id=?",(ch,)).fetchone(); mem={x["user_id"] for x in c.execute("select user_id from members where workspace_id=?",(cr["workspace_id"],))}; users={x["username"]:x["id"] for x in c.execute("select * from users")}; cm={x["user_id"] for x in c.execute("select user_id from cmembers where channel_id=?",(ch,))}; c.close()
 out=set()
 if "@channel" in body: out.update(cm-{author})
 for x in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)",body):
  if x in users and users[x] in mem and users[x]!=author: out.add(users[x])
 return sorted(out)
def emit(c, ch, typ, payload):
 n=c.execute("select coalesce(max(event_id),0)+1 n from events where channel_id=?",(ch,)).fetchone()["n"]
 c.execute("insert into events values(?,?,?,?)",(ch,n,typ,json.dumps(payload))); return n
def pageargs(request):
 try: lim=int(request.query_params.get("limit","50"))
 except: raise HTTPException(400)
 if lim<1 or lim>200: raise HTTPException(400)
 cur=request.query_params.get("cursor"); off=0
 if cur:
  try: off=int(cur)
  except: raise HTTPException(400)
 return lim,off
app=FastAPI()
@app.get("/api/health")
def health(request:Request): return {"status":"ok","node_id":int(request.url.port)-8000}
@app.post("/api/auth/register",status_code=201)
async def register(request:Request):
 d=await request.json(); u=d.get("username",""); p=d.get("password","")
 if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: raise HTTPException(400)
 c=db()
 try: c.execute("insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)",(u,p,d.get("display_name") or u,"UTC","","","")); c.commit()
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 uid=c.execute("select last_insert_rowid()").fetchone()[0]; t=secrets.token_urlsafe(24); c.execute("insert into tokens values(?,?)",(t,uid)); c.commit(); r=c.execute("select * from users where id=?",(uid,)).fetchone(); c.close(); return {"user":userobj(r),"token":t}
@app.post("/api/auth/login")
async def login(request:Request):
 d=await request.json(); c=db(); r=c.execute("select * from users where username=? and password=?",(d.get("username"),d.get("password"))).fetchone()
 if not r: c.close(); raise HTTPException(401)
 t=secrets.token_urlsafe(24); c.execute("insert into tokens values(?,?)",(t,r["id"])); c.commit(); c.close(); return {"user":userobj(r),"token":t}
@app.get("/api/auth/me")
def me(request:Request): return {"user":userobj(auth(request))}
@app.patch("/api/users/me")
async def patch_me(request:Request):
 u=auth(request); d=await request.json()
 allowed={"display_name","timezone","avatar_url","status_text","status_emoji"}
 if any(k not in allowed for k in d): raise HTTPException(400)
 if "timezone" in d and (not isinstance(d["timezone"],str) or len(d["timezone"])>64 or not d["timezone"]): raise HTTPException(400)
 for k in allowed:
  if k in d and (not isinstance(d[k],str) or len(d[k])>500): raise HTTPException(400)
 c=db(); vals={k:d[k] for k in allowed if k in d}
 if vals:
  c.execute("update users set "+",".join(k+"=?" for k in vals)+" where id=?",tuple(vals.values())+(u["id"],))
 c.commit(); r=c.execute("select * from users where id=?",(u["id"],)).fetchone(); c.close()
 return {"user":userobj(r)}

@app.get("/api/users/{uid}")
def getuser(uid:int):
 c=db(); r=c.execute("select * from users where id=?",(uid,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return {"user":userobj(r)}
@app.post("/api/workspaces",status_code=201)
async def createws(request:Request):
 u=auth(request); d=await request.json(); s=d.get("slug","")
 if not re.fullmatch(r"[a-z0-9-]{2,32}",s): raise HTTPException(400)
 c=db()
 try: c.execute("insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,'open')",(s,d.get("name",""),u["id"]))
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 wid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into members values(?,?,?)",(wid,u["id"],"owner")); c.execute("insert into channels(workspace_id,name,is_private,is_dm,topic,is_archived) values(?, 'general',0,0,'',0)",(wid,)); cid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into cmembers values(?,?)",(cid,u["id"])); c.commit(); c.close(); return {"workspace":{"id":wid,"slug":s,"name":d.get("name",""),"owner_id":u["id"],"join_mode":"open"},"general_channel":channel({"id":cid,"workspace_id":wid,"name":"general","is_private":0,"is_dm":0,"topic":"","is_archived":0})}
@app.get("/api/workspaces")
def listws(request:Request):
 u=auth(request); c=db(); rs=c.execute("select w.* from workspaces w join members m on w.id=m.workspace_id where m.user_id=?",(u["id"],)).fetchall(); c.close(); return {"workspaces":[dict(r) for r in rs]}
@app.get("/api/workspaces/{slug}")
def getws(slug:str,request:Request):
 u=auth(request); w=wsrow(slug); c=db(); inc=request.query_params.get("include_archived") in ("1","true"); rr=c.execute("select c.* from channels c where c.workspace_id=? and (c.is_private=0 or exists (select 1 from cmembers m where m.channel_id=c.id and m.user_id=?))"+("" if inc else " and c.is_archived=0"),(w["id"],u["id"])).fetchall(); states=[]; 
 for x in rr:
  q=c.execute("select coalesce(max(last_read_event_id),0) n from reads where user_id=? and channel_id=?",(u["id"],x["id"])).fetchone(); states.append({"channel_id":x["id"],"last_read_event_id":q["n"],"unread_count":0,"mention_count":0})
 c.close(); return {"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":w["join_mode"]},"channels":[channel(x) for x in rr],"read_state":states}
@app.get("/api/workspaces/{slug}/members")
def members(slug:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if not role(w["id"],u["id"]): raise HTTPException(403)
 c=db(); rs=c.execute("select user_id,role from members where workspace_id=?",(w["id"],)).fetchall(); c.close(); return {"members":[dict(x) for x in rs]}
@app.post("/api/workspaces/{slug}/channels",status_code=201)
async def createch(slug:str,request:Request):
 u=auth(request); w=wsrow(slug); rr=role(w["id"],u["id"]); d=await request.json(); name=d.get("name","")
 if rr not in ("owner","admin","member") or (d.get("is_private",False) and rr not in ("owner","admin")): raise HTTPException(403)
 if not re.fullmatch(r"[a-z0-9-]{1,32}",name): raise HTTPException(400)
 if len(d.get("topic",""))>250: raise HTTPException(400)
 c=db()
 if c.execute("select 1 from channels where workspace_id=? and name=?",(w["id"],name)).fetchone(): c.close(); raise HTTPException(409)
 try: c.execute("insert into channels(workspace_id,name,is_private,is_dm,topic,is_archived) values(?,?,?,?,?,0)",(w["id"],name,int(bool(d.get("is_private"))),0,d.get("topic","")))
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 cid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into cmembers values(?,?)",(cid,u["id"])); c.commit(); r=c.execute("select * from channels where id=?",(cid,)).fetchone(); c.close(); return {"channel":channel(r)}
@app.post("/api/channels/{cid}/join")
def join(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute("select * from channels where id=?",(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 rr=role(ch["workspace_id"],u["id"])
 if ch["is_private"] or rr=="guest" or (not rr and c.execute("select join_mode from workspaces where id=?",(ch["workspace_id"],)).fetchone()["join_mode"]!="open"): c.close(); raise HTTPException(403)
 if not rr: c.execute("insert into members values(?,?, 'member')",(ch["workspace_id"],u["id"]))
 before=c.execute("select 1 from cmembers where channel_id=? and user_id=?",(cid,u["id"])).fetchone(); c.execute("insert or ignore into cmembers values(?,?)",(cid,u["id"]));
 if not before: emit(c,cid,"member.joined",{"user_id":u["id"]})
 c.commit(); c.close(); return {"channel":channel(ch)}
@app.post("/api/channels/{cid}/leave")
def leave(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute("select * from channels where id=?",(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 cur=c.execute("delete from cmembers where channel_id=? and user_id=?",(cid,u["id"]))
 if cur.rowcount: emit(c,cid,"member.left",{"user_id":u["id"]})
 c.commit(); c.close(); return {"channel":channel(ch)}

@app.post("/api/channels/{cid}/archive")
def archive(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute("select * from channels where id=?",(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 if role(ch["workspace_id"],u["id"])!="owner": c.close(); raise HTTPException(403)
 c.execute("update channels set is_archived=1 where id=?",(cid,)); emit(c,cid,"channel.archived",{"channel":channel(dict(ch)|{"is_archived":1})}); c.commit(); r=c.execute("select * from channels where id=?",(cid,)).fetchone(); c.close(); return {"channel":channel(r)}
@app.post("/api/channels/{cid}/unarchive")
def unarchive(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute("select * from channels where id=?",(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 if role(ch["workspace_id"],u["id"])!="owner": c.close(); raise HTTPException(403)
 c.execute("update channels set is_archived=0 where id=?",(cid,)); emit(c,cid,"channel.unarchived",{"channel":channel(dict(ch)|{"is_archived":0})}); c.commit(); r=c.execute("select * from channels where id=?",(cid,)).fetchone(); c.close(); return {"channel":channel(r)}

@app.patch("/api/channels/{cid}")
async def patchch(cid:int,request:Request):
 u=auth(request); ch=require_member(cid,u["id"],False); d=await request.json()
 if ch["is_archived"]: raise HTTPException(423)
 if len(d.get("topic",""))>250: raise HTTPException(400)
 if role(ch["workspace_id"],u["id"]) not in ("owner","admin"):
  z=db(); ismem=z.execute("select 1 from cmembers where channel_id=? and user_id=?",(cid,u["id"])).fetchone(); z.close()
  if not ismem: raise HTTPException(403)
 c=db(); c.execute("update channels set topic=? where id=?",(d.get("topic",ch["topic"]),cid)); emit(c,cid,"channel.updated",{"channel":channel(dict(ch)|{"topic":d.get("topic",ch["topic"])})}); c.commit(); r=c.execute("select * from channels where id=?",(cid,)).fetchone(); c.close(); return {"channel":channel(r)}
@app.get("/api/channels/{cid}/members")
def chmembers(cid:int,request:Request):
 u=auth(request); require_member(cid,u["id"]); c=db(); rs=c.execute("select u.* from users u join cmembers m on u.id=m.user_id where m.channel_id=?",(cid,)).fetchall(); c.close(); return {"members":[userobj(x) for x in rs]}
@app.post("/api/channels/{cid}/messages",status_code=201)
async def postmsg(cid:int,request:Request):
 u=auth(request); ch=require_member(cid,u["id"],True); d=await request.json(); body=d.get("body","")
 if not body: raise HTTPException(400)
 if body.startswith("/") and not body.startswith("//") and body.split()[0] not in ("/me","/shrug"): raise HTTPException(400)
 if body.startswith("/shrug"): body=body[7:].strip()+" ¯\\_(ツ)_/¯"
 if d.get("parent_id"):
  z=db(); parent=z.execute("select id,channel_id from messages where id=? and deleted=0",(d["parent_id"],)).fetchone(); z.close()
  if not parent or parent["channel_id"]!=cid: raise HTTPException(404)
 c=db(); c.execute("BEGIN IMMEDIATE"); ts=now(); c.execute("insert into messages(channel_id,author_id,body,parent_id,created_at,edited_at) values(?,?,?,?,?,NULL)",(cid,u["id"],body,d.get("parent_id"),ts)); mid=c.execute("select last_insert_rowid()").fetchone()[0]; r=c.execute("select * from messages where id=?",(mid,)).fetchone(); typ="message.reply" if d.get("parent_id") else "message.created"; eid=emit(c,cid,typ,{"message":msg(r)}); c.execute("update messages set event_id=? where id=?",(eid,mid)); c.commit(); c.close(); return {"message":msg(dict(dict(r),event_id=eid))}
@app.get("/api/channels/{cid}/messages")
def getmsgs(cid:int,request:Request):
 u=auth(request); require_member(cid,u["id"]); lim,off=pageargs(request); c=db(); cur=request.query_params.get("cursor"); args=[cid]; where="channel_id=? and parent_id is null and deleted=0"
 if cur:
  try: boundary=int(cur)
  except: raise HTTPException(400)
  where+=" and id<?"; args.append(boundary)
 args.append(lim+1); c=db(); rs=c.execute("select * from messages where "+where+" order by id desc limit ?",args).fetchall(); c.close()
 more=len(rs)>lim; rs=rs[:lim]; nxt=str(rs[-1]["id"]) if more and rs else None
 return {"messages":[msg(x) for x in rs],"next_cursor":nxt}
@app.get("/api/messages/{mid}/replies")
def replies(mid:int,request:Request):
 u=auth(request); c=db(); p=c.execute("select channel_id from messages where id=?",(mid,)).fetchone(); c.close()
 if not p: raise HTTPException(404)
 require_member(p["channel_id"],u["id"]); lim,off=pageargs(request); c=db(); cur=request.query_params.get("cursor"); args=[mid]; where="parent_id=? and deleted=0"
 if cur:
  try: args.append(int(cur))
  except: raise HTTPException(400)
  where+=" and id>?"
 args.append(lim+1); c=db(); rs=c.execute("select * from messages where "+where+" order by id limit ?",args).fetchall(); c.close()
 more=len(rs)>lim; rs=rs[:lim]
 return {"replies":[msg(x) for x in rs],"next_cursor":str(rs[-1]["id"]) if more and rs else None}
@app.patch("/api/messages/{mid}")
async def edit(mid:int,request:Request):
 u=auth(request); d=await request.json(); c=db(); r=c.execute("select * from messages where id=?",(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 if r["author_id"]!=u["id"]: c.close(); raise HTTPException(403)
 c.execute("update messages set body=?,edited_at=? where id=?",(d.get("body",""),now(),mid)); c.commit(); r=c.execute("select * from messages where id=?",(mid,)).fetchone(); c.close(); return {"message":msg(r)}
@app.delete("/api/messages/{mid}")
def delete(mid:int,request:Request):
 u=auth(request); c=db(); r=c.execute("select * from messages where id=?",(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 if r["author_id"]!=u["id"]: c.close(); raise HTTPException(403)
 c.execute("update messages set deleted=1 where id=?",(mid,)); c.commit(); c.close(); return {"deleted":True}
@app.patch("/api/workspaces/{slug}/members/{uid}")
async def setrole(slug:str,uid:int,request:Request):
 u=auth(request); w=wsrow(slug); d=await request.json(); nr=d.get("role")
 if nr not in ("owner","admin","member","guest"): raise HTTPException(400)
 if nr=="owner": raise HTTPException(400)
 ar=role(w["id"],u["id"]); old=role(w["id"],uid)
 if not old: raise HTTPException(404)
 if uid==w["owner_id"] or ar not in ("owner","admin") or (ar=="admin" and old=="admin"): raise HTTPException(403)
 c=db(); c.execute("update members set role=? where workspace_id=? and user_id=?",(nr,w["id"],uid)); c.commit(); c.close(); return {"member":{"user_id":uid,"role":nr}}
@app.patch("/api/workspaces/{slug}")
async def patchws(slug:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403)
 d=await request.json()
 if "join_mode" in d and d["join_mode"] not in ("open","invite_only"): raise HTTPException(400)
 c=db(); c.execute("update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where id=?",(d.get("name"),d.get("join_mode"),w["id"])); c.commit(); r=c.execute("select * from workspaces where id=?",(w["id"],)).fetchone(); c.close(); return {"workspace":dict(r)}
@app.post("/api/workspaces/{slug}/transfer_ownership")
async def transfer(slug:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if u["id"]!=w["owner_id"]: raise HTTPException(403)
 d=await request.json(); uid=d.get("new_owner_id")
 if role(w["id"],uid)!="admin": raise HTTPException(400)
 c=db(); c.execute("update members set role='admin' where workspace_id=? and user_id=?",(w["id"],u["id"])); c.execute("update members set role='owner' where workspace_id=? and user_id=?",(w["id"],uid)); c.execute("update workspaces set owner_id=? where id=?",(uid,w["id"])); c.commit(); c.close(); return {"workspace":dict(wsrow(slug))}
@app.post("/api/workspaces/{slug}/invitations",status_code=201)
async def invite(slug:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403)
 d=await request.json(); target=d.get("invited_user_id"); c=db(); code=secrets.token_urlsafe(12); exp=None
 if d.get("invited_username"):
  z=c.execute("select id from users where username=?",(d["invited_username"],)).fetchone()
  if not z: c.close(); raise HTTPException(400)
  target=z["id"]
 if d.get("expires_in_seconds") is not None: import time; exp=time.time()+int(d["expires_in_seconds"])
 c.execute("insert into invites values(?,?,?,?,?,?,0)",(code,w["id"],target,0,1 if target else d.get("max_uses",1),exp)); c.commit(); c.close(); return {"invitation":{"code":code,"workspace_id":w["id"],"uses":0,"max_uses":d.get("max_uses",1)}}
@app.get("/api/workspaces/{slug}/invitations")
def list_invites(slug:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403)
 import time
 c=db(); rs=c.execute("select * from invites where workspace_id=? and revoked=0 and uses<max_uses and (expires is null or expires>?)",(w["id"],time.time())).fetchall(); c.close()
 return {"invitations":[{"code":x["code"],"workspace_id":x["workspace_id"],"invited_user_id":x["invited_user_id"],"uses":x["uses"],"max_uses":x["max_uses"]} for x in rs]}

@app.delete("/api/workspaces/{slug}/invitations/{code}")
def revoke_invite(slug:str,code:str,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(w["id"],u["id"]) not in ("owner","admin"): raise HTTPException(403)
 c=db(); x=c.execute("select * from invites where code=? and workspace_id=?",(code,w["id"])).fetchone()
 if not x: c.close(); raise HTTPException(404)
 c.execute("update invites set revoked=1 where code=?",(code,)); c.commit(); c.close(); return {"deleted":True}

@app.post("/api/invitations/{code}/accept")
def accept(code:str,request:Request):
 u=auth(request); import time; c=db(); x=c.execute("select * from invites where code=?",(code,)).fetchone()
 if not x or x["revoked"] or x["uses"]>=x["max_uses"] or (x["expires"] and x["expires"]<time.time()): c.close(); raise HTTPException(404)
 if x["invited_user_id"] and x["invited_user_id"]!=u["id"]: c.close(); raise HTTPException(403)
 c.execute("insert or replace into members values(?,?, 'member')",(x["workspace_id"],u["id"])); c.execute("update invites set uses=uses+1 where code=?",(code,)); c.commit(); c.close(); return {"workspace":dict(wsrow(db().execute("select slug from workspaces where id=?",(x["workspace_id"],)).fetchone()["slug"]))}
@app.get("/api/channels/{cid}/read")
def readget(cid:int,request:Request):
 u=auth(request); require_member(cid,u["id"]); c=db(); r=c.execute("select coalesce(max(last_read_event_id),0) n from reads where user_id=? and channel_id=?",(u["id"],cid)).fetchone(); last=r["n"]; rs=c.execute("select * from messages where channel_id=? and parent_id is null and deleted=0 and event_id>? and author_id!=?",(cid,last,u["id"])).fetchall(); c.close(); return {"read_state":{"channel_id":cid,"last_read_event_id":last,"unread_count":len(rs),"mention_count":sum(u["id"] in mentions(x["body"],cid,x["author_id"]) for x in rs)}}
@app.post("/api/channels/{cid}/read")
async def readpost(cid:int,request:Request):
 u=auth(request); require_member(cid,u["id"]); d=await request.json(); c=db(); old=c.execute("select coalesce(max(last_read_event_id),0) n from reads where user_id=? and channel_id=?",(u["id"],cid)).fetchone()["n"]; n=max(old,int(d.get("last_read_event_id",0))); c.execute("insert or replace into reads values(?,?,?)",(u["id"],cid,n)); c.commit(); c.close(); return readget(cid,request)
@app.get("/api/search")
def search(request:Request):
 u=auth(request); q=request.query_params.get("q","").lower(); lim,off=pageargs(request); wid=request.query_params.get("workspace_id"); c=db()
 args=[u["id"],"%"+q+"%"]; extra=("and ch.workspace_id=? " if wid else "")
 if wid: args.append(int(wid))
 if request.query_params.get("cursor"):
  try: args.append(int(request.query_params["cursor"]))
  except: raise HTTPException(400)
  extra+="and m.id<? "
 args.append(lim+1)
 rs=c.execute("select m.*,ch.workspace_id from messages m join channels ch on ch.id=m.channel_id join cmembers cm on cm.channel_id=m.channel_id and cm.user_id=? where m.deleted=0 and lower(m.body) like ? "+extra+"order by m.id desc limit ?",args).fetchall()
 c.close(); more=len(rs)>lim; rs=rs[:lim]; return {"results":[msg(x) for x in rs],"next_cursor":str(rs[-1]["id"]) if more and rs else None}
@app.post("/api/messages/{mid}/pin")
def pin(mid:int,request:Request):
 u=auth(request); c=db(); m=c.execute("select * from messages where id=? and deleted=0",(mid,)).fetchone()
 if not m: c.close(); raise HTTPException(404)
 require_member(m["channel_id"],u["id"],True); t=now(); c.execute("insert or ignore into pins values(?,?,?)",(mid,u["id"],t)); c.commit(); c.close(); return {"pin":{"message_id":mid,"pinned_by":u["id"],"pinned_at":t}}
@app.delete("/api/messages/{mid}/pin")
def unpin(mid:int,request:Request):
 u=auth(request); c=db(); m=c.execute("select * from messages where id=?",(mid,)).fetchone()
 if not m: c.close(); raise HTTPException(404)
 require_member(m["channel_id"],u["id"],True); c.execute("delete from pins where message_id=?",(mid,)); c.commit(); c.close(); return {"unpinned":True}
@app.get("/api/channels/{cid}/pins")
def pins(cid:int,request:Request):
 u=auth(request); require_member(cid,u["id"]); lim,off=pageargs(request); c=db(); cur=request.query_params.get("cursor"); args=[cid]; extra=""
 if cur:
  try: args.append(int(cur))
  except: raise HTTPException(400)
  extra=" and p.message_id<?"
 args.append(lim+1); c=db(); rs=c.execute("select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=?"+extra+" order by p.message_id desc limit ?",args).fetchall(); c.close(); more=len(rs)>lim; rs=rs[:lim]
 return {"pins":[{"message":msg(x),"pinned_by":x["pinned_by"],"pinned_at":x["pinned_at"]} for x in rs],"next_cursor":str(rs[-1]["message_id"]) if more and rs else None}
@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
 t=ws.query_params.get("token",""); c=db(); r=c.execute("select user_id from tokens where token=?",(t,)).fetchone(); c.close()
 if not r: await ws.close(code=1008); return
 await ws.accept(); subs={}
 try:
  while True:
   try: data=await asyncio.wait_for(ws.receive_json(),.25)
   except asyncio.TimeoutError: data=None
   if data:
    cid=int(data.get("channel_id",0)); c=db(); ok=c.execute("select 1 from cmembers where channel_id=? and user_id=?",(cid,r["user_id"])).fetchone(); head=c.execute("select coalesce(max(event_id),0) n from events where channel_id=?",(cid,)).fetchone()["n"]; 
    if not ok: c.close(); continue
    if data["type"]=="subscribe": subs[cid]=head; await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
    elif data["type"]=="resume":
     since=int(data.get("since_event_id",0)); es=c.execute("select * from events where channel_id=? and event_id>? order by event_id",(cid,since)).fetchall()
     for e in es: await ws.send_json(dict({"type":e["type"],"event_id":e["event_id"],"channel_id":cid},**json.loads(e["payload"])))
     subs[cid]=head; await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
    c.close()
   for cid,last in list(subs.items()):
    c=db(); es=c.execute("select * from events where channel_id=? and event_id>? order by event_id",(cid,last)).fetchall()
    for e in es: await ws.send_json(dict({"type":e["type"],"event_id":e["event_id"],"channel_id":cid},**json.loads(e["payload"]))); subs[cid]=e["event_id"]
    c.close()
 except (WebSocketDisconnect,Exception): pass
@app.get("/")
def home(): return HTMLResponse("<!doctype html><html><body><h1>Huddle</h1><form data-testid='auth-form'><input name='username'><input name='password' type='password'><button data-testid='auth-submit' data-button-role='primary'>Login</button></form></body></html>")
@app.exception_handler(HTTPException)
async def eh(request,exc): return JSONResponse({"detail":exc.detail},status_code=exc.status_code)
