import time
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, flash)
from werkzeug.security import generate_password_hash, check_password_hash
import db, email_helpers

auth_bp = Blueprint('auth', __name__)

# ── Tier helpers ───────────────────────────────────────────────────────────────

TIER_RANK = {'anonymous': 0, 'free': 0, 'plus': 1, 'pro': 2}

# Defaults — overridden at runtime by DB settings if set
_COOLDOWN_DEFAULTS = {'anonymous': 180, 'free': 180, 'plus': 120, 'pro': 0}
_LIMIT_DEFAULTS    = {'anonymous': 3,   'free': 3,   'plus': 20,  'pro': None}

def _int_setting(key, default):
    v = db.get_setting(key)
    try: return int(v) if v else default
    except: return default

def COOLDOWN(tier=None):
    """Return cooldown dict or single value, respecting DB overrides."""
    d = {
        'anonymous': _int_setting('COOLDOWN_FREE',  _COOLDOWN_DEFAULTS['anonymous']),
        'free':      _int_setting('COOLDOWN_FREE',  _COOLDOWN_DEFAULTS['free']),
        'plus':      _int_setting('COOLDOWN_PLUS',  _COOLDOWN_DEFAULTS['plus']),
        'pro':       _int_setting('COOLDOWN_PRO',   _COOLDOWN_DEFAULTS['pro']),
    }
    return d[tier] if tier else d

def DAILY_LIMIT(tier=None):
    """Return daily limit dict or single value, respecting DB overrides."""
    pro_raw = _int_setting('LIMIT_PRO', 0)
    d = {
        'anonymous': _int_setting('LIMIT_FREE', _LIMIT_DEFAULTS['anonymous']),
        'free':      _int_setting('LIMIT_FREE', _LIMIT_DEFAULTS['free']),
        'plus':      _int_setting('LIMIT_PLUS', _LIMIT_DEFAULTS['plus']),
        'pro':       pro_raw if pro_raw > 0 else None,
    }
    return d[tier] if tier else d

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return db.get_user_by_id(uid)

def user_tier(user=None):
    if user is None:
        user = current_user()
    if user is None:
        return 'anonymous'
    sub = db.get_active_subscription(user['id'])
    if sub:
        plan = sub['plan']          # e.g. 'pro_monthly', 'plus_yearly'
        if plan.startswith('pro'):
            return 'pro'
        if plan.startswith('plus'):
            return 'plus'
    return 'free'

def get_tier_rank(tier):
    return TIER_RANK.get(tier, 0)

def cooldown_remaining(tier, user_id=None, ip=None):
    """Returns seconds remaining in cooldown, or 0 if allowed."""
    wait = COOLDOWN(tier)
    if not wait:
        return 0
    last = db.get_last_usage_time(user_id=user_id, ip=ip)
    if not last:
        return 0
    elapsed = int(time.time()) - last
    remaining = wait - elapsed
    return max(0, remaining)

def can_generate(user, ip):
    """Returns (allowed: bool, reason: str, cooldown_secs: int)."""
    tier = user_tier(user)
    uid  = user['id'] if user else None

    # Check cooldown
    cd = cooldown_remaining(tier, user_id=uid, ip=(ip if not uid else None))
    if cd > 0:
        return False, 'cooldown', cd

    # Check daily limit
    limit = DAILY_LIMIT(tier)
    if limit is not None:
        used = db.count_today_usage(user_id=uid, ip=(ip if not uid else None))
        if used >= limit:
            return False, 'daily_limit', 0

    return True, 'ok', 0

# ── Auth routes ────────────────────────────────────────────────────────────────

@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template('signup.html')
    email    = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    name     = request.form.get('name', '').strip()

    if not email or not password or len(password) < 6:
        flash('Please provide a valid email and password (min 6 chars).', 'error')
        return render_template('signup.html', email=email, name=name)

    if db.get_user_by_email(email):
        flash('An account with this email already exists.', 'error')
        return render_template('signup.html', email=email, name=name)

    pw_hash = generate_password_hash(password)
    uid     = db.create_user(email, pw_hash, name)
    token   = db.create_email_token(uid, 'verify')
    email_helpers.send_verification_email(email, token)
    return redirect(url_for('auth.email_verify_sent'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html', next=request.args.get('next', '/'))
    email    = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    next_url = request.form.get('next', '/')

    user = db.get_user_by_email(email)
    if not user or not check_password_hash(user['password_hash'], password):
        flash('Invalid email or password.', 'error')
        return render_template('login.html', email=email, next=next_url)

    if not user['is_verified']:
        flash('Please verify your email first. Check your inbox.', 'warning')
        return render_template('login.html', email=email, next=next_url)

    session.clear()
    session['user_id'] = user['id']
    session.permanent = True
    return redirect(next_url if next_url.startswith('/') else '/')


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect('/')


@auth_bp.route('/email-verify')
def email_verify_sent():
    return render_template('email_verify.html')


@auth_bp.route('/verify-email/<token>')
def verify_email(token):
    uid = db.consume_email_token(token, 'verify')
    if not uid:
        return render_template('email_verify.html', error='Link expired or already used. Please request a new one.'), 400
    db.verify_user(uid)
    session.clear()
    session['user_id'] = uid
    session.permanent = True
    return redirect('/pricing?verified=1')


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'GET':
        return render_template('forgot_password.html')
    email = request.form.get('email', '').strip().lower()
    user  = db.get_user_by_email(email)
    if user:
        token = db.create_email_token(user['id'], 'reset')
        email_helpers.send_reset_email(email, token)
    # Always show same message (don't leak whether email exists)
    return render_template('forgot_password.html', sent=True)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'GET':
        uid = db.consume_email_token.__wrapped__(token, 'reset') if hasattr(db.consume_email_token, '__wrapped__') else None
        # Just show form — validate on POST
        return render_template('reset_password.html', token=token)

    password = request.form.get('password', '')
    confirm  = request.form.get('confirm', '')
    if len(password) < 6 or password != confirm:
        flash('Passwords must match and be at least 6 characters.', 'error')
        return render_template('reset_password.html', token=token)

    uid = db.consume_email_token(token, 'reset')
    if not uid:
        flash('Reset link expired or already used. Please request a new one.', 'error')
        return render_template('reset_password.html', token=token, expired=True)

    db.update_password(uid, generate_password_hash(password))
    flash('Password updated! Please log in.', 'success')
    return redirect('/login')


@auth_bp.route('/change-password', methods=['POST'])
def change_password():
    user = current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    old_pw  = request.form.get('old_password', '')
    new_pw  = request.form.get('new_password', '')
    confirm = request.form.get('confirm', '')
    if not check_password_hash(user['password_hash'], old_pw):
        flash('Current password is incorrect.', 'error')
        return redirect('/dashboard')
    if len(new_pw) < 6 or new_pw != confirm:
        flash('New passwords must match and be at least 6 characters.', 'error')
        return redirect('/dashboard')
    db.update_password(user['id'], generate_password_hash(new_pw))
    flash('Password changed successfully.', 'success')
    return redirect('/dashboard')


@auth_bp.route('/me')
def me():
    user = current_user()
    tier = user_tier(user)
    ip   = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    uid  = user['id'] if user else None

    used  = db.count_today_usage(user_id=uid, ip=(ip if not uid else None))
    limit = DAILY_LIMIT(tier)
    cd    = cooldown_remaining(tier, user_id=uid, ip=(ip if not uid else None))
    sub   = db.get_active_subscription(uid) if uid else None

    return jsonify({
        'logged_in':    user is not None,
        'email':        user['email'] if user else None,
        'name':         user['name']  if user else None,
        'verified':     bool(user['is_verified']) if user else False,
        'tier':         tier,
        'tier_rank':    TIER_RANK.get(tier, 0),
        'plan':         sub['plan'] if sub else tier,
        'gens_today':   used,
        'daily_limit':  limit,
        'cooldown_secs': cd,
        'cooldown_total': COOLDOWN(tier),
    })
