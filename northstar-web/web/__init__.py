"""Authenticated, testnet-only web interface. No production exchange routing."""
import base64
import functools
import hashlib
import hmac
import io
import csv
import warnings
import os
import re
import secrets
import sqlite3
import smtplib
from email.message import EmailMessage
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
import stripe
from requests.exceptions import RequestException
from PIL import Image, ImageOps, UnidentifiedImageError
from binance.client import Client
from binance.exceptions import BinanceAPIException
from cryptography.fernet import Fernet, InvalidToken
from flask import has_request_context, Flask, abort, g, jsonify, render_template, request, session, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
 disabled INTEGER NOT NULL DEFAULT 0, session_version INTEGER NOT NULL DEFAULT 0, credentials BLOB, avatar BLOB,
 customer TEXT UNIQUE, subscription TEXT, subscription_status TEXT NOT NULL DEFAULT 'none',
 billing_updated INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS super_admins (
 user_id INTEGER PRIMARY KEY REFERENCES users(id));
CREATE TABLE IF NOT EXISTS billing_prices (
 id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS platform_settings (
 name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS practice_resets (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), cutoff INTEGER NOT NULL, balance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_credentials (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), secret BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS live_orders (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 symbol TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL,
 quantity TEXT NOT NULL, price TEXT, status TEXT NOT NULL, exchange_id TEXT,
 created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), full_name TEXT NOT NULL, age INTEGER);
CREATE TABLE IF NOT EXISTS email_changes (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), token_hash TEXT NOT NULL,
 email TEXT NOT NULL, session_version INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 session_version INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checkouts (user_id INTEGER PRIMARY KEY REFERENCES users(id), url TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS billing_events(id TEXT PRIMARY KEY, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 symbol TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL,
 quantity TEXT NOT NULL, price TEXT, status TEXT NOT NULL,
 exchange_id TEXT, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS practice_quotes (order_id TEXT PRIMARY KEY REFERENCES orders(id), quote TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS practice_fills (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), price TEXT NOT NULL,
 value TEXT NOT NULL, pnl TEXT NOT NULL, balance TEXT NOT NULL,
 position TEXT NOT NULL, cost TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS limits (bucket TEXT NOT NULL, stamp INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS limit_bucket ON limits(bucket, stamp);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor INTEGER, action TEXT NOT NULL,
 target INTEGER, created INTEGER NOT NULL);
'''


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('APP_SECRET_KEY'),
        ENCRYPTION_KEY=os.environ.get('CREDENTIAL_ENCRYPTION_KEY'),
        DATABASE=os.environ.get('APP_DATABASE', str(Path(app.instance_path) / 'app.sqlite3')),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=True, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024, TESTING=False, PAPER_TRADING=False,
        STRIPE_SECRET_KEY=os.environ.get('STRIPE_SECRET_KEY'),
        STRIPE_WEBHOOK_SECRET=os.environ.get('STRIPE_WEBHOOK_SECRET'),
        STRIPE_PRICE_ID=os.environ.get('STRIPE_PRICE_ID'),
        PUBLIC_URL=os.environ.get('PUBLIC_URL', 'https://localhost'),
        BILLING_LIVE_ENABLED=os.environ.get('BILLING_LIVE_ENABLED') == 'true',
    )
    if config:
        app.config.update(config)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('APP_SECRET_KEY must contain at least 32 random characters.')
    if not app.config['ENCRYPTION_KEY']:
        raise RuntimeError('CREDENTIAL_ENCRYPTION_KEY is required (Fernet key).')
    cipher = Fernet(app.config['ENCRYPTION_KEY'])
    stripe.default_http_client = stripe.RequestsClient(timeout=10)
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        conn = g.pop('db', None)
        if conn:
            conn.close()

    with app.app_context():
        db().executescript(SCHEMA)
        db().commit()
        os.chmod(app.config['DATABASE'], 0o600)

    def audit(action, target=None):
        db().execute('INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)',
                     (session.get('uid') if has_request_context() else None, action, target, int(time.time())))

    def limit(bucket, count, seconds):
        # Shared across workers; atomic SQLite write transaction. Never trust X-Forwarded-For.
        conn = db()
        now = int(time.time())
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM limits WHERE stamp < ?', (now - 3600,))
            total = conn.execute('SELECT count(*) FROM limits WHERE bucket=? AND stamp>?',
                                 (bucket, now-seconds)).fetchone()[0]
            if total >= count:
                abort(429, 'Too many requests. Please wait before trying again.')
            conn.execute('INSERT INTO limits VALUES(?,?)', (bucket, now))

    def auth(admin=False):
        def decorate(fn):
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                if not g.user:
                    abort(401, 'Please sign in.')
                if admin and g.user['role'] != 'admin':
                    with db():
                        audit('admin_access_denied', g.user['id'])
                    abort(403, 'Administrator access required.')
                return fn(*args, **kwargs)
            return wrapped
        return decorate

    @app.before_request
    def protect():
        g.user = None
        if session.get('uid'):
            g.user = db().execute('SELECT * FROM users WHERE id=? AND disabled=0',
                                  (session['uid'],)).fetchone()
            if not g.user or session.get('version') != g.user['session_version']:
                g.user = None
                session.clear()
        if request.path.startswith('/api/'):
            limit('ip:' + (request.remote_addr or 'unknown'), 120, 60)
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.path != '/api/billing/webhook':
                if not hmac.compare_digest(session.get('csrf', ''), request.headers.get('X-CSRF-Token', '')) or not session.get('csrf'):
                    abort(403, 'Session expired. Refresh this page and try again.')
                if not request.is_json and request.path != '/api/profile/image':
                    abort(415, 'Use JSON requests.')

    @app.after_request
    def headers(response):
        response.headers.setdefault('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Cache-Control'] = 'no-store'
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if response.status_code == 429:
            response.headers['Retry-After'] = '60'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description if exc.code != 500 else 'Service unavailable.'), exc.code
        incident = secrets.token_hex(6)
        # Log identifiers and exception class only; never request bodies, credentials or raw errors.
        app.logger.error('Request failed incident=%s type=%s', incident, type(exc).__name__)
        return jsonify(error='An unexpected error occurred.', incident=incident), 500

    def payload():
        if request.content_length and request.content_length > 16384:
            abort(413, 'JSON request too large.')
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, 'Expected a JSON object.')
        return value

    def public_user(user):
        profile = db().execute('SELECT full_name, age FROM profiles WHERE user_id=?', (user['id'],)).fetchone()
        return dict(full_name=profile['full_name'] if profile else '', age=profile['age'] if profile else None, id=user['id'], email=user['email'], role=user['role'], connected=bool(user['credentials']), subscription=user['subscription_status'], avatar=bool(user['avatar']))

    @app.get('/')
    @app.get('/admin')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('index.html', csrf=session['csrf'], paper=app.config['PAPER_TRADING'])

    @app.get('/zapline')
    @app.get('/explore')
    def explore():
        source = (Path(app.static_folder) / 'northstar.html').read_text(encoding='utf-8-sig')
        def inline_hash(tag):
            body = source.split(f'<{tag}>', 1)[1].split(f'</{tag}>', 1)[0]
            digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
            return f"'sha256-{digest}'"

        response = app.response_class(source, mimetype='text/html')
        response.headers['Content-Security-Policy'] = (
            f"default-src 'none'; script-src {inline_hash('script')}; style-src {inline_hash('style')}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get('/api/session')
    def session_info():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return jsonify(user=public_user(g.user) if g.user else None, csrf=session['csrf'])

    def profile_fields(data):
        name = data.get('full_name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            abort(400, 'Enter your full name (up to 100 characters).')
        age = data.get('age')
        if age not in (None, ''):
            if not isinstance(age, str) or not re.fullmatch(r'[0-9]{1,3}', age) or not 1 <= int(age) <= 120:
                abort(400, 'Enter an age between 1 and 120.')
            age = int(age)
        return name.strip(), age or None

    @app.post('/api/profile')
    @auth()
    def update_profile():
        name, age = profile_fields(payload())
        with db():
            db().execute('INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(user_id) '
                         'DO UPDATE SET full_name=excluded.full_name, age=excluded.age',
                         (g.user['id'], name, age))
        return jsonify(ok=True)

    @app.post('/api/register')
    def register():
        limit('register:' + (request.remote_addr or ''), 5, 3600)
        data = payload()
        email = data.get('email', '')
        password = data.get('password', '')
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            abort(400, 'Enter a valid email address.')
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            abort(400, 'Use a password between 12 and 128 characters.')
        name, age = profile_fields(data)
        if data.get('confirm') != password:
            abort(400, 'Passwords must match.')
        hashed = generate_password_hash(password)
        try:
            with db():
                cursor = db().execute('INSERT INTO users(email,password,created) VALUES(?,?,?)',
                                      (email.lower().strip(), hashed, int(time.time())))
                db().execute('INSERT INTO profiles VALUES(?,?,?)', (cursor.lastrowid, name, age))
        except sqlite3.IntegrityError:
            abort(409, 'Unable to create this account. Try signing in.')
        return jsonify(ok=True), 201

    def require_mail():
        if not os.environ.get('SMTP_HOST') or not os.environ.get('SMTP_FROM'):
            abort(503, 'Account email delivery is unavailable. Contact your workspace administrator.')

    def send_account_link(recipient, subject, action, token):
        message = EmailMessage()
        message['From'], message['To'] = os.environ['SMTP_FROM'], recipient
        message['Subject'] = subject
        base = app.config['PUBLIC_URL'].rstrip('/')
        message.set_content(f'Open this Northstar link within 30 minutes: {base}/#{action}={token}')
        try:
            with smtplib.SMTP_SSL(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '465')), timeout=10) as smtp:
                if os.environ.get('SMTP_USER'):
                    smtp.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
                smtp.send_message(message)
        except (OSError, ValueError):
            app.logger.error('Northstar account email delivery failed')
            return False
        return True

    @app.post('/api/profile/email')
    @auth()
    def request_email_change():
        limit(f'email-change:{g.user["id"]}', 5, 900)
        require_mail()
        data = payload()
        email, password = data.get('email'), data.get('password')
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            abort(400, 'Enter a valid email address.')
        if not isinstance(password, str) or len(password) > 128 or not check_password_hash(g.user['password'], password):
            abort(400, 'Current password is incorrect.')
        email = email.lower()
        if db().execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            abort(409, 'Choose a different available email address.')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with db():
            db().execute('INSERT INTO email_changes VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                         'token_hash=excluded.token_hash,email=excluded.email,session_version=excluded.session_version,expires=excluded.expires',
                         (g.user['id'], digest, email, g.user['session_version'], int(time.time()) + 1800))
        if not send_account_link(email, 'Verify your new Northstar email address', 'verify-email', token):
            abort(503, 'Unable to send the verification email.')
        return jsonify(ok=True)

    @app.post('/api/profile/email/verify')
    @auth()
    def verify_email_change():
        token = payload().get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            abort(400, 'This verification link is invalid or expired.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            with db():
                change = db().execute(
                    'DELETE FROM email_changes WHERE user_id=? AND token_hash=? AND expires>? '
                    'RETURNING email,session_version', (g.user['id'], digest, int(time.time())),
                ).fetchone()
                if not change or not db().execute(
                    'UPDATE users SET email=?,session_version=session_version+1 '
                    'WHERE id=? AND session_version=? AND disabled=0',
                    (change['email'], g.user['id'], change['session_version']),
                ).rowcount:
                    abort(400, 'This verification link is invalid or expired.')
                db().execute('DELETE FROM password_resets WHERE user_id=?', (g.user['id'],))
        except sqlite3.IntegrityError:
            abort(409, 'This email address is unavailable.')
        session.clear()
        return jsonify(ok=True)

    @app.post('/api/password/forgot')
    def forgot_password():
        limit('forgot:' + (request.remote_addr or ''), 5, 900)
        email = payload().get('email')
        if not isinstance(email, str) or len(email) > 254:
            abort(400, 'Enter your email address.')
        require_mail()
        user = db().execute('SELECT * FROM users WHERE email=? AND disabled=0',
                            (email.strip().lower(),)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            with db():
                db().execute('DELETE FROM password_resets WHERE user_id=? OR expires<=?',
                             (user['id'], int(time.time())))
                db().execute('INSERT INTO password_resets VALUES(?,?,?,?)',
                             (digest, user['id'], user['session_version'], int(time.time()) + 1800))
            send_account_link(user['email'], 'Reset your Northstar password', 'reset', token)
        return jsonify(ok=True)

    @app.post('/api/password/reset')
    def reset_password():
        limit('reset:' + (request.remote_addr or ''), 10, 900)
        data = payload()
        token, password = data.get('token'), data.get('new')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            abort(400, 'This reset link is invalid or expired.')
        if not isinstance(password, str) or not 12 <= len(password) <= 128 or data.get('confirm') != password:
            abort(400, 'Use a matching password between 12 and 128 characters.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        hashed = generate_password_hash(password)
        with db():
            reset = db().execute('DELETE FROM password_resets WHERE token_hash=? AND expires>? '
                                 'RETURNING user_id, session_version', (digest, int(time.time()))).fetchone()
            if not reset or not db().execute(
                'UPDATE users SET password=?, session_version=session_version+1 '
                'WHERE id=? AND session_version=? AND disabled=0',
                (hashed, reset['user_id'], reset['session_version']),
            ).rowcount:
                abort(400, 'This reset link is invalid or expired.')
        with db():
            audit('password_reset_completed', reset['user_id'])
        session.clear()
        return jsonify(ok=True)

    # Constant-cost hash for unknown accounts prevents the obvious timing shortcut.
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32))

    @app.post('/api/login')
    def login():
        data = payload()
        email, password = data.get('email'), data.get('password')
        if not isinstance(email, str) or not isinstance(password, str) or len(email)>254 or len(password)>128:
            abort(400, 'Invalid sign-in details.')
        limit('login-ip:' + (request.remote_addr or ''), 10, 900)
        limit('login-account:' + hashlib.sha256(email.lower().encode()).hexdigest(), 10, 900)
        user = db().execute('SELECT * FROM users WHERE email=?', (email.lower().strip(),)).fetchone()
        valid = check_password_hash(user['password'] if user else dummy_hash, password)
        if not user or not valid or user['disabled']:
            with db():
                audit('login_failed', user['id'] if user else None)
            abort(401, 'Email or password is incorrect.')
        session.clear()
        session.update(uid=user['id'], version=user['session_version'], csrf=secrets.token_urlsafe(32))
        session.permanent = True
        with db():
            audit('login', user['id'])
        return jsonify(user=public_user(user), csrf=session['csrf'])

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.post('/api/password')
    @auth()
    def password_change():
        limit(f'password:{g.user["id"]}', 5, 900)
        data = payload()
        old, new = data.get('current'), data.get('new')
        if not isinstance(old,str) or len(old)>128 or not isinstance(new,str) or not 12 <= len(new) <= 128:
            abort(400, 'Use a new password between 12 and 128 characters.')
        if data.get('confirm') != new:
            abort(400, 'New passwords must match.')
        if old == new:
            abort(400, 'Choose a different new password.')
        if not check_password_hash(g.user['password'], old):
            abort(400, 'Current password is incorrect.')
        with db():
            db().execute('UPDATE users SET password=?, session_version=session_version+1 WHERE id=?',
                (generate_password_hash(new), g.user['id']))
            audit('password_changed',g.user['id'])
        session.clear()
        return jsonify(ok=True)

    @app.post('/api/credentials')
    @auth()
    def credentials():
        limit(f'credentials:{g.user["id"]}', 5, 60)
        data = payload()
        key, secret = data.get('key'), data.get('secret')
        if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9]{16,256}', v) for v in (key, secret)):
            abort(400, 'Enter valid testnet API credentials.')
        with db():
            db().execute('UPDATE users SET credentials=? WHERE id=?', (cipher.encrypt((key+'\n'+secret).encode()), g.user['id']))
            audit('credentials_updated', g.user['id'])
        return jsonify(ok=True)

    @app.delete('/api/credentials')
    @auth()
    def disconnect():
        with db():
            db().execute('UPDATE users SET credentials=NULL WHERE id=?', (g.user['id'],))
            audit('credentials_removed', g.user['id'])
        return jsonify(ok=True)

    @app.get('/api/orders')
    @auth()
    def orders():
        paper = app.config['PAPER_TRADING']
        return jsonify(orders=[dict(row) for row in db().execute(
            'SELECT o.id,o.symbol,o.side,o.kind,o.quantity,o.price,o.status,o.exchange_id,o.created,'
            'f.price AS fill_price,f.pnl FROM orders o LEFT JOIN practice_fills f ON f.order_id=o.id '
            "WHERE o.user_id=? AND (o.status IN ('PRACTICE','FILLED_PRACTICE','PENDING_PRACTICE'))=? "
            'ORDER BY o.created DESC,o.rowid DESC LIMIT 100', (g.user['id'], paper),
        )])

    def number(value, name):
        if not isinstance(value, str) or len(value)>24 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
            abort(400, f'{name} must be a positive decimal number.')
        try:
            n = Decimal(value)
        except InvalidOperation:
            abort(400, f'Invalid {name}.')
        if not n.is_finite() or n <= 0 or n > Decimal('1000000000'):
            abort(400, f'{name} is outside the allowed range.')
        return format(n, 'f')

    @app.get('/api/practice/analytics')
    @auth()
    def practice_analytics():
        if not app.config['PAPER_TRADING']:
            abort(404)
        cutoff, initial = practice_start(g.user['id'])
        trades = db().execute(
            'SELECT o.side,o.created,f.value,f.pnl,f.balance FROM practice_fills f '
            'JOIN orders o ON f.order_id=o.id WHERE o.user_id=? AND o.rowid>? ORDER BY o.rowid',
            (g.user['id'], cutoff),
        ).fetchall()
        values = {side: [Decimal(row['value']) for row in trades if row['side'] == side]
                  for side in ('BUY', 'SELL')}
        pnl = [Decimal(row['pnl']) for row in trades if row['side'] == 'SELL']
        return jsonify(
            balance=trades[-1]['balance'] if trades else initial, initial=initial,
            pnl=str(sum(pnl)), wins=sum(value > 0 for value in pnl),
            losses=sum(value < 0 for value in pnl), total=len(trades),
            average=str(sum(values['BUY'] + values['SELL']) / len(trades)) if trades else None,
            sides={side: dict(count=len(items), low=str(min(items)) if items else None,
                              high=str(max(items)) if items else None) for side, items in values.items()},
            series=[dict(created=row['created'], balance=row['balance']) for row in trades[-100:]],
        )

    def record_practice(token, symbol, side, kind, quantity, price, quote):
        conn = db()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute(
                'SELECT o.*, q.quote FROM orders o LEFT JOIN practice_quotes q '
                'ON o.id=q.order_id WHERE o.id=?', (token,),
            ).fetchone()
            if existing:
                if (existing['user_id'], existing['symbol'], existing['side'], existing['kind'],
                    existing['quantity'], existing['price']) != (g.user['id'], symbol, side, kind, quantity, price):
                    abort(409, 'Submission key belongs to a different order.')
                if existing['status'] not in ('PRACTICE', 'FILLED_PRACTICE', 'PENDING_PRACTICE'):
                    abort(409, 'Submission key already used.')
                if existing['quote'] is not None and existing['quote'] != quote:
                    abort(409, 'Submission key belongs to a different reference price.')
                return jsonify(id=token, status=existing['status'])
            fill = kind == 'MARKET' or (Decimal(quote) <= Decimal(price) if side == 'BUY' else Decimal(quote) >= Decimal(price))
            status = 'PENDING_PRACTICE' if not fill else 'FILLED_PRACTICE'
            if fill:
                cutoff, initial = practice_start(g.user['id'])
                previous_fill = conn.execute(
                    'SELECT 1 FROM practice_fills f JOIN orders o ON o.id=f.order_id WHERE o.user_id=? LIMIT 1',
                    (g.user['id'],),
                ).fetchone()
                if not previous_fill:
                    conn.execute('INSERT OR IGNORE INTO practice_resets VALUES(?,?,?)', (g.user['id'], cutoff, initial))
                cash = conn.execute(
                    'SELECT f.balance FROM practice_fills f JOIN orders o ON f.order_id=o.id '
                    'WHERE o.user_id=? AND o.rowid>? ORDER BY o.rowid DESC LIMIT 1', (g.user['id'], cutoff),
                ).fetchone()
                asset = conn.execute(
                    'SELECT f.position,f.cost FROM practice_fills f JOIN orders o ON f.order_id=o.id '
                    'WHERE o.user_id=? AND o.symbol=? AND o.rowid>? ORDER BY o.rowid DESC LIMIT 1',
                    (g.user['id'], symbol, cutoff),
                ).fetchone()
                balance = Decimal(cash['balance']) if cash else Decimal(initial)
                position, cost = map(Decimal, asset) if asset else (Decimal(0), Decimal(0))
                size, value = Decimal(quantity), Decimal(quantity) * Decimal(quote)
                pnl = Decimal(0)
                if side == 'BUY':
                    if value > balance:
                        abort(400, 'Insufficient virtual balance.')
                    balance, position, cost = balance - value, position + size, cost + value
                else:
                    if size > position:
                        abort(400, 'Insufficient virtual holdings for this pair.')
                    allocated = cost if size == position else cost * size / position
                    pnl = value - allocated
                    balance, position, cost = balance + value, position - size, cost - allocated
            conn.execute('INSERT INTO orders(id,user_id,symbol,side,kind,quantity,price,status,created) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (token,g.user['id'],symbol,side,kind,quantity,price,status,int(time.time())))
            conn.execute('INSERT INTO practice_quotes VALUES(?,?)', (token, quote))
            if fill:
                conn.execute('INSERT INTO practice_fills VALUES(?,?,?,?,?,?,?)',
                             (token,quote,str(value),str(pnl),str(balance),str(position),str(cost)))
            audit('practice_order_recorded', g.user['id'])
        return jsonify(id=token,status=status), 201

    def live_client(key, secret):
        client = Client(key, secret, testnet=False, ping=False, requests_params={'timeout': (4, 10)})
        client.FUTURES_URL = 'https://fapi.binance.com/fapi'
        return client

    def saved_live_client():
        row = db().execute('SELECT secret FROM live_credentials WHERE user_id=?', (g.user['id'],)).fetchone()
        if not row:
            abort(400, 'Connect a live Binance Futures API key first.')
        try:
            key, secret = cipher.decrypt(row['secret']).decode().split('\n')
        except (InvalidToken, ValueError):
            abort(503, 'Reconnect your live Binance Futures account.')
        return live_client(key, secret)

    @app.post('/api/live/credentials')
    @auth()
    def connect_live():
        limit(f'live-connect:{g.user["id"]}', 5, 60)
        data = payload()
        key, secret = data.get('key'), data.get('secret')
        if not all(isinstance(value, str) and re.fullmatch('[A-Za-z0-9]{16,256}', value) for value in (key, secret)):
            abort(400, 'Enter a valid Binance HMAC API key and secret.')
        try:
            client = live_client(key, secret)
            try:
                client.futures_account()
            finally:
                client.close_connection()
        except BinanceAPIException as exc:
            with db():
                audit('live_connection_rejected', g.user['id'])
            if exc.status_code == 451:
                abort(502, 'Binance Futures is unavailable from this server location (HTTP 451).')
            hints = {
                -2014: 'Check the API key format and use a production Binance HMAC key.',
                -2015: 'Check that the key is for production Binance, has USDⓈ-M Futures access, and allows this PC’s public IP.',
                -1021: 'Sync the Windows clock under Settings → Time & language → Date & time, then try again.',
                -1022: 'Check that the API secret matches this HMAC key.',
                -1011: 'Check the key’s IP allowlist for this PC’s public IP.',
            }
            code = exc.code if isinstance(exc.code, int) else 0
            abort(502, f'Binance rejected verification ({code}). ' +
                  hints.get(code, 'Check your Binance Futures account and API permissions.'))
        except RequestException:
            with db():
                audit('live_connection_unreachable', g.user['id'])
            abort(502, 'Northstar could not reach Binance Futures. Check the server’s internet connection and try again.')
        with db():
            db().execute('INSERT INTO live_credentials VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET secret=excluded.secret',
                         (g.user['id'], cipher.encrypt(f'{key}\n{secret}'.encode())))
            audit('live_credentials_updated', g.user['id'])
        return jsonify(ok=True)

    @app.delete('/api/live/credentials')
    @auth()
    def disconnect_live():
        with db():
            db().execute('DELETE FROM live_credentials WHERE user_id=?', (g.user['id'],))
            audit('live_credentials_removed', g.user['id'])
        return jsonify(ok=True)

    @app.get('/api/live/account')
    @auth()
    def live_account():
        limit(f'live-account:{g.user["id"]}', 20, 60)
        uid = g.user['id']
        orders = [dict(row) for row in db().execute(
            'SELECT id,symbol,side,kind,quantity,price,status,exchange_id,created FROM live_orders '
            'WHERE user_id=? ORDER BY created DESC,rowid DESC LIMIT 100', (uid,),
        )]
        if not db().execute('SELECT 1 FROM live_credentials WHERE user_id=?', (uid,)).fetchone():
            return jsonify(connected=False, orders=orders)
        client = saved_live_client()
        try:
            account = client.futures_account()
        except Exception:
            return jsonify(connected=True, orders=orders, error='Binance account data is temporarily unavailable.')
        finally:
            client.close_connection()
        assets = [{field: asset.get(field) for field in ('asset', 'walletBalance', 'availableBalance', 'unrealizedProfit')}
                  for asset in account.get('assets', []) if asset.get('asset') == 'USDT']
        return jsonify(connected=True, orders=orders, assets=assets, can_trade=account.get('canTrade') is True)

    @app.post('/api/live/orders/<token>/status')
    @auth()
    def sync_live_order(token):
        limit(f'live-status:{g.user["id"]}', 20, 60)
        row = db().execute('SELECT * FROM live_orders WHERE id=? AND user_id=?',
                           (token, g.user['id'])).fetchone()
        if not row:
            abort(404, 'Live order not found.')
        client = saved_live_client()
        try:
            result = client.futures_get_order(symbol=row['symbol'], origClientOrderId='ns-'+token)
            exchange_id, status = str(result['orderId']), result['status']
        except Exception:
            abort(502, 'Binance could not confirm this order. Check the order ID directly in Binance.')
        finally:
            client.close_connection()
        with db():
            db().execute('UPDATE live_orders SET status=?,exchange_id=? WHERE id=? AND user_id=?',
                         (status, exchange_id, token, g.user['id']))
        return jsonify(status=status, exchange_id=exchange_id)

    @app.post('/api/live/orders')
    @auth()
    def submit_live():
        limit(f'live-orders:{g.user["id"]}', 10, 60)
        data = payload()
        if data.get('confirmation') != 'LIVE':
            abort(400, 'Type LIVE to authorize this real-money order.')
        symbol, side, kind = data.get('symbol'), data.get('side'), data.get('kind')
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{1,16}USDT', symbol):
            abort(400, 'Enter a USDT futures pair.')
        if side not in ('BUY', 'SELL') or kind not in ('MARKET', 'LIMIT'):
            abort(400, 'Invalid order side or type.')
        quantity = number(data.get('quantity'), 'Quantity')
        price = number(data.get('price'), 'Limit price') if kind == 'LIMIT' else None
        token = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch('[a-f0-9]{32}', token):
            abort(400, 'A valid order submission key is required.')
        uid = g.user['id']
        client = saved_live_client()
        try:
            with db():
                cursor = db().execute(
                    'INSERT INTO live_orders(id,user_id,symbol,side,kind,quantity,price,status,created) '
                    "VALUES(?,?,?,?,?,?,?,'SUBMITTING',?) ON CONFLICT(id) DO NOTHING",
                    (token, uid, symbol, side, kind, quantity, price, int(time.time())),
                )
                if not cursor.rowcount:
                    previous = db().execute('SELECT * FROM live_orders WHERE id=?', (token,)).fetchone()
                    if tuple(previous[field] for field in ('user_id','symbol','side','kind','quantity','price')) != (uid,symbol,side,kind,quantity,price):
                        abort(409, 'Submission key already used.')
                    return jsonify(id=token, status=previous['status'], exchange_id=previous['exchange_id'])
                audit('live_order_submitted', uid)
            params = dict(symbol=symbol, side=side, type=kind, quantity=quantity, newClientOrderId='ns-'+token)
            if kind == 'LIMIT':
                params.update(price=price, timeInForce='GTC')
            status, exchange_id = 'UNKNOWN', None
            try:
                result = client.futures_create_order(**params)
                exchange_id = str(result['orderId'])
                status = result.get('status', 'SUBMITTED')
            except BinanceAPIException as error:
                if 400 <= error.status_code < 500 and error.status_code != 408 and error.code not in (-1000, -1001, -1006, -1007, -4116):
                    status = 'REJECTED'
            except Exception:
                pass
            with db():
                db().execute('UPDATE live_orders SET status=?,exchange_id=? WHERE id=?', (status, exchange_id, token))
            if status == 'UNKNOWN':
                return jsonify(status=status, error='Order outcome is uncertain. Check Binance for order ns-'+token+' before resubmitting.'), 502
            if status == 'REJECTED':
                return jsonify(status=status, error='Binance rejected the order. Check account permissions, position mode, available margin, and pair limits.'), 422
            return jsonify(id=token,status=status,exchange_id=exchange_id), 201
        finally:
            client.close_connection()

    @app.post('/api/orders')
    @auth()
    def submit():
        limit(f'orders:{g.user["id"]}', 10, 60)
        data = payload()
        if data.get('confirmed') is not True:
            abort(400, 'Confirm this order before submitting.')
        symbol, side, kind = data.get('symbol'), data.get('side'), data.get('kind')
        if not isinstance(symbol, str) or not re.fullmatch('[A-Z0-9]{5,20}', symbol):
            abort(400, 'Invalid trading symbol.')
        if side not in ('BUY','SELL') or kind not in ('MARKET','LIMIT'):
            abort(400, 'Invalid order side or type.')
        quantity = number(data.get('quantity'), 'Quantity')
        price = number(data.get('price'), 'Price') if kind == 'LIMIT' else None
        token = request.headers.get('Idempotency-Key','')
        if not re.fullmatch('[a-f0-9]{32}', token):
            abort(400, 'A valid order submission key is required.')
        if app.config['PAPER_TRADING']:
            if not symbol.endswith('USDT') or len(symbol) <= 4:
                abort(400, 'Practice pairs must be quoted in USDT.')
            quote = number(data.get('quote'), 'Practice reference price')
            return record_practice(token, symbol, side, kind, quantity, price, quote)
        existing = db().execute('SELECT * FROM orders WHERE id=?', (token,)).fetchone()
        if existing:
            if existing['user_id'] != g.user['id'] or existing['status'] in ('PRACTICE','FILLED_PRACTICE','PENDING_PRACTICE'):
                abort(409, 'Submission key already used.')
            if (existing['symbol'],existing['side'],existing['kind'],existing['quantity'],existing['price']) != (symbol,side,kind,quantity,price):
                abort(409, 'Submission key belongs to a different order.')
            return jsonify(id=token,status=existing['status'])
        if not g.user['credentials']:
            abort(400, 'Connect your Binance Futures Testnet account first.')
        try:
            key, secret = cipher.decrypt(g.user['credentials']).decode().split('\n')
        except (InvalidToken, ValueError):
            abort(503, 'Reconnect your testnet account.')
        try:
            with db():
                db().execute('INSERT INTO orders(id,user_id,symbol,side,kind,quantity,price,status,created) VALUES(?,?,?,?,?,?,?,?,?)',
                             (token,g.user['id'],symbol,side,kind,quantity,price,'SUBMITTING',int(time.time())))
                audit('order_submitted', g.user['id'])
        except sqlite3.IntegrityError:
            abort(409, 'This submission is already being processed. Refresh order history.')
        client = None
        status, exchange_id = 'UNKNOWN', None
        try:
            # ping=False avoids an unrelated spot endpoint; hard-pin futures testnet.
            client = Client(key,secret,testnet=True,ping=False,requests_params={'timeout':(5,15)})
            client.FUTURES_TESTNET_URL = 'https://testnet.binancefuture.com/fapi'
            params = dict(symbol=symbol,side=side,type=kind,quantity=quantity,newClientOrderId='web-'+token)
            if kind == 'LIMIT':
                params.update(price=price,timeInForce='GTC')
            response = client.futures_create_order(**params)
            status, exchange_id = response.get('status','SUBMITTED'), str(response.get('orderId',''))
        except Exception:
            # An interrupted response may mean the exchange accepted the order. Never auto-retry.
            app.logger.warning('Exchange outcome uncertain order=%s',token)
        finally:
            if client:
                try:
                    client.close_connection()
                except Exception:
                    pass
        with db():
            db().execute('UPDATE orders SET status=?,exchange_id=? WHERE id=?',(status,exchange_id,token))
        if status == 'UNKNOWN':
            return jsonify(id=token,status=status,error='Exchange outcome is uncertain. Check Binance testnet before placing another order.'), 502
        return jsonify(id=token,status=status), 201

    def practice_start(uid):
        reset = db().execute('SELECT cutoff,balance FROM practice_resets WHERE user_id=?', (uid,)).fetchone()
        if reset:
            return reset['cutoff'], reset['balance']
        setting = db().execute("SELECT value FROM platform_settings WHERE name='practice_start'").fetchone()
        return 0, setting['value'] if setting else '10000'

    @app.get('/api/admin/overview')
    @auth(admin=True)
    def admin_overview():
        conn = db()
        now = int(time.time())
        week = now - 7 * 86400
        counts = dict(conn.execute(
            "SELECT count(*) AS users, coalesce(sum(disabled=0),0) AS enabled, "
            "coalesce(sum(created>=?),0) AS registrations, "
            "coalesce(sum(subscription_status='active'),0) AS subscribers FROM users", (week,),
        ).fetchone())
        counts['recent_users'] = conn.execute(
            "SELECT count(DISTINCT actor) FROM audit WHERE action='login' AND created>=?", (week,),
        ).fetchone()[0]
        counts['practice_accounts'] = conn.execute(
            "SELECT count(*) FROM (SELECT user_id FROM orders WHERE status LIKE '%PRACTICE%' "
            "UNION SELECT user_id FROM practice_resets)"
        ).fetchone()[0]
        counts['live_accounts'] = conn.execute('SELECT count(*) FROM live_credentials').fetchone()[0]
        counts['orders'], counts['buys'], counts['sells'], counts['failed'] = conn.execute(
            "SELECT count(*),coalesce(sum(side='BUY'),0),coalesce(sum(side='SELL'),0),"
            "coalesce(sum(status IN ('REJECTED','UNKNOWN')),0) FROM "
            "(SELECT side,status FROM orders UNION ALL SELECT side,status FROM live_orders)"
        ).fetchone()
        fills = conn.execute('SELECT value,pnl FROM practice_fills').fetchall()
        counts['practice_fills'] = len(fills)
        counts['practice_volume'] = str(sum(Decimal(row['value']) for row in fills))
        counts['practice_pnl'] = str(sum(Decimal(row['pnl']) for row in fills))
        counts['filled_trades'] = len(fills) + conn.execute(
            "SELECT (SELECT count(*) FROM orders WHERE status='FILLED') + "
            "(SELECT count(*) FROM live_orders WHERE status='FILLED')"
        ).fetchone()[0]
        users = [dict(row) for row in conn.execute(
            "SELECT u.id,u.email,u.role,u.disabled,u.created,u.subscription_status,p.full_name,p.age,"
            "EXISTS(SELECT 1 FROM live_credentials l WHERE l.user_id=u.id) AS live_connected,"
            "EXISTS(SELECT 1 FROM orders o WHERE o.user_id=u.id AND o.status LIKE '%PRACTICE%') "
            "OR EXISTS(SELECT 1 FROM practice_resets r WHERE r.user_id=u.id) AS practice "
            "FROM users u LEFT JOIN profiles p ON p.user_id=u.id ORDER BY u.id"
        )]
        orders = [dict(row) for row in conn.execute(
            "SELECT user_id,symbol,side,kind,quantity,price,status,created,mode FROM "
            "(SELECT user_id,symbol,side,kind,quantity,price,status,created,"
            "CASE WHEN status LIKE '%PRACTICE%' THEN 'Practice' ELSE 'Testnet' END AS mode FROM orders "
            "UNION ALL SELECT user_id,symbol,side,kind,quantity,price,status,created,'Live' FROM live_orders) "
            "ORDER BY created DESC LIMIT 200"
        )]
        events = [dict(row) for row in conn.execute(
            'SELECT actor,action,target,created FROM audit ORDER BY id DESC LIMIT 200'
        )]
        growth = [dict(row) for row in conn.execute(
            "SELECT date(created,'unixepoch') AS day,count(*) AS registrations FROM users "
            "WHERE created>=? GROUP BY day ORDER BY day", (now - 30 * 86400,),
        )]
        subscriptions = [dict(row) for row in conn.execute(
            'SELECT subscription_status AS status,count(*) AS total FROM users GROUP BY subscription_status'
        )]
        return jsonify(counts=counts,users=users,orders=orders,events=events,growth=growth,
                       subscriptions=subscriptions,stripe_configured=bool(selected_price() and app.config['STRIPE_SECRET_KEY'] and app.config['STRIPE_WEBHOOK_SECRET']),
                       billing_price=selected_price(), billing_label=billing_label(),
                       practice_start=practice_start(0)[1],
                       super_admin=bool(conn.execute('SELECT 1 FROM super_admins WHERE user_id=?',(g.user['id'],)).fetchone()))

    @app.post('/api/admin/practice/<int:uid>/reset')
    @auth(admin=True)
    def reset_practice(uid):
        if payload().get('confirm') != 'RESET':
            abort(400, 'Confirm the practice reset.')
        with db():
            db().execute('BEGIN IMMEDIATE')
            if not db().execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                abort(404, 'User not found.')
            cutoff = db().execute('SELECT coalesce(max(rowid),0) FROM orders').fetchone()[0]
            db().execute('INSERT INTO practice_resets VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                         'cutoff=excluded.cutoff,balance=excluded.balance', (uid,cutoff,practice_start(0)[1]))
            audit('practice_reset', uid)
        return jsonify(ok=True)

    @app.post('/api/admin/settings/practice')
    @auth(admin=True)
    def configure_practice():
        if not db().execute('SELECT 1 FROM super_admins WHERE user_id=?',(g.user['id'],)).fetchone():
            abort(403, 'Super Admin access required.')
        amount = number(payload().get('balance'), 'Starting balance')
        with db():
            db().execute("INSERT INTO platform_settings VALUES('practice_start',?) "
                         "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (amount,))
            audit('practice_start_configured',g.user['id'])
        return jsonify(ok=True)

    @app.get('/api/admin/users')
    @auth(admin=True)
    def admin_users():
        return jsonify(users=[dict(row) for row in db().execute('SELECT id,email,role,disabled,created FROM users ORDER BY id LIMIT 200')])

    @app.post('/api/admin/users/<int:uid>/access')
    @auth(admin=True)
    def access(uid):
        data = payload()
        if type(data.get('disabled')) is not bool:
            abort(400, 'disabled must be a boolean.')
        if uid == g.user['id']:
            abort(400, 'You cannot disable your own account.')
        with db():
            cursor = db().execute("UPDATE users SET disabled=?, session_version=session_version+1 WHERE id=? AND role='user'",(int(data['disabled']),uid))
            if not cursor.rowcount:
                abort(404,'User not found or protected administrator.')
            audit('user_disabled' if data['disabled'] else 'user_enabled',uid)
        return jsonify(ok=True)

    @app.cli.command('promote-admin')
    @click.argument('email')
    @click.option('--super-admin', is_flag=True)
    def promote(email, super_admin):
        """Promote an existing account; requires trusted server shell access."""
        with db():
            cursor = db().execute("UPDATE users SET role='admin', session_version=session_version+1 WHERE email=?",(email.lower(),))
            if not cursor.rowcount:
                raise click.ClickException('Account not found.')
            uid = db().execute('SELECT id FROM users WHERE email=?', (email.lower(),)).fetchone()['id']
            if super_admin:
                db().execute('INSERT OR IGNORE INTO super_admins VALUES(?)', (uid,))
            audit('super_admin_promoted' if super_admin else 'admin_promoted', uid)
        click.echo('Administrator access granted.')

    @app.post('/api/profile/image')
    @auth()
    def avatar_upload():
        limit(f'avatar:{g.user["id"]}', 5, 60)
        uploaded = request.files.get('image')
        if not uploaded:
            abort(400, 'Choose a PNG, JPEG, or WebP image (maximum 2 MB).')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(uploaded.stream) as image:
                    if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 16000000:
                        abort(400, 'Unsupported image or excessive dimensions.')
                    image.load()
                    image = ImageOps.exif_transpose(image).convert('RGB')
                    image = ImageOps.fit(image, (256, 256))
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=85)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            abort(400, 'This file is not a supported image.')
        with db():
            db().execute('UPDATE users SET avatar=? WHERE id=?',(output.getvalue(),g.user['id']))
        return jsonify(ok=True)

    @app.get('/api/profile/image')
    @auth()
    def avatar_image():
        if not g.user['avatar']:
            abort(404, 'No profile image uploaded.')
        return send_file(io.BytesIO(g.user['avatar']), mimetype='image/jpeg', download_name='profile.jpg')

    def selected_price():
        row = db().execute("SELECT value FROM platform_settings WHERE name='stripe_price'").fetchone()
        return row['value'] if row else app.config['STRIPE_PRICE_ID']

    def billing_label():
        row = db().execute('SELECT label FROM billing_prices WHERE id=?', (selected_price(),)).fetchone()
        return row['label'] if row else None

    @app.get('/api/billing/plan')
    @auth()
    def billing_plan():
        return jsonify(label=billing_label(), member_export=g.user['subscription_status']=='active')

    @app.post('/api/admin/billing/price')
    @auth(admin=True)
    def choose_billing_price():
        limit(f'billing-price:{g.user["id"]}', 5, 60)
        price_id = payload().get('price_id')
        if not isinstance(price_id, str) or not re.fullmatch(r'price_[A-Za-z0-9]{1,100}', price_id):
            abort(400, 'Enter a Stripe recurring Price ID.')
        key = app.config['STRIPE_SECRET_KEY']
        if not key:
            abort(503, 'Configure a Stripe secret key before selecting a price.')
        try:
            price = stripe.Price.retrieve(price_id, api_key=key)
        except stripe.StripeError:
            abort(502, 'Stripe could not verify that Price ID.')
        if not price['active'] or not price['recurring'] or price['unit_amount'] is None or price.get('billing_scheme') != 'per_unit':
            abort(400, 'Select an active flat-rate recurring Stripe price.')
        if price['livemode'] and not app.config['BILLING_LIVE_ENABLED']:
            abort(403, 'Live billing is disabled for this deployment.')
        cadence = price['recurring']
        label = f"{price['currency'].upper()} {price['unit_amount']} minor units / {cadence['interval_count']} {cadence['interval']}"
        with db():
            db().execute('INSERT OR IGNORE INTO billing_prices VALUES(?,?)', (price_id,label))
            db().execute("INSERT INTO platform_settings VALUES('stripe_price',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (price_id,))
            audit('billing_price_selected', g.user['id'])
        return jsonify(ok=True, label=label)

    @app.get('/api/billing/member-orders.csv')
    @auth()
    def member_orders_csv():
        if g.user['subscription_status'] != 'active':
            abort(403, 'An active Northstar membership is required.')
        rows = db().execute(
            "SELECT 'Practice',symbol,side,kind,quantity,price,status,created FROM orders "
            "WHERE user_id=? AND status LIKE '%PRACTICE%' UNION ALL "
            "SELECT 'Live',symbol,side,kind,quantity,price,status,created FROM live_orders "
            "WHERE user_id=? ORDER BY created DESC", (g.user['id'],g.user['id']),
        )
        report = io.StringIO()
        writer = csv.writer(report)
        writer.writerow(['Mode','Pair','Side','Type','Quantity','Limit price','Status','Unix timestamp'])
        writer.writerows(rows)
        return app.response_class(report.getvalue(), mimetype='text/csv',
                                  headers={'Content-Disposition':'attachment; filename=northstar-orders.csv'})

    def billing_ready():
        if not selected_price() or not all(app.config.get(name) for name in ('STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET')):
            abort(503, 'Subscriptions are not configured yet. No payment has been taken.')
        if not app.config['BILLING_LIVE_ENABLED'] and not app.config['STRIPE_SECRET_KEY'].startswith(('sk_test_', 'rk_test_')):
            abort(503, 'Live payments are disabled. Configure a Stripe test-mode key.')
        base = app.config['PUBLIC_URL'].rstrip('/')
        if not base.startswith('https://') or base == 'https://localhost':
            abort(503, 'Billing requires a configured public HTTPS address.')
        return base

    @app.post('/api/billing/checkout')
    @auth()
    def checkout():
        limit(f'checkout:{g.user["id"]}', 3, 60)
        base = billing_ready()
        if g.user['subscription_status'] in ('active','trialing','past_due','unpaid'):
            abort(409, 'Manage your existing subscription in the billing portal.')
        try:
            with db():
                db().execute('BEGIN IMMEDIATE')
                cached = db().execute('SELECT * FROM checkouts WHERE user_id=? AND expires>?',
                    (g.user['id'],int(time.time()))).fetchone()
                if cached:
                    return jsonify(url=cached['url'])
                customer = g.user['customer']
                if not customer:
                    created = stripe.Customer.create(email=g.user['email'], metadata={'user_id':str(g.user['id'])},
                        api_key=app.config['STRIPE_SECRET_KEY'],idempotency_key=f'customer-{g.user["id"]}')
                    customer = created.id
                    db().execute('UPDATE users SET customer=? WHERE id=?',(customer,g.user['id']))
                checkout_session = stripe.checkout.Session.create(
                    api_key=app.config['STRIPE_SECRET_KEY'], customer=customer, mode='subscription',
                    line_items=[{'price':selected_price(),'quantity':1}],
                    client_reference_id=str(g.user['id']),
                    subscription_data={'metadata':{'user_id':str(g.user['id'])}},
                    success_url=base+'/?billing=returned',cancel_url=base+'/?billing=cancelled',
                    expires_at=(int(time.time())//1800)*1800+3600,
                    idempotency_key=f'checkout-{g.user["id"]}-{int(time.time())//1800}')
                db().execute('INSERT OR REPLACE INTO checkouts VALUES(?,?,?)',
                    (g.user['id'],checkout_session.url,int(time.time())+1700))
        except stripe.StripeError:
            abort(502, 'Payment provider unavailable. Please try again later.')
        return jsonify(url=checkout_session.url)

    @app.post('/api/billing/portal')
    @auth()
    def portal():
        base = billing_ready()
        if not g.user['customer']:
            abort(400, 'No billing account exists yet.')
        try:
            result = stripe.billing_portal.Session.create(customer=g.user['customer'],return_url=base,
                api_key=app.config['STRIPE_SECRET_KEY'])
        except stripe.StripeError:
            abort(502,'Billing portal unavailable. Please try again later.')
        return jsonify(url=result.url)

    @app.post('/api/billing/webhook')
    def webhook():
        secret = app.config['STRIPE_WEBHOOK_SECRET']
        if not secret:
            abort(503, 'Billing is not configured.')
        try:
            event = stripe.Webhook.construct_event(request.get_data(),request.headers.get('Stripe-Signature',''),secret)
        except (ValueError,stripe.SignatureVerificationError):
            abort(400,'Invalid webhook signature.')
        if event['type'] not in ('customer.subscription.created','customer.subscription.updated','customer.subscription.deleted'):
            return jsonify(received=True)
        obj = event['data']['object']
        with db():
            db().execute('BEGIN IMMEDIATE')
            if db().execute('SELECT 1 FROM billing_events WHERE id=?',(event['id'],)).fetchone():
                return jsonify(received=True)
            user = db().execute('SELECT * FROM users WHERE customer=?',(obj['customer'],)).fetchone()
            if user:
                try:
                    current = stripe.Subscription.list(customer=user['customer'],status='all',limit=100,
                        api_key=app.config['STRIPE_SECRET_KEY'])
                except stripe.StripeError:
                    abort(503, 'Unable to synchronize billing. Please retry webhook delivery.')
                prices = {row['id'] for row in db().execute('SELECT id FROM billing_prices')}
                prices.add(app.config['STRIPE_PRICE_ID'])
                relevant = [sub for sub in current['data'] if any(
                    item['price']['id'] in prices for item in sub['items']['data'])]
                # Use provider's current state, never the delivery order of event snapshots.
                active = [sub for sub in relevant if sub['status'] in ('active','trialing')]
                chosen = max(active or relevant, key=lambda sub:sub['created']) if relevant else None
                db().execute('UPDATE users SET subscription=?,subscription_status=?,billing_updated=? WHERE id=?',
                    (chosen['id'] if chosen else None,chosen['status'] if chosen else 'none',int(time.time()),user['id']))
                db().execute('DELETE FROM checkouts WHERE user_id=?',(user['id'],))
                audit('subscription_updated',user['id'])
            db().execute('INSERT INTO billing_events VALUES(?,?)',(event['id'],event['created']))
        return jsonify(received=True)

    @app.get('/healthz')
    def health():
        db().execute('SELECT 1').fetchone()
        return jsonify(status='ok',network='practice' if app.config['PAPER_TRADING'] else 'testnet')

    return app

