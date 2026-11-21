import os, secrets, re, json, datetime
import psycopg
from psycopg.rows import dict_row
from decimal import Decimal
from functools import wraps
from flask import Flask, g, request, session, redirect, url_for, render_template, abort, flash
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from dotenv import load_dotenv
load_dotenv()
app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('HTTPS')=='1', MAX_CONTENT_LENGTH=1000000)
DEMO=os.getenv('DEMO_MODE','0')=='1'
REGISTRATION_OPEN = os.getenv('REGISTRATION_OPEN', '0') == '1'
TASKS_OPEN = os.getenv('TASKS_OPEN', '0') == '1'
MANUAL_PAYMENT_ENABLED = os.getenv('MANUAL_PAYMENT_ENABLED', '0') == '1'
USDT_TRC20_ADDRESS = os.getenv('USDT_TRC20_ADDRESS', '')

class CursorAdapter:
 def __init__(self, cursor, lastrowid=None):
  self.cursor=cursor
  self.rowcount=cursor.rowcount
  self.lastrowid=lastrowid
 def fetchone(self): return self.cursor.fetchone()
 def fetchall(self): return self.cursor.fetchall()

class DBAdapter:
 def __init__(self, connection): self.connection=connection
 def execute(self, sql, params=()):
  query=sql.strip()
  if query.upper()=='BEGIN IMMEDIATE': query='BEGIN'
  inserted_user=query.upper().startswith('INSERT INTO USERS(') and 'ON CONFLICT' not in query.upper()
  if inserted_user: query+=' RETURNING id'
  with self.connection.cursor() as cur:
   cur.execute(query.replace('?', '%s'), params)
   new_id=cur.fetchone()['id'] if inserted_user else None
   # Keep query result cursor open after the context; psycopg fetchall requires
   # an open cursor, so return a materialized cursor-like result.
   if inserted_user: return CursorAdapterResult([],cur.rowcount,new_id)
   rows=cur.fetchall() if cur.description else []
   return CursorAdapterResult(rows,cur.rowcount)
 def commit(self): self.connection.commit()
 def rollback(self): self.connection.rollback()
 def close(self): self.connection.close()

class CursorAdapterResult:
 def __init__(self,rows,rowcount,lastrowid=None):
  self.rows=rows
  self.rowcount=rowcount
  self.lastrowid=lastrowid
 def fetchone(self): return self.rows.pop(0) if self.rows else None
 def fetchall(self):
  rows=self.rows
  self.rows=[]
  return rows

def db():
 if 'db' not in g:
  url=os.environ.get('DATABASE_URL')
  if not url: raise RuntimeError('DATABASE_URL is required. PostgreSQL connection is not configured.')
  g.db=DBAdapter(psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=10))
 return g.db

@app.teardown_appcontext
def close(e):
 if 'db' in g: g.db.close()

def init():
 d=db()
 # PostgreSQL schema: retain monetary balances as integer USDT cents.
 schema=[
  """CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0, balance BIGINT NOT NULL DEFAULT 0 CHECK (balance>=0), referral_code TEXT UNIQUE, referred_by BIGINT REFERENCES users(id))""",
  """CREATE TABLE IF NOT EXISTS surveys(id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT, reward BIGINT NOT NULL CHECK (reward>0), questions TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0, budget BIGINT NOT NULL DEFAULT 0 CHECK (budget>=0))""",
  """CREATE TABLE IF NOT EXISTS responses(id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), survey_id BIGINT REFERENCES surveys(id), answers TEXT NOT NULL, reward BIGINT NOT NULL, UNIQUE(user_id,survey_id))""",
  """CREATE TABLE IF NOT EXISTS manual_activations(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id), txid TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_at TIMESTAMPTZ, reviewed_by BIGINT REFERENCES users(id))""",
  """CREATE UNIQUE INDEX IF NOT EXISTS one_pending_manual_activation_per_user ON manual_activations(user_id) WHERE status='pending'""",
  """CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id BIGINT REFERENCES users(id), provider_id TEXT UNIQUE, address TEXT, amount TEXT, status TEXT DEFAULT 'waiting', created_at TIMESTAMPTZ DEFAULT NOW())""",
  """CREATE TABLE IF NOT EXISTS withdrawals(id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), amount BIGINT NOT NULL, address TEXT NOT NULL, status TEXT DEFAULT 'pending', tx TEXT UNIQUE)""",
  """CREATE TABLE IF NOT EXISTS referral_awards(id BIGSERIAL PRIMARY KEY, referrer_id BIGINT NOT NULL REFERENCES users(id), referred_id BIGINT NOT NULL UNIQUE REFERENCES users(id), amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
  """CREATE TABLE IF NOT EXISTS site_funds(id INTEGER PRIMARY KEY CHECK(id=1), referral_budget BIGINT NOT NULL DEFAULT 0 CHECK(referral_budget>=0))""",
  """INSERT INTO site_funds(id,referral_budget) VALUES(1,0) ON CONFLICT (id) DO NOTHING""",
 ]
 for statement in schema: d.execute(statement)
 email=os.getenv('ADMIN_EMAIL'); pw=os.getenv('ADMIN_PASSWORD')
 if email and pw and len(pw)>=12:
  d.execute('INSERT INTO users(name,email,password,admin,active,referral_code) VALUES(?,?,?,?,?,?) ON CONFLICT (email) DO NOTHING',('Administrator',email.lower(),generate_password_hash(pw),1,1,secrets.token_hex(12).upper()))

with app.app_context(): init()
@app.after_request
def private_indexing(response):
 response.headers['X-Robots-Tag']='noindex, nofollow, noarchive, nosnippet'
 return response
@app.get('/robots.txt')
def robots():
 return app.response_class('User-agent: *\nDisallow: /\n',mimetype='text/plain')
@app.before_request
def prepare():
 g.user=db().execute('SELECT * FROM users WHERE id=?',(session.get('uid'),)).fetchone(); session.setdefault('csrf',secrets.token_hex(24))
 if request.method=='POST' and request.endpoint!='webhook' and not secrets.compare_digest(request.form.get('csrf',''),session['csrf']): abort(400)
@app.context_processor
def context(): return dict(demo=DEMO,csrf=session.get('csrf'),money=lambda n:f'{n/100:.2f}',registration_open=REGISTRATION_OPEN,tasks_open=TASKS_OPEN)
def auth(admin=False,active=False):
 def deco(fn):
  @wraps(fn)
  def run(*a,**kw):
   if not g.user:return redirect(url_for('login'))
   if admin and not g.user['admin']:abort(403)
   if active and not g.user['active']:return redirect(url_for('membership'))
   return fn(*a,**kw)
  return run
 return deco
def cents(value):
 x=Decimal(value)*100
 if not x.is_finite() or x!=x.to_integral_value() or x<0 or x>100000000: raise ValueError('Invalid amount')
 return int(x)
@app.route('/')
def home():return render_template('page.html',page='home')
@app.route('/register',methods=['GET','POST'])
def register():
 if not REGISTRATION_OPEN:
  return render_template('page.html',page='registration_closed'), 403
 if request.method=='POST':
  name=request.form['name'].strip(); email=request.form['email'].strip().lower(); pw=request.form['password']
  if not name or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(pw)<12 or not request.form.get('agree'):flash('Enter valid details, a 12-character password and accept the terms.');return redirect(url_for('register'))
  try:
   refcode=request.form.get('referral','').strip().upper()
   referrer=db().execute('SELECT id FROM users WHERE referral_code=? AND admin=0',(refcode,)).fetchone() if refcode else None
   if refcode and not referrer:flash('Referral code was not recognized.');return redirect(url_for('register'))
   code=secrets.token_urlsafe(12).replace('-','').replace('_','').upper()
   cur=db().execute('INSERT INTO users(name,email,password,referral_code,referred_by) VALUES(?,?,?,?,?)',(name,email,generate_password_hash(pw),code,referrer['id'] if referrer else None));db().commit();session.clear();session['uid']=cur.lastrowid;return redirect(url_for('membership'))
  except psycopg.IntegrityError:flash('Account already exists.')
 return render_template('page.html',page='register')
@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  u=db().execute('SELECT * FROM users WHERE email=?',(request.form['email'].lower().strip(),)).fetchone()
  if u and check_password_hash(u['password'],request.form['password']):session.clear();session['uid']=u['id'];return redirect(url_for('admin' if u['admin'] else 'dashboard'))
  flash('Invalid credentials.')
 return render_template('page.html',page='login')
@app.post('/logout')
def logout():session.clear();return redirect('/')
def api(method,path,**kwargs):
 r=requests.request(method,'https://api.nowpayments.io/v1/'+path,headers={'x-api-key':os.environ['NOWPAYMENTS_API_KEY']},timeout=15,**kwargs);r.raise_for_status();return r.json()
@app.route('/membership', methods=['GET','POST'])
@auth()
def membership():
 if g.user['active']:return redirect(url_for('dashboard'))
 d=db()
 if request.method=='POST':
  if not MANUAL_PAYMENT_ENABLED:
   flash('Manual activation is not accepting payment submissions yet.');return redirect(url_for('membership'))
  if not re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}',USDT_TRC20_ADDRESS):
   flash('Payment address is not configured; do not send funds.');return redirect(url_for('membership'))
  txid=request.form.get('txid','').strip().lower()
  if not re.fullmatch(r'[a-f0-9]{64}',txid):
   flash('Enter a 64-character TRON transaction hash.');return redirect(url_for('membership'))
  try:
   d.execute('INSERT INTO manual_activations(user_id,txid) VALUES(?,?)',(g.user['id'],txid));d.commit()
   flash('Transaction submitted for review. Access is enabled only after verification.')
  except psycopg.IntegrityError:
   d.rollback();flash('Transaction already used or your payment is already awaiting review.')
  return redirect(url_for('membership'))
 pending=d.execute('SELECT * FROM manual_activations WHERE user_id=? ORDER BY id DESC LIMIT 1',(g.user['id'],)).fetchone()
 return render_template('page.html',page='membership_manual',manual_payment_enabled=MANUAL_PAYMENT_ENABLED,receiving_address=USDT_TRC20_ADDRESS if MANUAL_PAYMENT_ENABLED else '',manual_payment=pending)

@app.post('/admin/manual-activation/<int:id>/<action>')
@auth(admin=True)
def review_manual_activation(id,action):
 if action not in ('approve','reject'): abort(404)
 if not MANUAL_PAYMENT_ENABLED: abort(403)
 if action=='approve' and request.form.get('verified')!='yes':
  flash('Verify the on-chain payment and confirm before approval.');return redirect(url_for('admin'))
 d=db();d.execute('BEGIN')
 item=d.execute('SELECT * FROM manual_activations WHERE id=? FOR UPDATE',(id,)).fetchone()
 if not item or item['status']!='pending':
  d.rollback();flash('This request was already reviewed.');return redirect(url_for('admin'))
 if action=='approve':
  # This checkbox is only admin attestation. Verification must be completed on TRONSCAN first.
  d.execute("UPDATE manual_activations SET status='approved', reviewed_at=NOW(), reviewed_by=? WHERE id=?",(g.user['id'],id))
  d.execute('UPDATE users SET active=1 WHERE id=?',(item['user_id'],))
  flash('Activation approved after your on-chain verification.')
 else:
  d.execute("UPDATE manual_activations SET status='rejected', reviewed_at=NOW(), reviewed_by=? WHERE id=?",(g.user['id'],id))
  flash('Payment submission rejected.')
 d.commit();return redirect(url_for('admin'))

@app.get('/terms')
def terms():return render_template('page.html',page='terms')

@app.route('/dashboard')
@auth(active=True)
def dashboard():
 d=db(); surveys=d.execute('SELECT * FROM surveys WHERE published=1 AND budget>=reward AND id NOT IN (SELECT survey_id FROM responses WHERE user_id=?)',(g.user['id'],)).fetchall(); history=d.execute('SELECT s.title,r.reward FROM responses r JOIN surveys s ON s.id=r.survey_id WHERE r.user_id=?',(g.user['id'],)).fetchall(); withdrawals=d.execute('SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC',(g.user['id'],)).fetchall();return render_template('page.html',page='dashboard',surveys=surveys,history=history,withdrawals=withdrawals,referrals=d.execute('SELECT ra.*,u.name FROM referral_awards ra JOIN users u ON u.id=ra.referred_id WHERE ra.referrer_id=?',(g.user['id'],)).fetchall())
@app.route('/survey/<int:id>',methods=['GET','POST'])
@auth(active=True)
def survey(id):
 if not TASKS_OPEN: return render_template('page.html',page='tasks_closed'), 403
 d=db();s=d.execute('SELECT * FROM surveys WHERE id=? AND published=1',(id,)).fetchone()
 if not s:abort(404)
 questions=json.loads(s['questions'])
 if request.method=='POST':
  answers=[request.form.get('q'+str(i),'').strip() for i in range(len(questions))]
  if any(not a or len(a)>5000 for a in answers):abort(400)
  try:
   d.execute('BEGIN IMMEDIATE'); cur=d.execute('UPDATE surveys SET budget=budget-reward WHERE id=? AND published=1 AND budget>=reward AND reward=? AND questions=?',(id,s['reward'],s['questions']))
   if not cur.rowcount:raise ValueError()
   d.execute('INSERT INTO responses(user_id,survey_id,answers,reward) VALUES(?,?,?,?)',(g.user['id'],id,json.dumps(answers),s['reward']));d.execute('UPDATE users SET balance=balance+? WHERE id=?',(s['reward'],g.user['id']))
   # A referral is eligible only after the referred person completes a real funded task.
   # Credit only out of an independently allocated referral budget; never create unfunded balances.
   ref=d.execute('SELECT referred_by FROM users WHERE id=?',(g.user['id'],)).fetchone()
   if ref and ref['referred_by']:
    award=d.execute('SELECT id FROM referral_awards WHERE referred_id=?',(g.user['id'],)).fetchone()
    if not award:
     d.execute('INSERT INTO referral_awards(referrer_id,referred_id,amount,status) VALUES(?,?,1000,?)',(ref['referred_by'],g.user['id'],'pending'))
   d.commit();flash('Survey submitted.')
  except (psycopg.IntegrityError,ValueError):d.rollback();flash('Already completed or no funded slots remain.')
  return redirect(url_for('dashboard'))
 return render_template('page.html',page='survey',survey=s,questions=questions)
@app.post('/withdraw')
@auth(active=True)
def withdraw():
 try: amount=cents(request.form['amount']);address=request.form['address'].strip();assert amount>=5000 and re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}',address)
 except (ValueError,ArithmeticError,AssertionError):flash('Enter at least 50 USDT and a valid-looking TRON address.');return redirect(url_for('dashboard'))
 d=db();d.execute('BEGIN IMMEDIATE');cur=d.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?',(amount,g.user['id'],amount))
 if cur.rowcount:d.execute('INSERT INTO withdrawals(user_id,amount,address) VALUES(?,?,?)',(g.user['id'],amount,address));d.commit();flash('Withdrawal requested; administrator review required.')
 else:d.rollback();flash('Insufficient balance.')
 return redirect(url_for('dashboard'))
@app.route('/admin',methods=['GET','POST'])
@auth(admin=True)
def admin():
 d=db()
 if request.method=='POST':
  try:
   title=request.form['title'].strip(); qs=[x.strip() for x in request.form['questions'].splitlines() if x.strip()]; reward=cents(request.form['reward']);budget=cents(request.form['budget']);assert title and qs and reward>0 and budget>=reward
   d.execute('INSERT INTO surveys(title,description,reward,questions,published,budget) VALUES(?,?,?,?,?,?)',(title,request.form['description'],reward,json.dumps(qs),1,budget));d.commit();flash('Survey published.')
  except (ValueError,ArithmeticError,AssertionError):flash('Check survey details and funded reward budget.')
  return redirect(url_for('admin'))
 return render_template('page.html',page='admin',referral_budget=d.execute('SELECT referral_budget FROM site_funds WHERE id=1').fetchone()['referral_budget'],referral_awards=d.execute('SELECT ra.*,u.name AS new_member,rf.email AS referrer FROM referral_awards ra JOIN users u ON ra.referred_id=u.id JOIN users rf ON ra.referrer_id=rf.id ORDER BY ra.id DESC').fetchall(),surveys=d.execute('SELECT * FROM surveys').fetchall(),users=d.execute('SELECT id,name,email,active,balance FROM users WHERE admin=0').fetchall(),withdrawals=d.execute('SELECT w.*,u.email FROM withdrawals w JOIN users u ON u.id=w.user_id').fetchall(),responses=d.execute('SELECT r.*,s.title,u.email FROM responses r JOIN surveys s ON s.id=r.survey_id JOIN users u ON u.id=r.user_id').fetchall(), manual_activations=d.execute("SELECT m.*,u.email FROM manual_activations m JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT 100").fetchall(), receiving_address=USDT_TRC20_ADDRESS)
@app.post('/admin/referrals/fund')
@auth(admin=True)
def fund_referrals():
 try:
  amount=cents(request.form['amount']);assert amount>0
 except (ValueError,AssertionError,ArithmeticError):flash('Invalid referral funding amount.');return redirect(url_for('admin'))
 # Bookkeeping only. Do not fund this unless matching independent funds are actually reserved.
 db().execute('UPDATE site_funds SET referral_budget=referral_budget+? WHERE id=1',(amount,));db().commit();flash('Referral budget allocation recorded. Ensure reserved external funds match.');return redirect(url_for('admin'))
@app.post('/admin/referrals/<int:id>/approve')
@auth(admin=True)
def approve_referral(id):
 d=db();d.execute('BEGIN IMMEDIATE')
 award=d.execute("SELECT * FROM referral_awards WHERE id=? AND status='pending'",(id,)).fetchone()
 if not award:d.rollback();return redirect(url_for('admin'))
 cur=d.execute('UPDATE site_funds SET referral_budget=referral_budget-? WHERE id=1 AND referral_budget>=?',(award['amount'],award['amount']))
 if not cur.rowcount:d.rollback();flash('Insufficient allocated referral budget.');return redirect(url_for('admin'))
 d.execute("UPDATE referral_awards SET status='credited' WHERE id=?",(id,))
 d.execute('UPDATE users SET balance=balance+? WHERE id=?',(award['amount'],award['referrer_id']))
 d.commit();flash('Referral reward credited.');return redirect(url_for('admin'))
@app.route('/admin/survey/<int:id>',methods=['GET','POST'])
@auth(admin=True)
def edit(id):
 d=db();s=d.execute('SELECT * FROM surveys WHERE id=?',(id,)).fetchone()
 if not s:abort(404)
 if request.method=='POST':
  qs=[x.strip() for x in request.form['questions'].splitlines() if x.strip()];title=request.form['title'].strip()
  if not title or not qs:abort(400)
  # Preserve historical question wording once any answers exist.
  if d.execute('SELECT 1 FROM responses WHERE survey_id=?',(id,)).fetchone() and qs!=json.loads(s['questions']):flash('Create a new survey to change questions after responses exist.');return redirect(url_for('edit',id=id))
  d.execute('UPDATE surveys SET title=?,description=?,questions=?,published=? WHERE id=?',(title,request.form['description'],json.dumps(qs),int('published' in request.form),id));d.commit();return redirect(url_for('admin'))
 return render_template('page.html',page='edit',survey=s,questions=json.loads(s['questions']))
@app.post('/admin/withdraw/<int:id>')
@auth(admin=True)
def settle(id):
 d=db()
 if request.form.get('action')=='paid' and datetime.datetime.now(datetime.timezone.utc).day!=5:
  flash('Record payouts on the 5th of each month (UTC) only.');return redirect(url_for('admin'))
 d.execute('BEGIN IMMEDIATE');w=d.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",(id,)).fetchone()
 if w:
  if request.form['action']=='reject':d.execute("UPDATE withdrawals SET status='rejected' WHERE id=?",(id,));d.execute('UPDATE users SET balance=balance+? WHERE id=?',(w['amount'],w['user_id']))
  elif request.form['action']=='paid':
   tx=request.form.get('tx','').strip()
   if not re.fullmatch(r'[a-fA-F0-9]{64}',tx):d.rollback();flash('Enter the actual TRON transaction hash.');return redirect(url_for('admin'))
   try:d.execute("UPDATE withdrawals SET status='paid',tx=? WHERE id=?",(tx.lower(),id))
   except psycopg.IntegrityError:d.rollback();flash('Transaction already recorded.');return redirect(url_for('admin'))
  d.commit()
 return redirect(url_for('admin'))
if __name__=='__main__':app.run(host='127.0.0.1',port=8000)
