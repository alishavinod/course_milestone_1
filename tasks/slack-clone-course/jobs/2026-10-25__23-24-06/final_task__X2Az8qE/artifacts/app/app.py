import os,re,json,sqlite3,secrets,hashlib,time,asyncio,base64
from datetime import datetime,timezone
from fastapi import FastAPI,Request,HTTPException,WebSocket,WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
DB='/app/data.db'; os.makedirs('/app',exist_ok=True)
app=FastAPI()
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mention_json TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,target_id INTEGER,target_username TEXT,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_event INTEGER,PRIMARY KEY(user_id,channel_id));'''); c.commit(); c.close()
init()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('authorization','')
 if not h.lower().startswith('bearer '): raise HTTPException(401)
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h.split(None,1)[1],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def tokuser(t):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def err(code,msg='error'): raise HTTPException(code,msg)
def wsrow(slug):
 c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close()
 if not r: err(404)
 return r
def role(uid,wid):
 c=db(); r=c.execute('SELECT role FROM members WHERE user_id=? AND workspace_id=?',(uid,wid)).fetchone(); c.close(); return r['role'] if r else None
def chan(cid):
 c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 if not r: err(404)
 return r
def channelobj(r): return {**dict(r),'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'is_archived':bool(r['is_archived'])}
def messageobj(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 reacts=[]; files=[] 
 out=dict(r); out.pop('deleted',None); out['author']=userobj(u); out['reply_count']=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; out['files']=files; out['reactions']=reacts
 out['mentions']=json.loads(out.pop('mention_json','[]')) if 'mention_json' in out and out.get('mention_json') else []
 c.close(); return out
def page_params(req):
 try: lim=int(req.query_params.get('limit','50'))
 except: err(400)
 if not 1<=lim<=200: err(400)
 return lim,req.query_params.get('cursor')
def page(items,lim,cursor):
 ids=[str(x['id']) for x in items]
 if cursor:
  try:
   z=json.loads(base64.urlsafe_b64decode(cursor.encode()+b'='*((4-len(cursor)%4)%4)))
   snap=z['ids']; pos=int(z['pos'])
   if any(i not in snap for i in ids): items=[x for x in items if str(x['id']) in snap]
   items=sorted(items,key=lambda x:snap.index(str(x['id'])))
  except Exception: err(400)
 else: snap=ids; pos=0
 x=items[pos:pos+lim]; np=pos+lim
 nxt=None if np>=len(snap) else base64.urlsafe_b64encode(json.dumps({'ids':snap,'pos':np},separators=(',',':')).encode()).decode().rstrip('=')
 return x,nxt
def add_event(c,cid,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload))); return n
def visible(uid,cid):
 r=chan(cid); rr=role(uid,r['workspace_id'])
 if rr is None: return False
 if rr=='owner': return True
 c=db(); x=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close()
 return bool(x) and (not r['is_private'] or bool(x))
@app.get('/api/health')
async def health(request:Request):
 return {'status':'ok','node_id':int(request.url.port or 8000)-8000}
class Body(BaseModel): pass
@app.post('/api/auth/register')
async def register(request:Request):
 d=await request.json(); u=d.get('username',''); p=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8: err(400)
 c=db()
 try: c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,p,d.get('display_name') or u)); uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return JSONResponse({'user':userobj(r),'token':t},201)
@app.post('/api/auth/login')
async def login(request:Request):
 d=await request.json(); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
 if not r: c.close(); err(401)
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return {'user':userobj(r),'token':t}
@app.get('/api/auth/me')
async def me(request:Request): return {'user':userobj(auth(request))}
@app.get('/api/users/{uid}')
async def getuser(uid:int,request:Request):
 auth(request); c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 if not r: err(404)
 return {'user':userobj(r)}
@app.get('/api/workspaces')
async def workspaces(request:Request):
 u=auth(request); c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close(); return {'workspaces':[dict(x) for x in rs]}
@app.post('/api/workspaces')
async def createws(request:Request):
 u=auth(request); d=await request.json(); s=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',s): err(400)
 c=db()
 try:
  c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(s,d.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); gid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cmembers VALUES(?,?)',(gid,u['id'])); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); g=c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone(); c.close(); return JSONResponse({'workspace':dict(w),'general_channel':channelobj(g)},201)
@app.get('/api/workspaces/{slug}')
async def getws(slug:str,request:Request):
 u=auth(request); w=wsrow(slug); c=db(); include=request.query_params.get('include_archived') in ('1','true'); rs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(include))).fetchall(); rs=[x for x in rs if not x['is_private'] or role(u['id'],w['id'])=='owner' or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone()]
 states=[]
 for x in rs:
  q=c.execute('SELECT last_event FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone(); last=q['last_event'] if q else 0
  states.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':0,'mention_count':0})
 c.close(); return {'workspace':dict(w),'channels':[channelobj(x) for x in rs],'read_state':states}
@app.get('/api/workspaces/{slug}/members')
async def members(slug,request:Request):
 u=auth(request); w=wsrow(slug)
 if not role(u['id'],w['id']): err(403)
 c=db(); rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall(); c.close(); return {'members':[dict(x) for x in rs]}
@app.patch('/api/workspaces/{slug}')
async def patchws(slug,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(u['id'],w['id']) not in ('owner','admin'): err(403)
 d=await request.json()
 if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'): err(400)
 c=db(); c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id'])); c.commit(); r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone(); c.close(); return {'workspace':dict(r)}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patchmember(slug,uid:int,request:Request):
 u=auth(request); w=wsrow(slug); rr=role(u['id'],w['id']); d=await request.json(); nr=d.get('role')
 if nr not in ('owner','admin','member','guest'): err(400)
 if nr=='owner': err(400)
 old=role(uid,w['id'])
 if rr not in ('owner','admin') or old=='owner' or (rr=='admin' and old=='admin'): err(403)
 c=db(); c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],uid)); c.commit(); c.close(); return {'member':{'user_id':uid,'role':nr}}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(u['id'],w['id'])!='owner': err(403)
 d=await request.json(); nid=d.get('new_owner_id')
 if role(nid,w['id'])!='admin': err(400)
 c=db(); c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])); c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],nid)); c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(nid,w['id'])); c.commit(); c.close(); return {'workspace':dict(wsrow(slug))}
@app.post('/api/workspaces/{slug}/channels')
async def createch(slug,request:Request):
 u=auth(request); w=wsrow(slug); rr=role(u['id'],w['id']); d=await request.json(); name=d.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(d.get('topic',''))>250: err(400)
 if rr not in ('owner','admin','member') or (d.get('is_private',False) and rr not in ('owner','admin')): err(403)
 c=db()
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(): c.close(); err(409)
 try: c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic',''))); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit()
 except sqlite3.IntegrityError: c.close(); err(409)
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return JSONResponse({'channel':channelobj(r)},201)
@app.post('/api/channels/{cid}/join')
async def join(cid:int,request:Request):
 u=auth(request); r=chan(cid); rr=role(u['id'],r['workspace_id']); c=db(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
 if r['is_private']: err(403)
 if rr=='guest' or (not rr and w['join_mode']!='open'): err(403)
 if not rr: c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); add_event(c,cid,'member.joined',{'user_id':u['id']}); c.commit(); c.close(); return {'channel':channelobj(r)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,request:Request):
 u=auth(request); r=chan(cid); c=db(); c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])); add_event(c,cid,'member.left',{'user_id':u['id']}); c.commit(); c.close(); return {'channel':channelobj(r)}
@app.get('/api/channels/{cid}/members')
async def chmembers(cid:int,request:Request):
 u=auth(request); r=chan(cid); c=db()
 if not role(u['id'],r['workspace_id']): err(403)
 rs=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall(); c.close(); return {'members':[userobj(x) for x in rs]}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,request:Request):
 u=auth(request); r=chan(cid); rr=role(u['id'],r['workspace_id'])
 if rr is None: err(403)
 if rr!='owner' and not visible(u['id'],cid): err(403)
 d=await request.json()
 if 'topic' not in d or len(d['topic'])>250: err(400)
 c=db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],cid)); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); add_event(c,cid,'channel.updated',{'channel':channelobj(x)}); c.commit(); c.close(); return {'channel':channelobj(x)}
@app.post('/api/channels/{cid}/archive')
@app.post('/api/channels/{cid}/unarchive')
async def archive(cid:int,request:Request):
 act=request.url.path.rsplit('/',1)[-1]
 u=auth(request); r=chan(cid); 
 if role(u['id'],r['workspace_id'])!='owner' or act not in ('archive','unarchive'): err(403)
 val=act=='archive'; c=db(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid)); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); ev=add_event(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel':channelobj(x)}); c.commit(); c.close(); return {'channel':channelobj(x)}
@app.post('/api/channels/{cid}/messages')
async def postmsg(cid:int,request:Request):
 u=auth(request); r=chan(cid)
 if r['is_archived']: err(423)
 if not visible(u['id'],cid): err(403)
 d=await request.json(); body=d.get('body')
 if not isinstance(body,str) or not body: err(400)
 parent=d.get('parent_id'); c=db()
 if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,cid)).fetchone(): err(404)
 mentions=[]; text=' '+body+' '
 for m in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',body):
  if m=='channel':
   mentions += [x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=? AND user_id!=?',(cid,u['id'])).fetchall()]
  else:
   x=c.execute('SELECT id FROM users WHERE username=?',(m,)).fetchone()
   if x and x['id']!=u['id'] and role(x['id'],r['workspace_id']): mentions.append(x['id'])
 mentions=list(dict.fromkeys(mentions)); at=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mention_json) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,parent,at,json.dumps(mentions))); mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; mo=dict(c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()); c.execute('UPDATE messages SET event_id=? WHERE id=?',(add_event(c,cid,'message.reply' if parent else 'message.created',{'message':mo}),mid)); c.commit(); rr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close(); out=messageobj(rr); return JSONResponse({'message':out},201)
def listmsgs(uid,cid,reply=None):
 r=chan(cid)
 if not visible(uid,cid): err(403)
 c=db(); q='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND '+('parent_id=?' if reply else 'parent_id IS NULL')+' ORDER BY '+('created_at ASC' if reply else 'id DESC')
 return c.execute(q,(cid,reply) if reply else (cid,)).fetchall()
@app.get('/api/channels/{cid}/messages')
async def messages(cid:int,request:Request):
 u=auth(request); lim,st=page_params(request); rows=listmsgs(u['id'],cid); x,n=page(rows,lim,st); return {'messages':[messageobj(r) for r in x],'next_cursor':n}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,request:Request):
 u=auth(request); c=db(); p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close()
 if not p: err(404)
 lim,st=page_params(request); rows=listmsgs(u['id'],p['channel_id'],mid); x,n=page(rows,lim,st); return {'replies':[messageobj(r) for r in x],'next_cursor':n}
@app.get('/api/search')
async def search(request:Request):
 u=auth(request); lim,st=page_params(request); q=request.query_params.get('q',''); c=db(); rows=c.execute('SELECT m.*,c.workspace_id,c.is_private FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC',('%'+q+'%',)).fetchall(); rows=[r for r in rows if visible(u['id'],r['channel_id']) and (not request.query_params.get('workspace_id') or str(r['workspace_id'])==request.query_params['workspace_id'])]; x,n=page(rows,lim,st); return {'results':[{'message':messageobj(r)} for r in x],'next_cursor':n}
@app.post('/api/messages/{mid}/pin')
async def pin(mid:int,request:Request):
 u=auth(request); c=db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: err(404)
 if not visible(u['id'],m['channel_id']): err(403)
 c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now())); c.commit(); p=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone(); c.close(); return {'pin':dict(p)}
@app.delete('/api/messages/{mid}/pin')
async def unpin(mid:int,request:Request):
 u=auth(request); c=db(); c.execute('DELETE FROM pins WHERE message_id=?',(mid,)); c.commit(); c.close(); return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
async def pins(cid:int,request:Request):
 u=auth(request); r=chan(cid)
 if not visible(u['id'],cid): err(403)
 lim,st=page_params(request); c=db(); rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)).fetchall(); x,n=page(rows,lim,st); return {'pins':[{'message':messageobj(r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in x],'next_cursor':n}
@app.post('/api/workspaces/{slug}/invitations')
async def invite(slug,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(u['id'],w['id']) not in ('owner','admin'): err(403)
 d=await request.json(); code=secrets.token_urlsafe(12); tid=d.get('invited_user_id'); tu=d.get('invited_username'); uses=1 if (tid or tu) else d.get('max_uses',1); c=db(); c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],time.time()+d.get('expires_in_seconds',31536000),uses,0,tid,tu)); c.commit(); r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone(); c.close(); return JSONResponse({'invitation':dict(r)},201)
@app.get('/api/workspaces/{slug}/invitations')
async def invites(slug,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(u['id'],w['id']) not in ('owner','admin'): err(403)
 c=db(); rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND expires>?',(w['id'],time.time())).fetchall(); c.close(); return {'invitations':[dict(x) for x in rs]}
@app.post('/api/invitations/{code}/accept')
async def accept(code,request:Request):
 u=auth(request); c=db(); x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
 if not x or x['revoked'] or x['uses']>=x['max_uses'] or x['expires']<time.time(): err(404)
 if x['target_id'] and x['target_id']!=u['id']: err(403)
 if x['target_username'] and x['target_username']!=u['username']: err(403)
 c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member')); c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,)); c.commit(); c.close(); return {'accepted':True}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
async def revoke(slug,code,request:Request):
 u=auth(request); w=wsrow(slug)
 if role(u['id'],w['id']) not in ('owner','admin'): err(403)
 c=db(); c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,)); c.commit(); c.close(); return {'deleted':True}
def readstate(uid,cid):
 c=db()
 q=c.execute('SELECT last_event FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
 last=q['last_event'] if q else 0
 rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
 unread=mentions=0
 for x in rows:
  if x['kind'] not in ('message.created','message.reply'):
   continue
  msg=(json.loads(x['payload']).get('message') or {})
  if msg.get('parent_id') is not None:
   continue
  if msg.get('author_id') != uid:
   unread += 1
  try:
   mids=json.loads(msg.get('mention_json') or '[]')
  except Exception:
   mids=[]
  if str(uid) in [str(v) for v in mids]:
   mentions += 1
 c.close()
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}

@app.get('/api/channels/{cid}/read')
async def getread(cid:int,request:Request):
 u=auth(request); chan(cid); return {'read_state':readstate(u['id'],cid)}
@app.post('/api/channels/{cid}/read')
async def read(cid:int,request:Request):
 u=auth(request); d=await request.json(); c=db(); x=c.execute('SELECT last_event FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); old=x['last_event'] if x else 0; n=max(old,int(d.get('last_read_event_id',0))); c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],cid,n)); c.commit(); c.close(); return {'read_state':readstate(u['id'],cid)}

@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 u=tokuser(ws.query_params.get('token',''))
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs=set(); last={}
 try:
  while True:
   try:
    msg=await asyncio.wait_for(ws.receive_json(),.2)
    if msg.get('type') in ('subscribe','resume'):
     cid=int(msg['channel_id']); subs.add(cid); since=int(msg.get('since_event_id',0))
     c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.close()
     last[cid]=head if msg['type']=='subscribe' else since
     if msg['type']=='subscribe': await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
     else:
      c=db(); es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall(); c.close()
      for e in es: await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])}); last[cid]=e['event_id']
      await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
   except asyncio.TimeoutError: pass
   for cid in list(subs):
    c=db(); es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall(); c.close()
    for e in es:
     await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])}); last[cid]=e['event_id']
 except (WebSocketDisconnect,Exception): pass
