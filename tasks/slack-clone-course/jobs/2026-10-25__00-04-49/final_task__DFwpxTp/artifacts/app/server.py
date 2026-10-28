import os, re, json, time, uuid, hashlib, sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def db():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    try: c.execute('PRAGMA journal_mode=WAL')
    except sqlite3.OperationalError: pass
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
    '''); c.commit(); c.close()
init()
def userobj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':None,'status_text':'','status_emoji':''}
def workspace(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def channel(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':False,'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def js(h,status,obj):
    b=json.dumps(obj).encode(); h.send_response(status); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def read(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        try:return json.loads(self.rfile.read(n) or b'{}')
        except:return {}
    def auth(self):
        x=self.headers.get('Authorization','')
        if not x.startswith('Bearer '): return None
        c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close(); return r
    def need(self):
        u=self.auth()
        if not u: js(self,401,{'error':'unauthorized'})
        return u
    def route(self):
        p=urlparse(self.path).path
        if p=='/api/health':
            try:n=int(self.server.server_port)-8000
            except:n=0
            return js(self,200,{'status':'ok','node_id':n})
        if p=='/api/ws':
            self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
        if self.command=='POST' and p=='/api/auth/register': return self.register()
        if self.command=='POST' and p=='/api/auth/login': return self.login()
        if p=='/api/auth/me' and self.command=='GET':
            u=self.need()
            if u: return js(self,200,{'user':userobj(u)})
            return
        u=self.need()
        if not u:return
        parts=[unquote(x) for x in p.split('/') if x]
        if parts==['api','workspaces'] and self.command=='GET': return self.wlist(u)
        if parts==['api','workspaces'] and self.command=='POST': return self.wcreate(u)
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='members' and self.command=='GET': return self.members(u,parts[2])
        if len(parts)==3 and parts[:2]==['api','workspaces'] and self.command=='PATCH': return self.wpatch(u,parts[2])
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='channels' and self.command=='POST': return self.chcreate(u,parts[2])
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='invitations':
            return self.invite(u,parts[2]) if self.command=='POST' else self.ilist(u,parts[2])
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='members' and self.command=='PATCH': return self.role(u,parts[2],parts[4])
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='transfer_ownership' and self.command=='POST': return self.transfer(u,parts[2])
        if len(parts)==4 and parts[:2]==['api','invitations'] and parts[3]=='accept' and self.command=='POST': return self.accept(u,parts[2])
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='join' and self.command=='POST': return self.join(u,parts[2])
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='invitations' and self.command=='DELETE': return self.irevoke(u,parts[2],parts[4])
        js(self,404,{'error':'not found'})
    def register(self):
        d=self.read(); un=d.get('username',''); pw=d.get('password','')
        if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return js(self,400,{'error':'invalid input'})
        c=db()
        try:
            cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un)); uid=cur.lastrowid
            tok=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit()
            r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();js(self,201,{'user':userobj(r),'token':tok})
        except sqlite3.IntegrityError: js(self,409,{'error':'duplicate'})
        finally:c.close()
    def login(self):
        d=self.read();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username',''),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
        if not r:c.close();return js(self,401,{'error':'invalid credentials'})
        tok=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id']));c.commit();c.close();js(self,200,{'user':userobj(r),'token':tok})
    def wr(self,u,slug):
        c=db();r=c.execute('SELECT w.*,m.role FROM workspaces w JOIN members m ON m.workspace_id=w.id AND m.user_id=? WHERE w.slug=?',(u['id'],slug)).fetchone();c.close();return r
    def wcreate(self,u):
        d=self.read(); slug=d.get('slug','')
        if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return js(self,400,{'error':'bad slug'})
        c=db()
        try:
            cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id']));wid=cur.lastrowid
            c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cc=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cc.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();js(self,201,{'workspace':workspace(w),'general_channel':channel(ch)})
        except sqlite3.IntegrityError:js(self,409,{'error':'duplicate'})
        finally:c.close()
    def wlist(self,u):
        c=db();rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();js(self,200,{'workspaces':[workspace(x) for x in rs]})
    def members(self,u,slug):
        w=self.wr(u,slug)
        if not w:return js(self,403,{'error':'forbidden'})
        c=db();rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();js(self,200,{'members':[dict(x) for x in rs]})
    def wpatch(self,u,slug):
        w=self.wr(u,slug);d=self.read()
        if not w:return js(self,403,{'error':'forbidden'})
        if w['role'] not in ('owner','admin'):return js(self,403,{'error':'forbidden'})
        if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return js(self,400,{'error':'bad join mode'})
        c=db();c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit();r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();js(self,200,{'workspace':workspace(r)})
    def chcreate(self,u,slug):
        w=self.wr(u,slug);d=self.read()
        if not w:return js(self,403,{'error':'forbidden'})
        if w['role']=='guest' or (d.get('is_private',False) and w['role'] not in ('owner','admin')):return js(self,403,{'error':'forbidden'})
        name=d.get('name','')
        if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return js(self,400,{'error':'bad name'})
        c=db()
        if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():c.close();return js(self,409,{'error':'duplicate'})
        try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();js(self,201,{'channel':channel(r)})
        finally:c.close()
    def join(self,u,cid):
        c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
        if not ch:c.close();return js(self,404,{'error':'not found'})
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
        if ch['is_private'] or (m and m['role']=='guest') or (not m and w['join_mode']=='invite_only'):c.close();return js(self,403,{'error':'forbidden'})
        if not m:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();c.close();js(self,200,{'channel':channel(ch)})
    def role(self,u,slug,uid):
        w=self.wr(u,slug);d=self.read();tryid=int(uid) if str(uid).isdigit() else -1; new=d.get('role')
        if not w:return js(self,403,{'error':'forbidden'})
        if new=='owner' or new not in ('admin','member','guest'):return js(self,400,{'error':'bad role'})
        c=db();target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],tryid)).fetchone()
        if not target:c.close();return js(self,404,{'error':'not found'})
        if w['role'] not in ('owner','admin') or target['role']=='owner' or (w['role']=='admin' and target['role']=='admin'):c.close();return js(self,403,{'error':'forbidden'})
        c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],tryid));c.commit();c.close();js(self,200,{'member':{'user_id':tryid,'role':new}})
    def invite(self,u,slug):
        w=self.wr(u,slug)
        if not w or w['role'] not in ('owner','admin'):return js(self,403,{'error':'forbidden'})
        d=self.read();code=uuid.uuid4().hex;c=db();exp=time.time()+float(d.get('expires_in_seconds',31536000));maxu=int(d.get('max_uses',1));c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],exp,maxu,0,d.get('invited_user_id'),0));c.commit();c.close();js(self,201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maxu}})
    def accept(self,u,code):
        c=db();i=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
        if not i or i['revoked'] or i['uses']>=i['max_uses'] or i['expires_at']<time.time():c.close();return js(self,404,{'error':'not found'})
        if i['invited_user_id'] and i['invited_user_id']!=u['id']:c.close();return js(self,403,{'error':'forbidden'})
        c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(i['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,));c.commit();c.close();js(self,200,{'accepted':True})
    def ilist(self,u,slug):
        w=self.wr(u,slug)
        if not w or w['role'] not in ('owner','admin'):return js(self,403,{'error':'forbidden'})
        c=db();rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND expires_at>? AND uses<max_uses',(w['id'],time.time())).fetchall();c.close();js(self,200,{'invitations':[{'code':x['code'],'workspace_id':x['workspace_id'],'max_uses':x['max_uses']} for x in rs]})
    def irevoke(self,u,slug,code):
        w=self.wr(u,slug)
        if not w or w['role'] not in ('owner','admin'):return js(self,403,{'error':'forbidden'})
        c=db();c.execute('UPDATE invitations SET revoked=1 WHERE code=? AND workspace_id=?',(code,w['id']));c.commit();c.close();js(self,200,{'deleted':True})
    def transfer(self,u,slug):
        w=self.wr(u,slug);d=self.read();nid=d.get('new_owner_id')
        if not w:return js(self,403,{'error':'forbidden'})
        if w['role']!='owner':return js(self,403,{'error':'forbidden'})
        c=db();t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],nid)).fetchone()
        if not t or t['role']!='admin':c.close();return js(self,400,{'error':'target must be admin'})
        c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],nid));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(nid,w['id']));c.commit();c.close();js(self,200,{'transferred':True})
    def do_GET(self): self.route()
    def do_POST(self): self.route()
    def do_PATCH(self): self.route()
    def do_DELETE(self): self.route()
for port in (8000,8001,8002):
    pass
if __name__=='__main__':
    import sys
    port=int(sys.argv[1]);ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
