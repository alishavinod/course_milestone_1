import os, re, json, uuid, hashlib, sqlite3, asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

DB='/app/data.db'; os.makedirs('/app',exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mentions TEXT);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator INTEGER,users TEXT);
'''); c.commit(); c.close()
init()
# Migrate databases created by the initial schema: event ids are dense per channel.
try:
 c=db(); info=c.execute("PRAGMA table_info(events)").fetchall()
 if info and any(r['name']=='event_id' and r['pk']==1 for r in info):
  c.execute('ALTER TABLE events RENAME TO events_old')
  c.execute('CREATE TABLE events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id))')
  rows=c.execute('SELECT channel_id,event_id,kind,payload FROM events_old ORDER BY channel_id,event_id').fetchall()
  for r in rows: c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(r['channel_id'],r['event_id'],r['kind'],r['payload']))
  c.execute('DROP TABLE events_old'); c.commit()
 c.close()
except Exception:
 try: c.close()
 except: pass
def user_obj(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':None,'status_text':'','status_emoji':''}
def ch_obj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['archived'])}
def auth(req):
 h=req.headers.get('authorization','')
 if not h.lower().startswith('bearer '): raise HTTPException(401)
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def token_user(t):
 c=db(); r=c.execute('SELECT u.* FROM tokens x JOIN users u ON u.id=x.user_id WHERE x.token=?',(t,)).fetchone(); c.close(); return r
def err(code,msg='error'): raise HTTPException(code,msg)
def member(c,w,u):
 return c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
def accessible(c,ch,u):
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 m=member(c,w['id'],u)
 return bool(m) and (not ch['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u)).fetchone())
def add_event(c,ch,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(ch,n,kind,json.dumps(payload))); return n
def msg_obj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 reactions=[]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':reactions,'mentions':json.loads(r['mentions'] or '[]'),'event_id':r['event_id']}
def page_params(req):
 try: lim=int(req.query_params.get('limit','50'))
 except: err(400)
 if lim<1 or lim>200: err(400)
 cur=req.query_params.get('cursor')
 if cur is not None and (not cur.isdigit()): err(400)
 return lim, int(cur) if cur else 0
app=FastAPI()
@app.exception_handler(HTTPException)
async def he(req,e): return JSONResponse({'detail':e.detail},status_code=e.status_code)
@app.get('/api/health')
def health(request:Request): return {'status':'ok','node_id':int(os.getenv('NODE_ID','0'))}
@app.post('/api/auth/register',status_code=201)
async def register(request:Request):
 d=await request.json(); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: err(400)
 c=db()
 if c.execute('SELECT 1 FROM users WHERE username=?',(un,)).fetchone(): c.close(); err(409)
 cur=c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un,now())); uid=cur.lastrowid
 t=uuid.uuid4().hex+uuid.uuid4().hex; c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 return {'user':user_obj(r),'token':t}
@app.post('/api/auth/login')
async def login(request:Request):
 d=await request.json(); c=db(); r=c.execute('SELECT * FROM users WHERE username=?',(d.get('username'),)).fetchone()
 if not r or r['password']!=hashlib.sha256(d.get('password','').encode()).hexdigest(): c.close(); err(401)
 t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return {'user':user_obj(r),'token':t}
@app.get('/api/auth/me')
def me(request:Request): return {'user':user_obj(auth(request))}
@app.post('/api/workspaces',status_code=201)
async def create_ws(request:Request):
 u=auth(request); d=await request.json(); slug=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): err(400)
 c=db()
 if c.execute('SELECT 1 FROM workspaces WHERE slug=?',(slug,)).fetchone(): err(409)
 cur=c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(slug,d.get('name',''),u['id'],'open')); wid=cur.lastrowid
 c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); chid=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(chid,u['id']));c.commit()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone();c.close()
 return {'workspace':dict(w),'general_channel':ch_obj(ch)}
@app.get('/api/workspaces')
def list_ws(request:Request):
 u=auth(request);c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[dict(x) for x in rows]}
@app.get('/api/workspaces/{slug}')
def get_ws(slug:str,request:Request):
 u=auth(request); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w or not member(c,w['id'],u['id']): err(404)
 include=request.query_params.get('include_archived') in ('1','true')
 cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (archived=0 OR ?)',(w['id'],include)).fetchall()
 visible=[x for x in cs if accessible(c,x,u['id'])]
 rs=[]
 for x in visible:
  rr=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone()
  last=rr['last_read'] if rr else 0
  mm=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND event_id>? AND author_id!=?',(x['id'],last,u['id'])).fetchall()
  rs.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':len(mm),'mention_count':sum(u['id'] in json.loads(z['mentions'] or '[]') for z in mm)})
 out={'workspace':dict(w),'channels':[ch_obj(x) for x in visible],'read_state':rs}; c.close(); return out
@app.get('/api/workspaces/{slug}/members')
def members(slug,request:Request):
 u=auth(request);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w or not member(c,w['id'],u['id']):err(403)
 x=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return {'members':[dict(z) for z in x]}
@app.patch('/api/workspaces/{slug}')
async def patch_ws(slug,request:Request):
 u=auth(request);d=await request.json();c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=member(c,w['id'],u['id']) if w else None
 if not w:err(404)
 if not m or m['role'] not in ('owner','admin'):err(403)
 if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):err(400)
 sets=[];vals=[]
 for k in ('name','join_mode'):
  if k in d:sets.append(k+'=?');vals.append(d[k])
 if sets:c.execute('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',vals+[w['id']]);c.commit()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return {'workspace':dict(w)}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patch_member(slug,uid,request:Request):
 u=auth(request);d=await request.json();role=d.get('role');c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); me=member(c,w['id'],u['id']) if w else None; target=member(c,w['id'],int(uid)) if w else None
 if role not in ('owner','admin','member','guest'):err(400)
 if role=='owner':err(400)
 if not w or not me or not target:err(403)
 if int(uid)==w['owner_id'] or int(uid)==u['id']:err(403)
 if me['role'] not in ('owner','admin') or (me['role']=='admin' and target['role']=='admin'):err(403)
 c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],uid));c.commit();c.close();return {'member':{'user_id':int(uid),'role':role}}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug,request:Request):
 u=auth(request);d=await request.json();c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w or w['owner_id']!=u['id']:err(403)
 t=member(c,w['id'],d.get('new_owner_id'))
 if not t or t['role']!='admin':err(400)
 c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']));c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],d['new_owner_id']));c.commit();c.close();return {'ok':True}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug,request:Request):
 u=auth(request);d=await request.json();c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=member(c,w['id'],u['id']) if w else None
 if not w or not m:err(403)
 if m['role']=='guest' or (d.get('is_private',False) and m['role'] not in ('owner','admin')):err(403)
 name=d.get('name','')
 if len(d.get('topic',''))>250:err(400)
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name):err(400)
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():err(409)
 cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));x=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(x,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(x,)).fetchone();c.close();return {'channel':ch_obj(r)}
@app.post('/api/channels/{cid}/join')
def join(cid,request:Request):
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();m=member(c,w['id'],u['id'])
 if m and m['role']=='guest':err(403)
 if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():err(403)
 if not m and w['join_mode']!='open':err(403)
 if not m:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); add_event(c,cid,'member.joined',{'user_id':u['id']}); c.commit();c.close();return {'channel':ch_obj(ch)}
@app.post('/api/channels/{cid}/leave')
def leave(cid,request:Request):
 u=auth(request);c=db();had=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone();c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']));
 if had: add_event(c,cid,'member.left',{'user_id':u['id']})
 c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':ch_obj(r)}
@app.get('/api/channels/{cid}/members')
def chmembers(cid,request:Request):
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch or not accessible(c,ch,u['id']):err(403)
 rs=c.execute('SELECT u.* FROM cmembers m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return {'members':[user_obj(x) for x in rs]}
@app.patch('/api/channels/{cid}')
async def patch_ch(cid,request:Request):
 u=auth(request);d=await request.json();c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():err(403)
 if len(d.get('topic',''))>250:err(400)
 c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),cid)); add_event(c,cid,'channel.updated',{'channel_id':cid});c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':ch_obj(r)}
@app.post('/api/channels/{cid}/archive')
@app.post('/api/channels/{cid}/unarchive')
def archive(cid,request:Request,action=None):
 action='unarchive' if request.url.path.endswith('/unarchive') else 'archive'
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if w['owner_id']!=u['id']:err(403)
 val=1 if action=='archive' else 0;c.execute('UPDATE channels SET archived=? WHERE id=?',(val,cid));add_event(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel_id':cid});c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':ch_obj(r)}
@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def invite(slug,request:Request):
 u=auth(request);d=await request.json();c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=member(c,w['id'],u['id']) if w else None
 if not m or m['role'] not in ('owner','admin'):err(403)
 target=d.get('invited_user_id'); code=uuid.uuid4().hex
 if not target and d.get('invited_username'):
  z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone();target=z['id'] if z else None
 exp=(datetime.now(timezone.utc)+timedelta(seconds=int(d.get('expires_in_seconds',86400)))).isoformat()
 c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?)',(code,w['id'],target,exp,int(d.get('max_uses',1)),0,0));c.commit()
 r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close()
 return {'invitation':dict(r)}
@app.get('/api/workspaces/{slug}/invitations')
def list_inv(slug,request:Request):
 u=auth(request);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=member(c,w['id'],u['id']) if w else None
 if not m or m['role'] not in ('owner','admin'):err(403)
 r=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0',(w['id'],)).fetchall();c.close();return {'invitations':[dict(x) for x in r]}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def del_inv(slug,code,request:Request):
 u=auth(request);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=member(c,w['id'],u['id']) if w else None
 if not m or m['role'] not in ('owner','admin'):err(403)
 c.execute('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(code,w['id']));c.commit();c.close();return {'deleted':True}
@app.post('/api/invitations/{code}/accept')
def accept(code,request:Request):
 u=auth(request);c=db();x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
 if not x or x['revoked'] or x['uses']>=x['max_uses'] or datetime.fromisoformat(x['expires'])<datetime.now(timezone.utc):err(404)
 if x['invited_user_id'] and x['invited_user_id']!=u['id']:err(403)
 c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,));c.commit();c.close();return {'accepted':True}

@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid,request:Request):
 u=auth(request);d=await request.json();body=d.get('body')
 c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 if ch['archived']:err(423)
 if not accessible(c,ch,u['id']):err(403)
 if not isinstance(body,str) or not body.strip():err(400)
 parent=d.get('parent_id')
 if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,cid)).fetchone():err(404)
 names=re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',body); mids=[]
 ws=ch['workspace_id']
 for name in names:
  if name=='channel':
   mids += [x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=? AND user_id!=?',(cid,u['id']))]
  else:
   z=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
   if z and z['id']!=u['id'] and member(c,ws,z['id']) and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,z['id'])).fetchone(): mids.append(z['id'])
 mids=list(dict.fromkeys(mids)); typ='message.reply' if parent else 'message.created'
 c.execute('BEGIN IMMEDIATE')
 cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created,mentions) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,parent,now(),json.dumps(mids)))
 mid=cur.lastrowid; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msg_obj(c,r); eid=add_event(c,cid,typ,mo); mo['event_id']=eid; c.execute('UPDATE messages SET event_id=? WHERE id=?',(eid,mid)); c.execute('UPDATE events SET payload=? WHERE channel_id=? AND event_id=?',(json.dumps(mo),cid,eid)); c.commit(); c.close()
 return {'message':mo}

@app.get('/api/channels/{cid}/messages')
def get_messages(cid,request:Request):
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 if not accessible(c,ch,u['id']):err(403)
 lim,cur=page_params(request); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?', (cid,cur,cur,lim+1)).fetchall()
 nxt=str(rows[lim-1]['id']) if len(rows)>lim else None; rows=rows[:lim]; out=[msg_obj(c,x) for x in rows];c.close();return {'messages':out,'next_cursor':nxt}
@app.get('/api/messages/{mid}/replies')
def replies(mid,request:Request):
 u=auth(request);c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not p:err(404)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(p['channel_id'],)).fetchone()
 if not accessible(c,ch,u['id']):err(403)
 lim,cur=page_params(request); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id ASC LIMIT ?',(mid,cur,lim+1)).fetchall()
 nxt=str(rows[lim-1]['id']) if len(rows)>lim else None;c.close();return {'replies':[msg_obj(db(),x) for x in rows[:lim]],'next_cursor':nxt}
@app.patch('/api/messages/{mid}')
async def edit_message(mid,request:Request):
 u=auth(request);d=await request.json();c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:err(404)
 if r['author_id']!=u['id']:err(403)
 if not d.get('body','').strip():err(400)
 c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(d['body'],now(),mid));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); out=msg_obj(c,r); c.close();return {'message':out}
@app.delete('/api/messages/{mid}')
def delete_message(mid,request:Request):
 u=auth(request);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:return {'deleted':True}
 ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone();w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if r['author_id']!=u['id'] and w['owner_id']!=u['id']:err(403)
 c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));c.commit();c.close();return {'deleted':True}
@app.get('/api/search')
def search(request:Request):
 u=auth(request);q=request.query_params.get('q','');lim,cur=page_params(request);c=db(); rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND (?=0 OR m.id<?) AND m.body LIKE ? ORDER BY m.id DESC LIMIT ?', (cur,cur,'%'+q+'%',lim+1)).fetchall()
 out=[]
 for r in rows:
  ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
  if accessible(c,ch,u['id']) and (not request.query_params.get('workspace_id') or str(ch['workspace_id'])==request.query_params['workspace_id']):out.append({'message':msg_obj(c,r)})
 nxt=str(rows[lim-1]['id']) if len(rows)>lim else None;c.close();return {'results':out[:lim],'next_cursor':nxt}
@app.post('/api/messages/{mid}/pin')
def pin(mid,request:Request):
 u=auth(request);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r:err(404)
 c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));c.commit();p=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone();c.close();return {'pin':dict(p)}
@app.delete('/api/messages/{mid}/pin')
def unpin(mid,request:Request):
 auth(request);c=db();c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();c.close();return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
def pins(cid,request:Request):
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 if not accessible(c,ch,u['id']):err(403)
 lim,cur=page_params(request); rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND (?=0 OR p.message_id<?) ORDER BY p.message_id DESC LIMIT ?',(cid,cur,cur,lim+1)).fetchall()
 out=[{'message':msg_obj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows[:lim]];nxt=str(rows[lim-1]['message_id']) if len(rows)>lim else None;c.close();return {'pins':out,'next_cursor':nxt}
@app.get('/api/channels/{cid}/read')
def get_read(cid,request:Request):
 u=auth(request);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:err(404)
 if not accessible(c,ch,u['id']):err(403)
 r=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();last=r['last_read'] if r else 0
 rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND event_id>? AND author_id!=?',(cid,last,u['id'])).fetchall();mentions=sum(u['id'] in json.loads(x['mentions'] or '[]') for x in rows);c.close();return {'read_state':{'channel_id':int(cid),'last_read_event_id':last,'unread_count':len(rows),'mention_count':mentions}}
@app.post('/api/channels/{cid}/read')
async def mark_read(cid,request:Request):
 u=auth(request);d=await request.json();c=db();old=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();last=max(old['last_read'] if old else 0,int(d.get('last_read_event_id') or 0));c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],cid,last));c.commit();c.close();return get_read(cid,request)

@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 t=ws.query_params.get('token',''); u=token_user(t)
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs={}; last={}
 try:
  while True:
   try: data=await asyncio.wait_for(ws.receive_json(),.15)
   except asyncio.TimeoutError: data=None
   if data:
    cid=int(data.get('channel_id',0))
    c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) x FROM events WHERE channel_id=?',(cid,)).fetchone()['x']
    if data.get('type')=='subscribe': subs[cid]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif data.get('type')=='resume':
     n=int(data.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)).fetchall()
     for x in rows: await ws.send_json({'type':x['kind'],'event_id':x['event_id'],'channel_id':cid,**json.loads(x['payload'])})
     subs[cid]=head;await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    c.close()
   for cid,n in list(subs.items()):
    c=db();rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)).fetchall()
    for x in rows: await ws.send_json({'type':x['kind'],'event_id':x['event_id'],'channel_id':cid,**json.loads(x['payload'])});subs[cid]=x['event_id']
    c.close()
 except (WebSocketDisconnect,Exception): pass
