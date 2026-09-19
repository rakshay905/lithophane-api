import os, time
from functools import wraps
from flask import (Blueprint, render_template, request, redirect,
                   session, jsonify, flash)
from werkzeug.security import check_password_hash, generate_password_hash
import db, email_helpers

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

ADMIN_EMAIL    = 'er.ranaakshay@gmail.com'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')  # Set this in .env

def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
def index():
    if session.get('admin_logged_in'):
        return redirect('/admin/dashboard')
    return redirect('/admin/login')


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('admin_login.html')
    email    = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    pw_env   = ADMIN_PASSWORD
    if email == ADMIN_EMAIL and pw_env and password == pw_env:
        session['admin_logged_in'] = True
        return redirect('/admin/dashboard')
    flash('Invalid credentials.', 'error')
    return render_template('admin_login.html')


@admin_bp.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect('/admin/login')


@admin_bp.route('/dashboard')
@_admin_required
def dashboard():
    stats = db.get_usage_stats()
    recent = db.get_recent_usage(limit=20)
    open_tickets = db.count_tickets('open')
    # Quick MRR estimate: count active subs by plan
    price_map = {'plus_monthly':99,'plus_yearly':799,'pro_monthly':199,'pro_yearly':1799}
    all_subs = db.get_all_subscriptions(limit=500, status='active')
    mrr = sum(price_map.get(s['plan'], 0) for s in all_subs)
    return render_template('admin.html', page='dashboard', stats=stats, recent=recent,
                           open_tickets=open_tickets, mrr=mrr)


@admin_bp.route('/users')
@_admin_required
def users():
    page   = max(1, int(request.args.get('page', 1)))
    query  = request.args.get('q', '').strip()
    limit  = 50
    offset = (page - 1) * limit
    if query:
        rows  = db.search_users(query, limit=limit, offset=offset)
        total = db.count_search_users(query)
    else:
        rows  = db.get_all_users(limit=limit, offset=offset)
        total = db.count_users()
    return render_template('admin.html', page='users', rows=rows,
                           total=total, cur_page=page, q=query,
                           total_pages=max(1, (total + limit - 1) // limit))


@admin_bp.route('/users/<int:user_id>/override', methods=['POST'])
@_admin_required
def override(user_id):
    plan = request.form.get('plan', 'pro_monthly')
    days = int(request.form.get('days', 30))
    days = max(1, min(365, days))
    valid = ('free', 'plus_monthly', 'plus_yearly', 'pro_monthly', 'pro_yearly')
    if plan not in valid:
        flash('Invalid plan.', 'error')
        return redirect('/admin/users')
    if plan == 'free':
        db.cancel_subscription_by_user(user_id)
        flash(f'User {user_id} downgraded to Free.', 'success')
    else:
        db.override_subscription(user_id, plan, days)
        flash(f'User {user_id} granted {plan} for {days} days.', 'success')
    return redirect('/admin/users')


@admin_bp.route('/usage')
@_admin_required
def usage():
    recent = db.get_recent_usage(limit=200)
    return render_template('admin.html', page='usage', recent=recent)


# ── Settings ───────────────────────────────────────────────────────────────────

# All configurable settings with metadata
SETTINGS_SCHEMA = {
    'email': {
        'label': 'Email / SMTP',
        'icon': '📧',
        'fields': [
            {'key': 'SMTP_HOST',  'label': 'SMTP Host',        'type': 'text',     'placeholder': 'smtp-relay.brevo.com', 'hint': 'Brevo: smtp-relay.brevo.com · Gmail: smtp.gmail.com'},
            {'key': 'SMTP_PORT',  'label': 'SMTP Port',        'type': 'number',   'placeholder': '587'},
            {'key': 'SMTP_USER',  'label': 'SMTP Username',    'type': 'email',    'placeholder': 'your@email.com', 'hint': 'For Brevo: your Brevo login email'},
            {'key': 'SMTP_PASS',  'label': 'SMTP Password/Key','type': 'password', 'placeholder': '••••••••', 'hint': 'Brevo: SMTP key from dashboard.brevo.com → Settings → SMTP & API'},
            {'key': 'SMTP_FROM',  'label': 'From Address',     'type': 'text',     'placeholder': 'Lithophane Studio <noreply@qualwiz.com>'},
            {'key': 'SITE_BASE_URL', 'label': 'Site Base URL', 'type': 'text',     'placeholder': 'https://lithophane.qualwiz.com', 'hint': 'Used in email links'},
        ]
    },
    'razorpay': {
        'label': 'Razorpay',
        'icon': '💳',
        'fields': [
            {'key': 'RZP_KEY_ID',           'label': 'API Key ID',          'type': 'text',     'placeholder': 'rzp_live_...'},
            {'key': 'RZP_KEY_SECRET',        'label': 'API Key Secret',      'type': 'password', 'placeholder': '••••••••'},
            {'key': 'RZP_WEBHOOK_SECRET',    'label': 'Webhook Secret',      'type': 'password', 'placeholder': '••••••••', 'hint': 'Set after adding webhook in Razorpay dashboard → Settings → Webhooks'},
            {'key': 'RZP_PLAN_PLUS_MONTHLY', 'label': 'Plus Monthly Plan ID','type': 'text',     'placeholder': 'plan_...', 'hint': '₹99/month'},
            {'key': 'RZP_PLAN_PLUS_YEARLY',  'label': 'Plus Yearly Plan ID', 'type': 'text',     'placeholder': 'plan_...', 'hint': '₹799/year'},
            {'key': 'RZP_PLAN_PRO_MONTHLY',  'label': 'Pro Monthly Plan ID', 'type': 'text',     'placeholder': 'plan_...', 'hint': '₹199/month'},
            {'key': 'RZP_PLAN_PRO_YEARLY',   'label': 'Pro Yearly Plan ID',  'type': 'text',     'placeholder': 'plan_...', 'hint': '₹1799/year'},
        ]
    },
    'limits': {
        'label': 'Tier Limits & Cooldowns',
        'icon': '⚙️',
        'fields': [
            {'key': 'LIMIT_FREE',    'label': 'Free — Daily Limit',    'type': 'number', 'placeholder': '3',   'hint': 'Generations per day for Free/Anonymous users'},
            {'key': 'LIMIT_PLUS',    'label': 'Plus — Daily Limit',    'type': 'number', 'placeholder': '20',  'hint': 'Generations per day for Plus users'},
            {'key': 'LIMIT_PRO',     'label': 'Pro — Daily Limit',     'type': 'number', 'placeholder': '0',   'hint': 'Set 0 for unlimited'},
            {'key': 'COOLDOWN_FREE', 'label': 'Free — Cooldown (sec)', 'type': 'number', 'placeholder': '180', 'hint': '180 = 3 minutes between generations'},
            {'key': 'COOLDOWN_PLUS', 'label': 'Plus — Cooldown (sec)', 'type': 'number', 'placeholder': '120', 'hint': '120 = 2 minutes'},
            {'key': 'COOLDOWN_PRO',  'label': 'Pro — Cooldown (sec)',  'type': 'number', 'placeholder': '0',   'hint': '0 = no cooldown'},
        ]
    },
    'site': {
        'label': 'Site Settings',
        'icon': '🌐',
        'fields': [
            {'key': 'SITE_NAME',         'label': 'Site Name',            'type': 'text',     'placeholder': 'Lithophane Studio'},
            {'key': 'MAINTENANCE_MODE',  'label': 'Maintenance Mode',     'type': 'select',   'options': [('0','Off'),('1','On — show maintenance page')], 'hint': 'Blocks all non-admin access'},
            {'key': 'ANNOUNCEMENT',      'label': 'Announcement Banner',  'type': 'text',     'placeholder': 'Leave blank to hide', 'hint': 'Shown at the top of the main page'},
            {'key': 'FREE_REQUIRE_LOGIN','label': 'Require Login for Free','type': 'select',  'options': [('0','No — allow anonymous'),('1','Yes — must sign up for free tier')], 'hint': 'Forces signup before any generation'},
            {'key': 'ADMIN_EMAIL_NOTIFY','label': 'Admin Email on Signup', 'type': 'select',  'options': [('0','Disabled'),('1','Email admin on every new signup')]},
        ]
    },
    'pricing': {
        'label': 'Pricing Display',
        'icon': '💰',
        'fields': [
            {'key': 'PRICE_PLUS_MONTHLY', 'label': 'Plus Monthly Price',  'type': 'text', 'placeholder': '₹99',     'hint': 'Display only — change actual price in Razorpay dashboard'},
            {'key': 'PRICE_PLUS_YEARLY',  'label': 'Plus Yearly Price',   'type': 'text', 'placeholder': '₹799'},
            {'key': 'PRICE_PRO_MONTHLY',  'label': 'Pro Monthly Price',   'type': 'text', 'placeholder': '₹199'},
            {'key': 'PRICE_PRO_YEARLY',   'label': 'Pro Yearly Price',    'type': 'text', 'placeholder': '₹1,799'},
            {'key': 'PLUS_SAVINGS_BADGE', 'label': 'Plus Yearly Savings', 'type': 'text', 'placeholder': 'Save ~33%', 'hint': 'Text shown on yearly badge'},
            {'key': 'PRO_SAVINGS_BADGE',  'label': 'Pro Yearly Savings',  'type': 'text', 'placeholder': 'Save ~25%'},
        ]
    },
}

@admin_bp.route('/settings', methods=['GET', 'POST'])
@_admin_required
def settings():
    saved = False
    error = None
    if request.method == 'POST':
        section = request.form.get('section', '')
        if section in SETTINGS_SCHEMA:
            kv = {}
            for field in SETTINGS_SCHEMA[section]['fields']:
                val = request.form.get(field['key'], '').strip()
                # Don't overwrite password/secret fields if left blank
                if field['type'] == 'password' and not val:
                    continue
                kv[field['key']] = val
            db.bulk_save_settings(kv)
            saved = True
            flash(f'✅ {SETTINGS_SCHEMA[section]["label"]} settings saved.', 'success')
    current = db.get_all_settings()
    # Fill in env-var defaults for display
    for section_data in SETTINGS_SCHEMA.values():
        for field in section_data['fields']:
            k = field['key']
            if k not in current:
                current[k] = os.getenv(k, '')
    return render_template('admin.html', page='settings',
                           schema=SETTINGS_SCHEMA, current=current, saved=saved)


@admin_bp.route('/settings/test-email', methods=['POST'])
@_admin_required
def test_email():
    to = request.form.get('test_email_to', ADMIN_EMAIL).strip()
    try:
        email_helpers.send_test_email(to)
        flash(f'✅ Test email sent to {to}. Check your inbox.', 'success')
    except Exception as e:
        flash(f'❌ Email failed: {e}', 'error')
    return redirect('/admin/settings')


# ── Payments ───────────────────────────────────────────────────────────────────

@admin_bp.route('/payments')
@_admin_required
def payments():
    page   = max(1, int(request.args.get('page', 1)))
    status_filter = request.args.get('status', '')
    limit  = 50
    offset = (page - 1) * limit
    rows   = db.get_all_subscriptions(limit=limit, offset=offset,
                                      status=status_filter or None)
    total  = db.count_subscriptions(status=status_filter or None)
    price_map = {'plus_monthly':99,'plus_yearly':799,'pro_monthly':199,'pro_yearly':1799}
    active_rows = db.get_all_subscriptions(limit=500, status='active')
    active_revenue = sum(price_map.get(s['plan'], 0) for s in active_rows)
    total_active   = len(active_rows)
    return render_template('admin.html', page='payments', rows=rows,
                           total=total, cur_page=page,
                           total_pages=max(1, (total + limit - 1) // limit),
                           status_filter=status_filter,
                           active_revenue=active_revenue, total_active=total_active)


@admin_bp.route('/payments/<int:sub_id>/cancel', methods=['POST'])
@_admin_required
def cancel_payment(sub_id):
    db.cancel_subscription_by_id(sub_id)
    flash(f'Subscription #{sub_id} marked as cancelled.', 'success')
    return redirect('/admin/payments')


# ── Tickets ────────────────────────────────────────────────────────────────────

@admin_bp.route('/tickets')
@_admin_required
def tickets():
    status_filter = request.args.get('status', '')
    page   = max(1, int(request.args.get('page', 1)))
    limit  = 50
    offset = (page - 1) * limit
    rows   = db.get_tickets(status=status_filter or None, limit=limit, offset=offset)
    total  = db.count_tickets(status=status_filter or None)
    open_count = db.count_tickets('open')
    return render_template('admin.html', page='tickets', rows=rows,
                           total=total, cur_page=page,
                           total_pages=max(1, (total + limit - 1) // limit),
                           status_filter=status_filter, open_count=open_count)


@admin_bp.route('/tickets/<int:ticket_id>', methods=['GET', 'POST'])
@_admin_required
def ticket_detail(ticket_id):
    ticket  = db.get_ticket(ticket_id)
    if not ticket:
        flash('Ticket not found.', 'error')
        return redirect('/admin/tickets')
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'reply':
            msg = request.form.get('message', '').strip()
            if msg:
                db.add_ticket_reply(ticket_id, msg, is_admin=True)
                flash('Reply sent.', 'success')
        elif action in ('open', 'in_progress', 'closed', 'resolved'):
            db.set_ticket_status(ticket_id, action)
            flash(f'Ticket marked as {action}.', 'success')
        return redirect(f'/admin/tickets/{ticket_id}')
    replies = db.get_ticket_replies(ticket_id)
    return render_template('admin.html', page='ticket_detail',
                           ticket=ticket, replies=replies)


# ── Insights ───────────────────────────────────────────────────────────────────

@admin_bp.route('/insights')
@_admin_required
def insights():
    import json
    signups     = db.get_daily_signups(30)
    generations = db.get_daily_generations(30)
    tier_dist   = db.get_tier_distribution()
    revenue     = db.get_monthly_revenue_estimate(6)
    tool_usage  = db.get_tool_usage_breakdown()

    # Build price lookup for revenue estimates
    price_map = {
        'plus_monthly': 99, 'plus_yearly': 799,
        'pro_monthly': 199, 'pro_yearly': 1799,
    }
    revenue_by_month = {}
    for r in revenue:
        m = r['month']
        revenue_by_month[m] = revenue_by_month.get(m, 0) + price_map.get(r['plan'], 0) * r['count']

    return render_template('admin.html', page='insights',
                           signups_json=json.dumps(signups),
                           generations_json=json.dumps(generations),
                           tier_dist=tier_dist,
                           revenue_json=json.dumps([{'month': k, 'amount': v}
                                                    for k, v in sorted(revenue_by_month.items())]),
                           tool_usage=tool_usage)
