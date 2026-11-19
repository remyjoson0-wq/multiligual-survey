import os, sqlite3, secrets, re, json
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
DB=os.getenv('DATABASE','survey.db'); DEMO=os.getenv('DEMO_MODE','1')=='1'
def db():
 if 'db' not in g:
  g.db=sqlite3.connect(DB,timeout=15); g.db.row_factory=sqlite3.Row; g.db.execute('PRAGMA foreign_keys=ON')
 return g.db
@app.teardown_appcontext
def close(e):
 if 'db' in g: g.db.close()
def init():
 d=db(); d.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT,email TEXT UNIQUE,password TEXT,admin INTEGER DEFAULT 0,active INTEGER DEFAULT 0,balance INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS surveys(id INTEGER PRIMARY KEY,title TEXT,description TEXT,reward INTEGER,questions TEXT,published INTEGER DEFAULT 0,budget INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),survey_id INTEGER REFERENCES surveys(id),answers TEXT,reward INTEGER,UNIQUE(user_id,survey_id)); CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),provider_id TEXT UNIQUE,address TEXT,amount TEXT,status TEXT DEFAULT 'waiting'); CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),amount INTEGER,address TEXT,status TEXT DEFAULT 'pending',tx TEXT UNIQUE);'''); d.commit()
 email=os.getenv('ADMIN_EMAIL'); pw=os.getenv('ADMIN_PASSWORD')
 if email and pw and len(pw)>=12:
  d.execute('INSERT OR IGNORE INTO users(name,email,password,admin,active) VALUES(?,?,?,?,?)',('Administrator',email.lower(),generate_password_hash(pw),1,1)); d.commit()
with app.app_context(): init()
@app.before_request
def prepare():
 g.user=db().execute('SELECT * FROM users WHERE id=?',(session.get('uid'),)).fetchone(); session.setdefault('csrf',secrets.token_hex(24))
 if request.method=='POST' and request.endpoint!='webhook' and not secrets.compare_digest(request.form.get('csrf',''),session['csrf']): abort(400)
@app.context_processor
def context(): return dict(demo=DEMO,csrf=session.get('csrf'),money=lambda n:f'{n/100:.2f}')
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
 if request.method=='POST':
  name=request.form['name'].strip(); email=request.form['email'].strip().lower(); pw=request.form['password']
  if not name or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(pw)<12 or not request.form.get('agree'):flash('Enter valid details, a 12-character password and accept the terms.');return redirect(url_for('register'))
  try:
   cur=db().execute('INSERT INTO users(name,email,password) VALUES(?,?,?)',(name,email,generate_password_hash(pw)));db().commit();session.clear();session['uid']=cur.lastrowid;return redirect(url_for('membership'))
  except sqlite3.IntegrityError:flash('Account already exists.')
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
@app.route('/membership',methods=['GET','POST'])
@auth()
def membership():
 if g.user['active']:return redirect(url_for('dashboard'))
 d=db(); p=d.execute('SELECT * FROM payments WHERE user_id=? ORDER BY rowid DESC LIMIT 1',(g.user['id'],)).fetchone()
 if request.method=='POST':
  if DEMO:
   d.execute('UPDATE users SET active=1 WHERE id=?',(g.user['id'],));d.commit();return redirect(url_for('dashboard'))
  if not os.getenv('LIVE_PAYMENTS')=='1':flash('Real payments are currently disabled.');return redirect(url_for('membership'))
  if not p or p['status'] in ('expired','failed'):
   try:
    order=secrets.token_hex(16); data=api('POST','payment',json={'price_amount':10,'price_currency':'usd','pay_currency':'usdttrc20','order_id':order,'order_description':'Membership access, no guaranteed earnings','is_fixed_rate':True,'ipn_callback_url':os.environ['PUBLIC_URL'].rstrip('/')+'/webhook'})
    d.execute('INSERT INTO payments(id,user_id,provider_id,address,amount) VALUES(?,?,?,?,?)',(order,g.user['id'],str(data['payment_id']),data['pay_address'],str(data['pay_amount'])));d.commit()
   except (requests.RequestException,KeyError,ValueError):flash('Payment service unavailable. No payment instruction was created.')
  elif p['provider_id']:
   try: verify(p['provider_id'])
   except (requests.RequestException,KeyError,ValueError):flash('Verification unavailable. Try again later.')
  return redirect(url_for('membership'))
 return render_template('page.html',page='membership',payment=p)
def verify(pid):
 d=db(); p=d.execute('SELECT * FROM payments WHERE provider_id=?',(str(pid),)).fetchone()
 if not p:return
 data=api('GET','payment/'+str(pid)); status=data.get('payment_status','waiting')
 if str(data.get('payment_id'))!=p['provider_id'] or data.get('order_id')!=p['id'] or data.get('pay_currency')!='usdttrc20':return
 if status=='finished' and Decimal(str(data.get('actually_paid') or 0))>=Decimal(p['amount']):
  d.execute('UPDATE users SET active=1 WHERE id=?',(p['user_id'],));d.execute("UPDATE payments SET status='finished' WHERE id=?",(p['id'],))
 elif status!='finished':d.execute("UPDATE payments SET status=? WHERE id=? AND status!='finished'",(status,p['id']))
 d.commit()
@app.post('/webhook')
def webhook():
 if DEMO or os.getenv('LIVE_PAYMENTS')!='1':abort(404)
 # Notification is only a hint; authoritative status is fetched directly using merchant API credentials.
 data=request.get_json(silent=True) or {}; pid=str(data.get('payment_id',''))
 if not pid.isdigit():abort(400)
 try:verify(pid)
 except (requests.RequestException,KeyError,ValueError):return 'retry',503
 return 'ok'
@app.route('/dashboard')
@auth(active=True)
def dashboard():
 d=db(); surveys=d.execute('SELECT * FROM surveys WHERE published=1 AND budget>=reward AND id NOT IN (SELECT survey_id FROM responses WHERE user_id=?)',(g.user['id'],)).fetchall(); history=d.execute('SELECT s.title,r.reward FROM responses r JOIN surveys s ON s.id=r.survey_id WHERE r.user_id=?',(g.user['id'],)).fetchall(); withdrawals=d.execute('SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC',(g.user['id'],)).fetchall();return render_template('page.html',page='dashboard',surveys=surveys,history=history,withdrawals=withdrawals)
@app.route('/survey/<int:id>',methods=['GET','POST'])
@auth(active=True)
def survey(id):
 d=db();s=d.execute('SELECT * FROM surveys WHERE id=? AND published=1',(id,)).fetchone()
 if not s:abort(404)
 questions=json.loads(s['questions'])
 if request.method=='POST':
  answers=[request.form.get('q'+str(i),'').strip() for i in range(len(questions))]
  if any(not a or len(a)>5000 for a in answers):abort(400)
  try:
   d.execute('BEGIN IMMEDIATE'); cur=d.execute('UPDATE surveys SET budget=budget-reward WHERE id=? AND published=1 AND budget>=reward AND reward=? AND questions=?',(id,s['reward'],s['questions']))
   if not cur.rowcount:raise ValueError()
   d.execute('INSERT INTO responses(user_id,survey_id,answers,reward) VALUES(?,?,?,?)',(g.user['id'],id,json.dumps(answers),s['reward']));d.execute('UPDATE users SET balance=balance+? WHERE id=?',(s['reward'],g.user['id']));d.commit();flash('Survey submitted.')
  except (sqlite3.IntegrityError,ValueError):d.rollback();flash('Already completed or no funded slots remain.')
  return redirect(url_for('dashboard'))
 return render_template('page.html',page='survey',survey=s,questions=questions)
@app.post('/withdraw')
@auth(active=True)
def withdraw():
 try: amount=cents(request.form['amount']);address=request.form['address'].strip();assert amount>=100 and re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}',address)
 except (ValueError,ArithmeticError,AssertionError):flash('Enter at least 1 USDT and a valid-looking TRON address.');return redirect(url_for('dashboard'))
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
 return render_template('page.html',page='admin',surveys=d.execute('SELECT * FROM surveys').fetchall(),users=d.execute('SELECT id,name,email,active,balance FROM users WHERE admin=0').fetchall(),withdrawals=d.execute('SELECT w.*,u.email FROM withdrawals w JOIN users u ON u.id=w.user_id').fetchall(),responses=d.execute('SELECT r.*,s.title,u.email FROM responses r JOIN surveys s ON s.id=r.survey_id JOIN users u ON u.id=r.user_id').fetchall())
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
 d=db();d.execute('BEGIN IMMEDIATE');w=d.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",(id,)).fetchone()
 if w:
  if request.form['action']=='reject':d.execute("UPDATE withdrawals SET status='rejected' WHERE id=?",(id,));d.execute('UPDATE users SET balance=balance+? WHERE id=?',(w['amount'],w['user_id']))
  elif request.form['action']=='paid':
   tx=request.form.get('tx','').strip()
   if not re.fullmatch(r'[a-fA-F0-9]{64}',tx):d.rollback();flash('Enter the actual TRON transaction hash.');return redirect(url_for('admin'))
   try:d.execute("UPDATE withdrawals SET status='paid',tx=? WHERE id=?",(tx.lower(),id))
   except sqlite3.IntegrityError:d.rollback();flash('Transaction already recorded.');return redirect(url_for('admin'))
  d.commit()
 return redirect(url_for('admin'))
if __name__=='__main__':app.run(host='127.0.0.1',port=8000)
