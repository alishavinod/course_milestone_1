import os,re,json,sqlite3,secrets,hashlib,asyncio,datetime,time
from fastapi import FastAPI,Request,Response,HTTPException,WebSocket,WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
DB='/app/data/app.db'; os.makedirs('/app/data',exist_ok=True)
app=FastAPI()
NODE=int(os.getenv('NODE_ID','0'))
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def init():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000'); c.execute('PRAGMA journal_mode=WAL')
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created REAL);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created REAL,edited REAL,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at REAL);
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,target_id INTEGER,revoked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_event INTEGER,PRIMARY KEY(user_id,channel_id));''')
 c.commit();c.close()
init()
def now(): return time.time()
def iso(t=None):
 t=t or now(); return datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def auth(req):
 h=req.headers.get('authorization','')
 if not h.startswith('Bearer '): raise HTTPException(401)
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone();c.close()
 if not r: raise HTTPException(401)
 return r
def tok(uid):
 t=secrets.token_urlsafe(24); c=db();c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();c.close();return t
def user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':None,'status_text':'','status_emoji':''}
def getws(c,slug):
 r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not r: raise HTTPException(404)
 return r
def role(c,w,u): 
 r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
 return r['role'] if r else None
def chan(c,i):
 r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
 if not r: raise HTTPException(404)
 return r
def co(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['archived'])}
def accessible(c,ch,uid):
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 return bool(c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone()) or w['owner_id']==uid
def event(c,cid,typ,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,typ,json.dumps(payload))); return n
def msgobj(c,r):
 au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 reps=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':reps,'created_at':iso(r['created']),'edited_at':iso(r['edited']) if r['edited'] else None,'files':[],'reactions':[],'mentions':json.loads(r['mentions'] or '[]')}
def page_args(req):
 try: lim=int(req.query_params.get('limit','50'))
 except: raise HTTPException(400)
 if lim<1 or lim>200: raise HTTPException(400)
 return lim,req.query_params.get('cursor')
def cursor_encode(x): return secrets.token_urlsafe(8)+'.'+str(x)
def cursor_decode(s):
 if not s or '.' not in s: raise HTTPException(400)
 try:return int(s.rsplit('.',1)[1])
 except:raise HTTPException(400)
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 d=await req.json(); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 c=db()
 try:c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un,now()));uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.commit()
 except sqlite3.IntegrityError: c.close();raise HTTPException(409)
 c.close();return {'user':user({'id':uid,'username':un,'display_name':d.get('display_name') or un}),'token':tok(uid)}
@app.post('/api/auth/login')
async def login(req:Request):
 d=await req.json();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone();c.close()
 if not r:raise HTTPException(401)
 return {'user':user(r),'token':tok(r['id'])}
@app.get('/api/auth/me')
def me(req:Request):return {'user':user(auth(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
 c=db();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
 if not r:raise HTTPException(404)
 return {'user':user(r)}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 u=auth(req);d=await req.json(); slug=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'):raise HTTPException(400)
 c=db()
 try:c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,"open")',(slug,d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
 except sqlite3.IntegrityError:c.close();raise HTTPException(409)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'workspace':dict(w),'general_channel':co(ch)}
@app.get('/api/workspaces')
def listws(req:Request):
 u=auth(req);c=db();rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[dict(x) for x in rs]}
@app.get('/api/workspaces/{slug}')
def detail(slug:str,req:Request):
 u=auth(req);c=db();w=getws(c,slug)
 if not role(c,w['id'],u['id']):c.close();raise HTTPException(403)
 include=req.query_params.get('include_archived','') in ('1','true'); af='1=1' if include else 'archived=0'; cs=c.execute('SELECT ch.* FROM channels ch WHERE ch.workspace_id=? AND '+af+' AND (ch.is_private=0 OR ch.id IN (SELECT channel_id FROM channel_members WHERE user_id=?) OR ch.workspace_id IN (SELECT workspace_id FROM members WHERE user_id=? AND role=\'owner\'))',(w['id'],u['id'],u['id'])).fetchall();rs=[]
 for ch in cs: rs.append(readstate(c,ch['id'],u['id']))
 c.close();return {'workspace':dict(w),'channels':[co(x) for x in cs],'read_state':rs}
@app.get('/api/workspaces/{slug}/members')
def members(slug,req:Request):
 u=auth(req);c=db();w=getws(c,slug)
 if not role(c,w['id'],u['id']):raise HTTPException(403)
 rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return {'members':[dict(x) for x in rs]}
@app.patch('/api/workspaces/{slug}')
async def patchws(slug,req:Request):
 u=auth(req);d=await req.json();c=db();w=getws(c,slug)
 if role(c,w['id'],u['id'])!='owner':raise HTTPException(403)
 if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):raise HTTPException(400)
 c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit();r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return {'workspace':dict(r)}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patchmember(slug,uid:int,req:Request):
 u=auth(req);d=await req.json();c=db();w=getws(c,slug);me=role(c,w['id'],u['id']); old=role(c,w['id'],uid);new=d.get('role')
 if new=='owner' or new not in ('admin','member','guest'):raise HTTPException(400)
 if me not in ('owner','admin') or uid==w['owner_id'] or (me=='admin' and old=='admin'):raise HTTPException(403)
 c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],uid));c.commit();c.close();return {'member':{'user_id':uid,'role':new}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createch(slug,req:Request):
 u=auth(req);d=await req.json();c=db();w=getws(c,slug);r=role(c,w['id'],u['id']); name=d.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(d.get('topic',''))>250:raise HTTPException(400)
 if r=='guest' or r is None or (d.get('is_private',False) and r not in ('owner','admin')):raise HTTPException(403)
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():raise HTTPException(409)
 c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));i=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(i,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return {'channel':co(r)}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
 u=auth(req);c=db();ch=chan(c,cid);w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();rr=role(c,w['id'],u['id'])
 if ch['is_private'] or rr=='guest' or (not rr and w['join_mode']!='open'):raise HTTPException(403)
 if not rr:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));event(c,cid,'member.joined',{'user_id':u['id']});c.commit();c.close();return {'channel':co(ch)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
 u=auth(req);c=db();ch=chan(c,cid);c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']));event(c,cid,'member.left',{'user_id':u['id']});c.commit();c.close();return {'channel':co(ch)}
@app.get('/api/channels/{cid}/members')
def chmembers(cid:int,req:Request):
 u=auth(req);c=db();ch=chan(c,cid)
 if not accessible(c,ch,u['id']):raise HTTPException(403)
 rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return {'members':[user(x) for x in rs]}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,req:Request):
 u=auth(req);d=await req.json();c=db();ch=chan(c,cid);w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if not accessible(c,ch,u['id']) and w['owner_id']!=u['id']:raise HTTPException(403)
 if len(d.get('topic',''))>250:raise HTTPException(400)
 c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',ch['topic']),cid));event(c,cid,'channel.updated',{'channel_id':cid});c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':co(r)}
@app.post('/api/channels/{cid}/archive')
async def archive(cid:int,req:Request):
 u=auth(req);c=db();ch=chan(c,cid);w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if w['owner_id']!=u['id']:raise HTTPException(403)
 c.execute('UPDATE channels SET archived=1 WHERE id=?',(cid,));event(c,cid,'channel.archived',{});c.commit();c.close();return {'channel':co(dict(ch)|{'archived':1})}
@app.post('/api/channels/{cid}/unarchive')
async def unarchive(cid:int,req:Request):
 u=auth(req);c=db();ch=chan(c,cid);w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if w['owner_id']!=u['id']:raise HTTPException(403)
 c.execute('UPDATE channels SET archived=0 WHERE id=?',(cid,));event(c,cid,'channel.unarchived',{});c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':co(r)}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def postmsg(cid:int,req:Request):
 u=auth(req);d=await req.json();body=d.get('body','')
 if not isinstance(body,str) or not body:raise HTTPException(400)
 c=db();ch=chan(c,cid)
 if not accessible(c,ch,u['id']):raise HTTPException(403)
 if ch['archived']:raise HTTPException(423)
 mentions=[];w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mids=[x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(cid,))]
 if '@channel' in body:mentions=[x for x in mids if x!=u['id']]
 for m in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)\b',body):
  x=c.execute('SELECT id FROM users WHERE username=?',(m,)).fetchone()
  if x and x['id'] in mids and x['id']!=u['id']:mentions.append(x['id'])
 mentions=list(dict.fromkeys(mentions)); parent=d.get('parent_id')
 if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,cid)).fetchone():raise HTTPException(404)
 c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created,mentions) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,parent,now(),json.dumps(mentions)));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();obj=msgobj(c,r);event(c,cid,'message.reply' if parent else 'message.created',obj);c.commit();c.close();return {'message':obj}
@app.get('/api/channels/{cid}/messages')
def messages(cid:int,req:Request):
 u=auth(req);lim,cur=page_args(req);c=db();ch=chan(c,cid)
 if not accessible(c,ch,u['id']):raise HTTPException(403)
 before=cursor_decode(cur) if cur else 10**18
 rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(cid,before,lim+1)).fetchall(); more=len(rs)>lim;rs=rs[:lim];out={'messages':[msgobj(c,x) for x in rs],'next_cursor':cursor_encode(rs[-1]['id']) if more else None};c.close();return out
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request):
 u=auth(req);lim,cur=page_args(req);c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not p:raise HTTPException(404)
 if not accessible(c,chan(c,p['channel_id']),u['id']):raise HTTPException(403)
 before=cursor_decode(cur) if cur else 0
 rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?',(mid,before,lim+1)).fetchall();more=len(rs)>lim;rs=rs[:lim];c.close();return {'replies':[msgobj(db(),x) for x in rs],'next_cursor':cursor_encode(rs[-1]['id']) if more else None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
 u=auth(req);d=await req.json();c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r or r['deleted']:raise HTTPException(404)
 if r['author_id']!=u['id']:raise HTTPException(403)
 c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(d.get('body',''),now(),mid));r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();o=msgobj(c,r);event(c,r['channel_id'],'message.updated',o);c.commit();c.close();return {'message':o}
@app.delete('/api/messages/{mid}')
def delete(mid:int,req:Request):
 u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:return {'deleted':True}
 w=c.execute('SELECT * FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],)).fetchone()
 if r['author_id']!=u['id'] and w['owner_id']!=u['id']:raise HTTPException(403)
 c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));event(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit();c.close();return {'deleted':True}
@app.get('/api/search')
def search(req:Request):
 u=auth(req);q=req.query_params.get('q','');lim,cur=page_args(req);c=db();before=cursor_decode(cur) if cur else 10**18;wid=req.query_params.get('workspace_id')
 rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id<? AND m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC LIMIT ?',(before,'%'+q+'%',lim*5)).fetchall();out=[]
 for r in rows:
  if wid and str(r['channel_id']) not in [str(x['id']) for x in c.execute('SELECT id FROM channels WHERE workspace_id=?',(wid,))]:continue
  if accessible(c,chan(c,r['channel_id']),u['id']):out.append(msgobj(c,r))
 out=out[:lim];c.close();return {'results':out,'next_cursor':cursor_encode(out[-1]['id']) if len(out)==lim else None}
@app.post('/api/messages/{mid}/pin')
async def pin(mid:int,req:Request):
 u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:raise HTTPException(404)
 if not accessible(c,chan(c,r['channel_id']),u['id']):raise HTTPException(403)
 c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone();c.commit();c.close();return {'pin':{'message_id':mid,'pinned_by':x['pinned_by'],'pinned_at':iso(x['pinned_at'])}}
@app.delete('/api/messages/{mid}/pin')
def unpin(mid:int,req:Request):
 auth(req);c=db();c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();c.close();return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
def pins(cid:int,req:Request):
 u=auth(req);lim,cur=page_args(req);c=db();ch=chan(c,cid)
 if not accessible(c,ch,u['id']):raise HTTPException(403)
 rs=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ?',(cid,lim+1)).fetchall();more=len(rs)>lim;rs=rs[:lim];out=[{'message':msgobj(c,r),'pinned_by':r['pinned_by'],'pinned_at':iso(r['pinned_at'])} for r in rs];c.close();return {'pins':out,'next_cursor':None if not more else cursor_encode(rs[-1]['message_id'])}
def readstate(c,cid,uid):
 r=c.execute('SELECT last_event FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone();last=r['last_event'] if r else 0
 rows=c.execute('SELECT e.event_id,e.payload FROM events e WHERE e.channel_id=? AND e.event_id>? AND e.type IN ("message.created","message.reply")',(cid,last)).fetchall();un=men=0
 for x in rows:
  p=json.loads(x['payload']); 
  if p.get('author_id')!=uid:un+=1
  if uid in p.get('mentions',[]):men+=1
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':un,'mention_count':men}
@app.get('/api/channels/{cid}/read')
def getread(cid:int,req:Request):
 u=auth(req);c=db();x=readstate(c,cid,u['id']);c.close();return {'read_state':x}
@app.post('/api/channels/{cid}/read')
async def postread(cid:int,req:Request):
 u=auth(req);d=await req.json();c=db();old=c.execute('SELECT last_event FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();v=max(old['last_event'] if old else 0,int(d.get('last_read_event_id',0)));c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],cid,v));c.commit();x=readstate(c,cid,u['id']);c.close();return {'read_state':x}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer_ownership(slug,req:Request):
 u=auth(req); d=await req.json(); c=db(); w=getws(c,slug)
 if role(c,w['id'],u['id'])!='owner': c.close(); raise HTTPException(403)
 target=d.get('new_owner_id')
 if not target or role(c,w['id'],int(target))!='admin': c.close(); raise HTTPException(400)
 c.execute('UPDATE members SET role=\'admin\' WHERE workspace_id=? AND user_id=?',(w['id'],u['id']))
 c.execute('UPDATE members SET role=\'owner\' WHERE workspace_id=? AND user_id=?',(w['id'],int(target)))
 c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(int(target),w['id']))
 c.commit(); r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone(); c.close()
 return {'workspace':dict(r)}
@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def invite(slug,req:Request):
 u=auth(req);d=await req.json();c=db();w=getws(c,slug)
 if role(c,w['id'],u['id']) not in ('owner','admin'):raise HTTPException(403)
 code=secrets.token_urlsafe(12);exp=now()+d.get('expires_in_seconds',86400);c.execute('INSERT INTO invites VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],exp,d.get('max_uses',1),0,d.get('invited_user_id')));c.commit();c.close();return {'invitation':{'code':code,'workspace_id':w['id'],'expires_at':iso(exp),'max_uses':d.get('max_uses',1)}}
@app.post('/api/invitations/{code}/accept')
def accept(code,req:Request):
 u=auth(req);c=db();i=c.execute('SELECT * FROM invites WHERE code=?',(code,)).fetchone()
 if not i or i['revoked'] or i['uses']>=i['max_uses'] or i['expires']<now():raise HTTPException(404)
 if i['target_id'] and i['target_id']!=u['id']:raise HTTPException(403)
 c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(i['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(code,));c.commit();c.close();return {'workspace_id':i['workspace_id'],'accepted':True}
@app.get('/api/workspaces/{slug}/invitations')
def listinv(slug,req:Request):
 u=auth(req);c=db();w=getws(c,slug)
 if role(c,w['id'],u['id']) not in ('owner','admin'):raise HTTPException(403)
 rs=c.execute('SELECT * FROM invites WHERE workspace_id=? AND revoked=0',(w['id'],)).fetchall();c.close();return {'invitations':[dict(x) for x in rs]}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def delinv(slug,code,req:Request):
 u=auth(req);c=db();w=getws(c,slug)
 if role(c,w['id'],u['id']) not in ('owner','admin'):raise HTTPException(403)
 c.execute('UPDATE invites SET revoked=1 WHERE code=?',(code,));c.commit();c.close();return {'deleted':True}
@app.websocket('/api/ws')
async def ws(sock:WebSocket):
 token=sock.query_params.get('token','');c=db();r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
 if not r: await sock.close(code=1008);return
 await sock.accept(); subs={}
 try:
  while True:
   try: msg=await asyncio.wait_for(sock.receive_json(),.15)
   except asyncio.TimeoutError: msg=None
   c=db()
   if msg:
    cid=int(msg.get('channel_id')); typ=msg.get('type')
    if typ=='subscribe':
     head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];subs[cid]=head;await sock.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif typ=='resume':
     since=int(msg.get('since_event_id',0));subs[cid]=since
     rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
     for e in rows:
      p=json.loads(e['payload']);p.update({'type':e['type'],'event_id':e['event_id'],'channel_id':cid});await sock.send_json(p);subs[cid]=e['event_id']
     await sock.send_json({'type':'resumed','channel_id':cid,'head_event_id':subs[cid]})
   for cid,last in list(subs.items()):
    rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
    for e in rows:
     p=json.loads(e['payload']);p.update({'type':e['type'],'event_id':e['event_id'],'channel_id':cid});await sock.send_json(p);subs[cid]=e['event_id']
   c.close()
 except (WebSocketDisconnect,Exception): pass
