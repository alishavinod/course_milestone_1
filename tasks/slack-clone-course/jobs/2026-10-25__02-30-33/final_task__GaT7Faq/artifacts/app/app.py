import os, re, json, uuid, hashlib, sqlite3, asyncio, threading
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.websockets import WebSocketState

DB='/app/data/app.db'; os.makedirs('/app/data',exist_ok=True)
NODE=int(os.getenv('NODE_ID','0')); lock=threading.RLock()
app=FastAPI()
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 with lock:
  c=conn(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,event_seq INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER,PRIMARY KEY(user_id,channel_id));
'''); c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 c=conn(); r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); c.close(); return out
def run(sql,args=()):
 with lock:
  c=conn(); r=c.execute(sql,args); c.commit(); x=r.lastrowid; c.close(); return x
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('authorization',''); t=h[7:] if h.lower().startswith('bearer ') else ''
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,),True)
 return r
def err(code,msg='error'): return JSONResponse({'error':msg},status_code=code)
async def body(req):
 try: return await req.json()
 except: return {}
def member(uid,wid): return q('SELECT role FROM members WHERE user_id=? AND workspace_id=?',(uid,wid),True)
def chmem(uid,cid): return q('SELECT 1 FROM channel_members WHERE user_id=? AND channel_id=?',(uid,cid),True)
def channel(cid): return q('SELECT * FROM channels WHERE id=?',(cid,),True)
def wspace(slug): return q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
def resource_channel(r):
 d=dict(r)
 for k in ('is_private','is_dm','is_archived'):
  d[k]=bool(d.get(k))
 return d
def emit(cid,kind,payload):
 with lock:
  c=conn(); c.execute('BEGIN IMMEDIATE'); r=c.execute('SELECT event_seq FROM channels WHERE id=?',(cid,)).fetchone(); n=(r[0] if r else 0)+1
  c.execute('UPDATE channels SET event_seq=? WHERE id=?',(n,cid)); c.execute('INSERT INTO events(channel_id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload))); c.commit(); c.close(); return n
def msgobj(r):
 if not r:return None
 d=dict(r); au=q('SELECT * FROM users WHERE id=?',(d['author_id'],),True); d['author']=userobj(au); d['reply_count']=q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],),True)['n']; d['files']=[]; d['reactions']=[]
 for x in q('SELECT emoji,user_id FROM reactions WHERE message_id=?',(d['id'],)):
  z=next((a for a in d['reactions'] if a['emoji']==x['emoji']),None)
  if not z:d['reactions'].append({'emoji':x['emoji'],'count':1,'user_ids':[x['user_id']]})
  else:z['count']+=1;z['user_ids'].append(x['user_id'])
 d['mentions']=json.loads(d.get('mentions','[]')) if 'mentions' in d else []
 return d
def page(rows,limit,cursor, key='id'):
 if limit is None:limit=50
 if limit<1 or limit>200: raise ValueError()
 start=0
 if cursor:
  try:start=int(cursor)
  except:raise ValueError()
 out=rows[start:start+limit]; nxt=str(start+limit) if start+limit<len(rows) else None
 return out,nxt
@app.get('/api/health')
async def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register')
async def register(req:Request):
 b=await body(req); u=b.get('username',''); p=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8:return err(400)
 if q('SELECT id FROM users WHERE username=?',(u,),True):return err(409)
 uid=run('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,p,b.get('display_name') or u)); tok=uuid.uuid4().hex+uuid.uuid4().hex;run('INSERT INTO tokens VALUES(?,?)',(tok,uid))
 return JSONResponse({'user':userobj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':tok},201)
@app.post('/api/auth/login')
async def login(req:Request):
 b=await body(req); r=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password')),True)
 if not r:return err(401)
 t=uuid.uuid4().hex+uuid.uuid4().hex;run('INSERT INTO tokens VALUES(?,?)',(t,r['id']));return {'user':userobj(r),'token':t}
@app.get('/api/auth/me')
async def me(req:Request):
 r=auth(req);return {'user':userobj(r)} if r else err(401)
@app.get('/api/users/{uid}')
async def getuser(uid:int):
 r=q('SELECT * FROM users WHERE id=?',(uid,),True);return {'user':userobj(r)} if r else err(404)
@app.patch('/api/users/me')
async def patchme(req:Request):
 r=auth(req)
 if not r:return err(401)
 b=await body(req); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:v for k,v in b.items() if k in allowed}
 if 'timezone' in vals and not isinstance(vals['timezone'],str):return err(400)
 if any(len(str(v))>500 for v in vals.values()):return err(400)
 if vals:run('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(r['id'],))
 return {'user':userobj(q('SELECT * FROM users WHERE id=?',(r['id'],),True))}
@app.post('/api/workspaces')
async def createws(req:Request):
 r=auth(req)
 if not r:return err(401)
 b=await body(req); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return err(400)
 if wspace(slug):return err(409)
 wid=run('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],r['id']));run('INSERT INTO members VALUES(?,?,?)',(wid,r['id'],'owner'));cid=run('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));run('INSERT INTO channel_members VALUES(?,?)',(cid,r['id']))
 return JSONResponse({'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(wid,),True)),'general_channel':resource_channel(channel(cid))},201)
@app.get('/api/workspaces')
async def listws(req:Request):
 r=auth(req)
 if not r:return err(401)
 return {'workspaces':[dict(x) for x in q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(r['id'],))]}
@app.get('/api/workspaces/{slug}')
async def detail(slug:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 if not w or not member(r['id'],w['id']):return err(404)
 include=req.query_params.get('include_archived') in ('1','true')
 cs=q('SELECT * FROM channels WHERE workspace_id=?'+('' if include else ' AND is_archived=0'),(w['id'],))
 cs=[resource_channel(x) for x in cs if not x['is_private'] or chmem(r['id'],x['id']) or member(r['id'],w['id'])['role'] in ('owner','admin')]
 return {'workspace':dict(w),'channels':cs,'read_state':[readstate(r['id'],x['id']) for x in cs]}
@app.get('/api/workspaces/{slug}/members')
async def members(slug:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 if not w or not member(r['id'],w['id']):return err(403)
 return {'members':[dict(x) for x in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]}
@app.patch('/api/workspaces/{slug}')
async def patchws(slug:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 m=member(r['id'],w['id'])
 if not w:return err(404)
 if not m or m['role'] not in ('owner','admin'):return err(403)
 b=await body(req)
 if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return err(400)
 vals={k:b[k] for k in ('name','join_mode') if k in b}
 if vals:run('UPDATE workspaces SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(w['id'],))
 return {'workspace':dict(wspace(slug))}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def role(slug:str,uid:int,req:Request):
 r=auth(req);w=wspace(slug); b=await body(req)
 if not r:return err(401)
 me=member(r['id'],w['id']) if w else None; target=member(uid,w['id']) if w else None
 if not me or me['role'] not in ('owner','admin'):return err(403)
 if b.get('role') not in ('owner','admin','member','guest'):return err(400)
 if b['role']=='owner':return err(400)
 if not target or target['role']=='owner' or (me['role']=='admin' and target['role']=='admin'):return err(403)
 run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(b['role'],w['id'],uid));return {'member':{'user_id':uid,'role':b['role']}}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,req:Request):
 r=auth(req);w=wspace(slug);b=await body(req)
 if not r:return err(401)
 if not w or not member(r['id'],w['id']) or member(r['id'],w['id'])['role']!='owner':return err(403)
 t=member(b.get('new_owner_id'),w['id'])
 if not t or t['role']!='admin':return err(400)
 run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],r['id']));run('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']));run('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id']))
 return {'workspace':dict(wspace(slug))}
@app.post('/api/workspaces/{slug}/channels')
async def createch(slug:str,req:Request):
 r=auth(req);w=wspace(slug);b=await body(req)
 if not r:return err(401)
 m=member(r['id'],w['id']) if w else None
 if not w or not m:return err(403)
 if m['role']=='guest' or (b.get('is_private') and m['role'] not in ('owner','admin')):return err(403)
 name=b.get('name','')
 if 'topic' in b and (not isinstance(b.get('topic'),str) or len(b.get('topic',''))>250):return err(400)
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return err(400)
 if q('SELECT id FROM channels WHERE workspace_id=? AND name=?',(w['id'],name),True):return err(409)
 cid=run('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')))
 run('INSERT INTO channel_members VALUES(?,?)',(cid,r['id']));return JSONResponse({'channel':resource_channel(channel(cid))},201)
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True);m=member(r['id'],w['id'])
 if c['is_private'] and not chmem(r['id'],cid):return err(403)
 if m and m['role']=='guest':return err(403)
 if not m:
  if w['join_mode']!='open':return err(403)
  run('INSERT INTO members VALUES(?,?,?)',(w['id'],r['id'],'member'))
 if not chmem(r['id'],cid):run('INSERT INTO channel_members VALUES(?,?)',(cid,r['id']));emit(cid,'member.joined',{'user_id':r['id']})
 return {'channel':resource_channel(channel(cid))}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 run('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,r['id']));emit(cid,'member.left',{'user_id':r['id']});return {'channel':resource_channel(channel(cid))}
@app.get('/api/channels/{cid}/members')
async def chmembers(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 if not chmem(r['id'],cid):return err(403)
 return {'members':[userobj(x) for x in q('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True);m=member(r['id'],w['id'])
 if not chmem(r['id'],cid) and (not m or m['role']!='owner'):return err(403)
 b=await body(req)
 if 'topic' in b and len(str(b['topic']))>250:return err(400)
 run('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',c['topic']),cid));emit(cid,'channel.updated',{'channel':resource_channel(channel(cid))});return {'channel':resource_channel(channel(cid))}
@app.post('/api/channels/{cid}/archive')
async def archive(cid:int,req:Request):
 return await arch(cid,req,1)
@app.post('/api/channels/{cid}/unarchive')
async def unarchive(cid:int,req:Request):
 return await arch(cid,req,0)
async def arch(cid,req,val):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
 if r['id']!=w['owner_id']:return err(403)
 run('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));emit(cid,'channel.archived' if val else 'channel.unarchived',{'channel':resource_channel(channel(cid))});return {'channel':resource_channel(channel(cid))}
@app.post('/api/workspaces/{slug}/invitations')
async def invite(slug:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 if not w:return err(404)
 m=member(r['id'],w['id'])
 if not m or m['role'] not in ('owner','admin'):return err(403)
 b=await body(req); target=b.get('invited_user_id')
 if b.get('invited_username'):
  u=q('SELECT id FROM users WHERE username=?',(b['invited_username'],),True)
  target=u['id'] if u else None
 if b.get('invited_username') and target is None:return err(400)
 code=uuid.uuid4().hex; exp=(datetime.now(timezone.utc)+timedelta(seconds=int(b.get('expires_in_seconds',604800)))).isoformat()
 run('INSERT INTO invitations(code,workspace_id,invited_user_id,expires_at,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],target,exp,int(b.get('max_uses',1))))
 return JSONResponse({'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':int(b.get('max_uses',1))}},201)
@app.get('/api/workspaces/{slug}/invitations')
async def listinv(slug:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 if not w:return err(404)
 m=member(r['id'],w['id'])
 if not m or m['role'] not in ('owner','admin'):return err(403)
 return {'invitations':[dict(x) for x in q('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],))]}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
async def delinv(slug:str,code:str,req:Request):
 r=auth(req);w=wspace(slug)
 if not r:return err(401)
 m=member(r['id'],w['id']) if w else None
 if not m or m['role'] not in ('owner','admin'):return err(403)
 run('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(code,w['id']));return {'deleted':True}
@app.post('/api/invitations/{code}/accept')
async def accept(code:str,req:Request):
 r=auth(req);i=q('SELECT * FROM invitations WHERE code=?',(code,),True)
 if not r:return err(401)
 if not i or i['revoked'] or i['uses']>=i['max_uses'] or datetime.fromisoformat(i['expires_at'].replace('Z','+00:00'))<datetime.now(timezone.utc):return err(404)
 if i['invited_user_id'] and i['invited_user_id']!=r['id']:return err(403)
 if not member(r['id'],i['workspace_id']):run('INSERT INTO members VALUES(?,?,?)',(i['workspace_id'],r['id'],'member'))
 run('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,));return {'accepted':True}
def access(uid,cid):
 c=channel(cid)
 if not c:return None,None
 w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
 m=member(uid,w['id'])
 if not m:return c,False
 if c['is_private'] and not chmem(uid,cid) and m['role'] not in ('owner','admin'):return c,False
 return c, bool(chmem(uid,cid) or m['role'] in ('owner','admin'))
def mentions(body,uid,cid):
 c=channel(cid); w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
 ids=[]; names=re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',body)
 for name in names:
  if name=='channel':
   ids += [x['user_id'] for x in q('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(cid,uid))]
  else:
   u=q('SELECT id FROM users WHERE username=?',(name,),True)
   if u and u['id']!=uid and member(u['id'],w['id']) and chmem(u['id'],cid):ids.append(u['id'])
 return list(dict.fromkeys(ids))
@app.post('/api/channels/{cid}/messages')
async def postmsg(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 _,ok=access(r['id'],cid)
 if not ok:return err(403)
 if c['is_archived']:return err(423)
 b=await body(req); bodytxt=b.get('body')
 if not isinstance(bodytxt,str) or not bodytxt.strip():return err(400)
 parent=b.get('parent_id')
 if parent and not q('SELECT id FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,cid),True):return err(404)
 created=now(); mid=run('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,0)',(cid,r['id'],bodytxt,parent,created))
 ids=mentions(bodytxt,r['id'],cid); run('UPDATE messages SET event_id=0 WHERE id=?',(mid,))
 ev=emit(cid,'message.reply' if parent else 'message.created',{'message_id':mid})
 run('UPDATE messages SET event_id=? WHERE id=?',(ev,mid))
 mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True)); mo['mentions']=ids
 payload={'message':mo}; run('UPDATE messages SET body=? WHERE id=?',(bodytxt,mid))
 return JSONResponse({'message':mo},201)
def paged_messages(cid,limit,cursor,parent=None):
 sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND '+('parent_id=?' if parent else 'parent_id IS NULL')
 args=[cid]+([parent] if parent else [])
 if cursor:
  try:
   boundary=int(cursor.split(':')[-1])
  except: raise ValueError()
  sql += ' AND id '+('> ?' if parent else '< ?'); args.append(boundary)
 sql += ' ORDER BY id '+('ASC' if parent else 'DESC')
 rows=[msgobj(x) for x in q(sql,tuple(args))]
 if limit is None: limit=50
 if limit<1 or limit>200: raise ValueError()
 out=rows[:limit]
 nxt=(('r:' if parent else 'm:')+str(out[-1]['id'])) if len(rows)>limit else None
 return out,nxt
@app.get('/api/channels/{cid}/messages')
async def messages(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 _,ok=access(r['id'],cid)
 if not ok:return err(403)
 try: rows,n=paged_messages(cid,int(req.query_params.get('limit','50')),req.query_params.get('cursor'))
 except: return err(400)
 return {'messages':rows,'next_cursor':n}
@app.patch('/api/messages/{mid}')
async def editmsg(mid:int,req:Request):
 r=auth(req);m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not r:return err(401)
 if not m:return err(404)
 if m['author_id']!=r['id']:return err(403)
 if m['deleted']:return err(409)
 b=await body(req)
 if not isinstance(b.get('body'),str) or not b['body'].strip():return err(400)
 run('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid)); ev=emit(m['channel_id'],'message.updated',{'message_id':mid});return {'message':msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))}
@app.delete('/api/messages/{mid}')
async def delmsg(mid:int,req:Request):
 r=auth(req);m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not r:return err(401)
 if not m:return err(404)
 c=channel(m['channel_id']);w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
 if m['author_id']!=r['id'] and w['owner_id']!=r['id']:return err(403)
 if not m['deleted']:run('UPDATE messages SET deleted=1 WHERE id=?',(mid,));emit(m['channel_id'],'message.deleted',{'message_id':mid})
 return {'deleted':True}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
 r=auth(req);m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not r:return err(401)
 if not m:return err(404)
 _,ok=access(r['id'],m['channel_id'])
 if not ok:return err(403)
 try: rows,n=paged_messages(m['channel_id'],int(req.query_params.get('limit','50')),req.query_params.get('cursor'),mid)
 except:return err(400)
 return {'replies':rows,'next_cursor':n}
@app.get('/api/search')
async def search(req:Request):
 r=auth(req)
 if not r:return err(401)
 term=req.query_params.get('q',''); ws=req.query_params.get('workspace_id')
 rows=[]
 for m in q('SELECT * FROM messages WHERE deleted=0 AND body LIKE ? ORDER BY id DESC',('%'+term+'%',)):
  c,ok=access(r['id'],m['channel_id'])
  if ok and (not ws or str(c['workspace_id'])==ws):rows.append(msgobj(m))
 try: rows,n=page(rows,int(req.query_params.get('limit','50')),req.query_params.get('cursor'))
 except:return err(400)
 return {'results':rows,'next_cursor':n}
@app.post('/api/messages/{mid}/pin')
async def pin(mid:int,req:Request):
 r=auth(req);m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not r:return err(401)
 if not m:return err(404)
 _,ok=access(r['id'],m['channel_id'])
 if not ok:return err(403)
 p=q('SELECT * FROM pins WHERE message_id=?',(mid,),True)
 if not p:run('INSERT INTO pins VALUES(?,?,?)',(mid,r['id'],now()));p=q('SELECT * FROM pins WHERE message_id=?',(mid,),True)
 return {'pin':dict(p)}
@app.delete('/api/messages/{mid}/pin')
async def unpin(mid:int,req:Request):
 r=auth(req);m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not r:return err(401)
 if not m:return err(404)
 run('DELETE FROM pins WHERE message_id=?',(mid,));return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
async def pins(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 _,ok=access(r['id'],cid)
 if not ok:return err(403)
 vals=[]
 for p in q('SELECT * FROM pins WHERE message_id IN (SELECT id FROM messages WHERE channel_id=?) ORDER BY pinned_at DESC',(cid,)):
  vals.append({'message':msgobj(q('SELECT * FROM messages WHERE id=?',(p['message_id'],),True)),'pinned_by':p['pinned_by'],'pinned_at':p['pinned_at']})
 try: vals,n=page(vals,int(req.query_params.get('limit','50')),req.query_params.get('cursor'))
 except:return err(400)
 return {'pins':vals,'next_cursor':n}
def readstate(uid,cid):
 r=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(uid,cid),True)
 last=r['last_read_event_id'] if r else 0
 offset=q('''SELECT count(*) AS n FROM events
  WHERE channel_id=? AND event_id < COALESCE(
    (SELECT min(event_id) FROM messages WHERE channel_id=? AND parent_id IS NULL), 0)
  AND kind!='message.created' ''',(cid,cid),True)['n']
 rows=q('''SELECT m.* FROM messages m JOIN events e
  ON e.channel_id=m.channel_id AND e.event_id=m.event_id
  WHERE m.channel_id=? AND m.parent_id IS NULL AND m.deleted=0
  AND m.author_id!=? AND m.event_id>? AND e.kind='message.created' ''',(cid,uid,last+offset))
 ment=0
 for x in rows:
  if uid in mentions(x['body'],x['author_id'],cid): ment+=1
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':len(rows),'mention_count':ment}
@app.get('/api/channels/{cid}/read')
async def getread(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 return {'read_state':readstate(r['id'],cid)}
@app.post('/api/channels/{cid}/read')
async def postread(cid:int,req:Request):
 r=auth(req);c=channel(cid)
 if not r:return err(401)
 if not c:return err(404)
 b=await body(req); old=readstate(r['id'],cid)['last_read_event_id']; n=max(old,int(b.get('last_read_event_id',0)))
 run('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(r['id'],cid,n))
 return {'read_state':readstate(r['id'],cid)}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 tok=ws.query_params.get('token',''); u=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,),True)
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs={}; last={}
 try:
  while True:
   try:
    data=await asyncio.wait_for(ws.receive_json(),timeout=.5)
    cid=int(data.get('channel_id'))
    if data.get('type')=='subscribe':
     subs[cid]=True; h=q('SELECT event_seq FROM channels WHERE id=?',(cid,),True); await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h[0] if h else 0})
    elif data.get('type')=='resume':
     since=int(data.get('since_event_id',0))
     for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
      p=json.loads(e['payload']); await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**p})
     h=q('SELECT event_seq FROM channels WHERE id=?',(cid,),True);await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h[0] if h else 0});last[cid]=h[0] if h else 0
   except asyncio.TimeoutError: pass
   for cid in list(subs):
    n=last.get(cid,0)
    for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)):
     p=json.loads(e['payload']); await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**p});last[cid]=e['event_id']
 except (WebSocketDisconnect,Exception): pass
