import os, re, json, sqlite3, secrets, hashlib, base64, asyncio, datetime, threading
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional

DB='/app/data/app.db'; os.makedirs('/app/data',exist_ok=True)
NODE=int(os.getenv('NODE_ID','0')); PORT=int(os.getenv('PORT','8000'))
lock=threading.Lock()
def conn():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 with conn() as c:
  c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,invited_username TEXT,expires REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created REAL);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
''')
init()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('authorization','')
 if not h.lower().startswith('bearer '): raise HTTPException(401)
 with conn() as c:
  r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h.split(None,1)[1],)).fetchone()
 if not r: raise HTTPException(401)
 return r
def auth_ws(token):
 with conn() as c: r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token or '',)).fetchone()
 return r
def bodycheck(x):
 if not isinstance(x,dict): raise HTTPException(400)
def wrow(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def crow(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def member(uid,wid):
 with conn() as c:return c.execute('SELECT role FROM members WHERE user_id=? AND workspace_id=?',(uid,wid)).fetchone()
def channel_access(u,ch,write=False):
 m=member(u['id'],ch['workspace_id'])
 with conn() as c: cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
 with conn() as c: wowner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 owner_ok=bool(wowner and u['id']==wowner['owner_id'])
 if ch['is_private'] and not cm and not owner_ok: raise HTTPException(403)
 if not cm and not (m and owner_ok): raise HTTPException(403)
 if write and ch['is_archived']: raise HTTPException(423)
 return True
def event(cid,kind,payload):
 import time
 for _ in range(30):
  try:
   with conn() as c:
    c.execute('BEGIN IMMEDIATE')
    q=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    c.execute('INSERT INTO events(channel_id,event_id,kind,payload,created) VALUES(?,?,?,?,?)',(cid,q,kind,json.dumps(payload),time.time()))
    c.commit()
    return q
  except sqlite3.OperationalError:
   time.sleep(.02)
 raise HTTPException(503)
def encode(v): return base64.urlsafe_b64encode(json.dumps(v).encode()).decode()
def decode(s):
 try:return json.loads(base64.urlsafe_b64decode(s.encode()).decode())
 except: raise HTTPException(400)

app=FastAPI()
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 x=await req.json(); bodycheck(x); un=x.get('username',''); pw=x.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 try:
  with conn() as c:
   cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,x.get('display_name') or un)); uid=cur.lastrowid
 except sqlite3.IntegrityError: raise HTTPException(409)
 t=secrets.token_urlsafe(24)
 with conn() as c:c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 return {'user':userobj(r),'token':t}
@app.post('/api/auth/login')
async def login(req:Request):
 x=await req.json()
 with conn() as c:r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
 if not r: raise HTTPException(401)
 t=secrets.token_urlsafe(24)
 with conn() as c:c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']))
 return {'user':userobj(r),'token':t}
@app.get('/api/auth/me')
async def me(req:Request): return {'user':userobj(auth(req))}
@app.get('/api/users/{uid}')
async def getuser(uid:int):
 with conn() as c:r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 if not r:raise HTTPException(404)
 return {'user':userobj(r)}
@app.patch('/api/users/me')
async def patchme(req:Request):
 u=auth(req); x=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']
 if 'timezone' in x and (not isinstance(x['timezone'],str) or len(x['timezone'])>64):raise HTTPException(400)
 vals=[x[k] for k in allowed if k in x]; sets=','.join(k+'=?' for k in allowed if k in x)
 if sets:
  with conn() as c:c.execute('UPDATE users SET '+sets+' WHERE id=?',(*vals,u['id']))
 with conn() as c:r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
 return {'user':userobj(r)}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 u=auth(req);x=await req.json()
 if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):raise HTTPException(400)
 try:
  with conn() as c:
   cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(x['slug'],x['name'],u['id'])); wid=cur.lastrowid
   c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']))
 except sqlite3.IntegrityError:raise HTTPException(409)
 with conn() as c:r=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 return {'workspace':wrow(r),'general_channel':crow(ch)}
@app.get('/api/workspaces')
async def listws(req:Request):
 u=auth(req)
 with conn() as c: rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
 return {'workspaces':[wrow(r) for r in rs]}
@app.get('/api/workspaces/{slug}')
async def detail(slug:str,req:Request):
 u=auth(req)
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:raise HTTPException(404)
 with conn() as c:
  cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?)) AND (is_archived=0 OR ?)',(w['id'],u['id'],req.query_params.get('include_archived') in ('1','true'))).fetchall()
  rs=c.execute('SELECT * FROM readstate WHERE user_id=? AND channel_id IN (SELECT id FROM channels WHERE workspace_id=?)',(u['id'],w['id'])).fetchall()
 return {'workspace':wrow(w),'channels':[crow(x) for x in cs],'read_state':[{'channel_id':x['channel_id'],'last_read_event_id':x['last_read_event_id'],'unread_count':0,'mention_count':0} for x in rs]}
@app.get('/api/workspaces/{slug}/members')
async def members(slug:str,req:Request):
 u=auth(req)
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w or not member(u['id'],w['id']):raise HTTPException(403 if w else 404)
 with conn() as c:rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
 return {'members':[dict(x) for x in rs]}
@app.patch('/api/workspaces/{slug}')
async def patchws(slug:str,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 m=member(u['id'],w['id']) if w else None
 if not w:raise HTTPException(404)
 if not m or m['role'] != 'owner':raise HTTPException(403)
 if 'join_mode' in x and x['join_mode'] not in ('open','invite_only'):raise HTTPException(400)
 sets=[];vals=[]
 for k in ('name','join_mode'):
  if k in x:sets.append(k+'=?');vals.append(x[k])
 if sets:
  with conn() as c:c.execute('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',(*vals,w['id']));w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone()
 return {'workspace':wrow(w)}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patchrole(slug:str,uid:int,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:raise HTTPException(404)
 a=member(u['id'],w['id']); target=member(uid,w['id'])
 if x.get('role')=='owner': raise HTTPException(400)
 if not a or a['role'] not in ('owner','admin') or not target or x.get('role') not in ('admin','member','guest'): raise HTTPException(403)
 if uid==w['owner_id'] or uid==u['id'] or (a['role']=='admin' and target['role']=='admin'):raise HTTPException(403)
 with conn() as c:c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(x['role'],w['id'],uid))
 return {'member':{'user_id':uid,'role':x['role']}}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w or u['id']!=w['owner_id']:raise HTTPException(403)
 t=member(x.get('new_owner_id'),w['id'])
 if not t or t['role']!='admin':raise HTTPException(400)
 with conn() as c:c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],x['new_owner_id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(x['new_owner_id'],w['id']))
 return {'workspace':wrow(dict(w))}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createch(slug:str,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:raise HTTPException(404)
 m=member(u['id'],w['id'])
 if not m or m['role']=='guest' or (x.get('is_private',False) and m['role'] not in ('owner','admin')):raise HTTPException(403)
 if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')) or len(x.get('topic',''))>250:raise HTTPException(400)
 with conn() as c:
  if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],x.get('name'))).fetchone(): raise HTTPException(409)
 try:
  with conn() as c:cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic','')));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']))
 except sqlite3.IntegrityError:raise HTTPException(409)
 with conn() as c:r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 return {'channel':crow(r)}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
 u=auth(req)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch else None
 if not ch:raise HTTPException(404)
 if ch['is_private']:raise HTTPException(403)
 m=member(u['id'],w['id'])
 if m and m['role']=='guest':raise HTTPException(403)
 if not m and w['join_mode']=='invite_only':raise HTTPException(403)
 with conn() as c:
  if not m:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
  c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
 event(cid,'member.joined',{'user_id':u['id']})
 return {'channel':crow(ch)}
@app.get('/api/channels/{cid}/members')
async def chmembers(cid:int,req:Request):
 u=auth(req)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 channel_access(u,ch)
 with conn() as c:rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall()
 return {'members':[userobj(x) for x in rs]}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 channel_access(u,ch)
 if len(x.get('topic',''))>250:raise HTTPException(400)
 with conn() as c:c.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',ch['topic']),cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 event(cid,'channel.updated',{'channel':crow(ch)})
 return {'channel':crow(ch)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
 u=auth(req)
 with conn() as c:c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 event(cid,'member.left',{'user_id':u['id']})
 return {'channel':crow(ch)}
@app.post('/api/channels/{cid}/archive')
@app.post('/api/channels/{cid}/unarchive')
async def archive(cid:int,req:Request):
 u=auth(req); action=req.url.path.rsplit('/',1)[-1]
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch else None
 if not ch:raise HTTPException(404)
 if u['id']!=w['owner_id']:raise HTTPException(403)
 with conn() as c:c.execute('UPDATE channels SET is_archived=? WHERE id=?',(action=='archive',cid));ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 event(cid,'channel.archived' if action=='archive' else 'channel.unarchived',{'channel':crow(ch)})
 return {'channel':crow(ch)}
def msgobj(r):
 with conn() as c:
  a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); rr=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)).fetchall()
 reactions={}
 for z in rr: reactions.setdefault(z['emoji'],[]).append(z['user_id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':count_replies(r['id']),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in reactions.items()],'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def count_replies(mid):
 with conn() as c:return c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(mid,)).fetchone()[0]
def mentions(body,cid,author):
 with conn() as c:
  ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone(); mem=c.execute('SELECT u.id,u.username FROM users u JOIN members m ON m.user_id=u.id WHERE m.workspace_id=?',(ch['workspace_id'],)).fetchall();cm={x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(cid,))}
 out=[]
 for x in mem:
  if x['id']!=author and x['id'] in cm and re.search(r'(?<![\w@])@'+re.escape(x['username'])+r'(?!\w)',body):out.append(x['id'])
 if re.search(r'(?<!\w)@channel(?!\w)',body):
  out += [x for x in cm if x!=author]
 return list(dict.fromkeys(out))
@app.post('/api/channels/{cid}/messages',status_code=201)
async def postmsg(cid:int,req:Request):
 u=auth(req);x=await req.json(); body=x.get('body','')
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 channel_access(u,ch,True)
 if not isinstance(body,str) or not body.strip():raise HTTPException(400)
 parent=x.get('parent_id')
 if parent:
  with conn() as c:
   if not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,cid)).fetchone():raise HTTPException(400)
 with conn() as c:
  cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,parent,now()));mid=cur.lastrowid;r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 m=msgobj(r); ev=event(cid,'message.reply' if parent else 'message.created',{'message':m})
 with conn() as c:c.execute('UPDATE messages SET event_id=? WHERE id=?',(ev,mid))
 return {'message':m}
@app.get('/api/channels/{cid}/messages')
async def messages(cid:int,req:Request):
 u=auth(req)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 channel_access(u,ch); limit=int(req.query_params.get('limit','50'))
 if limit<1 or limit>200:raise HTTPException(400)
 cur=req.query_params.get('cursor'); where='channel_id=? AND parent_id IS NULL AND deleted=0'; args=[cid]
 if cur:
  z=decode(cur)
  if not isinstance(z,list):raise HTTPException(400)
  where+=' AND (created_at<? OR (created_at=? AND id<?))';args += [z[0],z[0],z[1]]
 with conn() as c:rs=c.execute('SELECT * FROM messages WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT ?',(*args,limit+1)).fetchall()
 nxt=encode([rs[limit-1]['created_at'],rs[limit-1]['id']]) if len(rs)>limit else None
 return {'messages':[msgobj(r) for r in rs[:limit]],'next_cursor':nxt}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
 u=auth(req)
 with conn() as c:r0=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r0:raise HTTPException(404)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(r0['channel_id'],)).fetchone()
 channel_access(u,ch);limit=int(req.query_params.get('limit','50'))
 if limit<1 or limit>200:raise HTTPException(400)
 cur=req.query_params.get('cursor');args=[mid];where='parent_id=? AND deleted=0'
 if cur:
  z=decode(cur);where+=' AND (created_at>? OR (created_at=? AND id>?))';args += [z[0],z[0],z[1]]
 with conn() as c:rs=c.execute('SELECT * FROM messages WHERE '+where+' ORDER BY created_at,id LIMIT ?',(*args,limit+1)).fetchall()
 return {'replies':[msgobj(r) for r in rs[:limit]],'next_cursor':encode([rs[limit-1]['created_at'],rs[limit-1]['id']]) if len(rs)>limit else None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:raise HTTPException(404)
 if r['author_id']!=u['id']:raise HTTPException(403)
 if r['deleted']:raise HTTPException(409)
 if not x.get('body','').strip():raise HTTPException(400)
 with conn() as c:c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(x['body'],now(),mid));r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 return {'message':msgobj(r)}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
 u=auth(req)
 with conn() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:raise HTTPException(404)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone();w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if r['author_id']!=u['id'] and w['owner_id']!=u['id']:raise HTTPException(403)
 with conn() as c:c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,))
 return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def react(mid:int,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:raise HTTPException(404)
 try:
  with conn() as c:c.execute('INSERT INTO reactions VALUES(?,?,?)',(mid,u['id'],x.get('emoji','')))
 except:pass
 with conn() as c:rs=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(mid,)).fetchall()
 d={}
 for z in rs:d.setdefault(z['emoji'],[]).append(z['user_id'])
 return {'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def unreact(mid:int,emoji:str,req:Request):
 u=auth(req)
 with conn() as c:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
 return await react(mid,Request({'type':'http','headers':{}}))
@app.get('/api/search')
async def search(req:Request):
 u=auth(req);q=req.query_params.get('q','');limit=int(req.query_params.get('limit','50'))
 if limit<1 or limit>200: raise HTTPException(400)
 wid=req.query_params.get('workspace_id'); cur=req.query_params.get('cursor'); args=['%'+q+'%',u['id']]
 where='m.deleted=0 AND m.body LIKE ? AND (ch.is_private=0 OR ch.id IN (SELECT channel_id FROM channel_members WHERE user_id=?))'
 if wid: where+=' AND ch.workspace_id=?'; args.append(int(wid))
 if cur:
  z=decode(cur)
  if not isinstance(z,list) or len(z)!=2: raise HTTPException(400)
  where+=' AND (m.created_at<? OR (m.created_at=? AND m.id<?))'; args += [z[0],z[0],z[1]]
 with conn() as c:rs=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE '+where+' ORDER BY m.created_at DESC,m.id DESC LIMIT ?',(*args,limit+1)).fetchall()
 nxt=encode([rs[limit-1]['created_at'],rs[limit-1]['id']]) if len(rs)>limit else None
 return {'results':[msgobj(r) for r in rs[:limit]],'next_cursor':nxt}
@app.post('/api/messages/{mid}/pin')
async def pin(mid:int,req:Request):
 u=auth(req)
 with conn() as c:r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:raise HTTPException(404)
 with conn() as c:c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));p=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
 return {'pin':dict(p)}
@app.delete('/api/messages/{mid}/pin')
async def unpin(mid:int,req:Request):
 auth(req)
 with conn() as c:c.execute('DELETE FROM pins WHERE message_id=?',(mid,))
 return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
async def pins(cid:int,req:Request):
 u=auth(req)
 with conn() as c:ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:raise HTTPException(404)
 channel_access(u,ch); limit=int(req.query_params.get('limit','50'))
 if limit<1 or limit>200:raise HTTPException(400)
 cur=req.query_params.get('cursor'); args=[cid]; where='m.channel_id=?'
 if cur:
  z=decode(cur)
  if not isinstance(z,list) or len(z)!=2:raise HTTPException(400)
  where+=' AND (p.pinned_at<? OR (p.pinned_at=? AND p.message_id<?))';args += [z[0],z[0],z[1]]
 with conn() as c:rs=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE '+where+' ORDER BY p.pinned_at DESC,p.message_id DESC LIMIT ?',(*args,limit+1)).fetchall()
 nxt=encode([rs[limit-1]['pinned_at'],rs[limit-1]['message_id']]) if len(rs)>limit else None
 return {'pins':[{'message':msgobj(r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rs[:limit]],'next_cursor':nxt}
@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def invite(slug:str,req:Request):
 u=auth(req);x=await req.json()
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 m=member(u['id'],w['id']) if w else None
 if not w:raise HTTPException(404)
 if not m or m['role'] not in ('owner','admin'):raise HTTPException(403)
 code=secrets.token_urlsafe(12); ex=__import__('time').time()+x.get('expires_in_seconds',86400)
 with conn() as c:c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],x.get('invited_user_id'),x.get('invited_username'),ex,x.get('max_uses',1),0,0))
 return {'invitation':{'code':code,'workspace_id':w['id'],'max_uses':x.get('max_uses',1)}}
@app.get('/api/workspaces/{slug}/invitations')
async def invites(slug:str,req:Request):
 u=auth(req)
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 m=member(u['id'],w['id']) if w else None
 if not w:raise HTTPException(404)
 if not m or m['role'] not in ('owner','admin'):raise HTTPException(403)
 with conn() as c:rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0',(w['id'],)).fetchall()
 return {'invitations':[dict(x) for x in rs]}
@app.post('/api/invitations/{code}/accept')
async def accept(code:str,req:Request):
 u=auth(req)
 with conn() as c:i=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
 if not i or i['revoked'] or i['uses']>=i['max_uses'] or i['expires']<__import__('time').time():raise HTTPException(404)
 if i['invited_user_id'] and i['invited_user_id']!=u['id']:raise HTTPException(403)
 if i['invited_username'] and i['invited_username']!=u['username']:raise HTTPException(403)
 with conn() as c:c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(i['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,))
 return {'accepted':True}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
async def revoke(slug:str,code:str,req:Request):
 u=auth(req)
 with conn() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 m=member(u['id'],w['id']) if w else None
 if not w:raise HTTPException(404)
 if not m or m['role'] not in ('owner','admin'):raise HTTPException(403)
 with conn() as c:c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,))
 return {'deleted':True}
def read_obj(uid,cid,eid):
 with conn() as c:
  rows=c.execute("""SELECT m.*,e.event_id FROM messages m JOIN events e ON e.channel_id=m.channel_id AND e.event_id=m.event_id
                    WHERE m.channel_id=? AND m.event_id>? AND m.deleted=0 AND m.parent_id IS NULL
                    ORDER BY m.event_id""",(cid,eid)).fetchall()
 mentions_n=sum(1 for r in rows if uid in mentions(r['body'],cid,r['author_id']))
 return {'channel_id':cid,'last_read_event_id':eid,'unread_count':len(rows),'mention_count':mentions_n}
@app.get('/api/channels/{cid}/read')
async def getread(cid:int,req:Request):
 u=auth(req)
 with conn() as c:r=c.execute('SELECT last_read_event_id FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
 eid=r['last_read_event_id'] if r else 0
 return {'read_state':read_obj(u['id'],cid,eid)}
@app.post('/api/channels/{cid}/read')
async def postread(cid:int,req:Request):
 u=auth(req);x=await req.json();eid=int(x.get('last_read_event_id',0))
 with conn() as c:
  r=c.execute('SELECT last_read_event_id FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
  eid=max(eid,r['last_read_event_id'] if r else 0)
  c.execute('INSERT OR REPLACE INTO readstate VALUES(?,?,?)',(u['id'],cid,eid))
 return {'read_state':read_obj(u['id'],cid,eid)}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 u=auth_ws(ws.query_params.get('token'))
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs={}; last={}
 try:
  while True:
   try:
    data=await asyncio.wait_for(ws.receive_json(),.2)
    typ=data.get('type'); cid=int(data.get('channel_id',0))
    if typ in ('subscribe','resume'):
     since=int(data.get('since_event_id',0)); subs[cid]=True; last[cid]=since
     with conn() as c: head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
     if typ=='subscribe':
      last[cid]=head
      await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
     else:
      with conn() as c: rs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
      for r in rs:
       p=json.loads(r['payload']); await ws.send_json({'type':r['kind'],'event_id':r['event_id'],'channel_id':cid,**p}); last[cid]=r['event_id']
      await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
   except asyncio.TimeoutError: pass
   for cid in list(subs):
    with conn() as c: rs=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last[cid])).fetchall()
    for r in rs:
     p=json.loads(r['payload']); await ws.send_json({'type':r['kind'],'event_id':r['event_id'],'channel_id':cid,**p});last[cid]=r['event_id']
 except (WebSocketDisconnect,Exception): return
@app.get('/')
async def root():
    return HTMLResponse('<!doctype html><html><head><title>Huddle</title></head><body><div id="app"><h1>Huddle</h1></div></body></html>')
