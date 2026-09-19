import os, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import db as _db

def _cfg(key, default=''):
    """Read from DB settings first, then .env, then default."""
    return _db.get_setting(key, default)

_STYLE = """
body{margin:0;padding:0;background:#0d0b1a;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
.wrap{max-width:480px;margin:40px auto;background:#15122a;border-radius:12px;overflow:hidden;border:1px solid rgba(130,100,255,.25)}
.header{background:linear-gradient(135deg,#1a1060,#2a1880);padding:28px 32px;text-align:center}
.header h1{margin:0;font-size:22px;color:#e0d4ff;letter-spacing:.5px}
.header p{margin:6px 0 0;font-size:13px;color:#a090cc}
.body{padding:28px 32px}
.body p{color:#c0b4e8;font-size:14px;line-height:1.6;margin:0 0 16px}
.btn{display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff!important;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:600;margin:8px 0}
.footer{padding:18px 32px;border-top:1px solid rgba(130,100,255,.15);text-align:center}
.footer p{color:#5a5280;font-size:12px;margin:0}
"""

def _send(to_email: str, subject: str, html: str):
    smtp_user = _cfg('SMTP_USER')
    smtp_pass = _cfg('SMTP_PASS')
    smtp_host = _cfg('SMTP_HOST', 'smtp-relay.brevo.com')
    smtp_port = int(_cfg('SMTP_PORT', '587'))
    smtp_from = _cfg('SMTP_FROM', 'Lithophane Studio <noreply@qualwiz.com>')

    if not smtp_user or not smtp_pass or smtp_pass == 'BREVO_SMTP_KEY_HERE':
        print(f'[email] SMTP not configured — skipping send to {to_email}')
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = smtp_from
    msg['To']      = to_email
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.ehlo()
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, to_email, msg.as_string())

def send_test_email(to_email: str):
    """Send a test email to verify SMTP settings work."""
    html = f"""<!DOCTYPE html><html><head><style>{_STYLE}</style></head><body>
<div class="wrap">
  <div class="header">
    <h1>🔦 Lithophane Studio</h1>
    <p>SMTP test email</p>
  </div>
  <div class="body">
    <p>✅ SMTP is configured correctly. This is a test email from your Lithophane Studio admin panel.</p>
  </div>
  <div class="footer"><p>© Lithophane Studio · qualwiz.com</p></div>
</div></body></html>"""
    _send(to_email, 'Lithophane Studio — SMTP Test', html)

def send_verification_email(to_email: str, token: str):
    base_url = _cfg('SITE_BASE_URL', 'https://lithophane.qualwiz.com')
    link = f'{base_url}/verify-email/{token}'
    html = f"""<!DOCTYPE html><html><head><style>{_STYLE}</style></head><body>
<div class="wrap">
  <div class="header">
    <h1>🔦 Lithophane Studio</h1>
    <p>Confirm your email address</p>
  </div>
  <div class="body">
    <p>Thanks for signing up! Click the button below to verify your email and activate your account.</p>
    <p style="text-align:center"><a href="{link}" class="btn">Verify Email Address</a></p>
    <p>This link expires in <strong>24 hours</strong>. If you didn't create an account, you can safely ignore this email.</p>
    <p style="font-size:12px;color:#5a5280;word-break:break-all">Or copy this link: {link}</p>
  </div>
  <div class="footer"><p>© Lithophane Studio · qualwiz.com</p></div>
</div></body></html>"""
    _send(to_email, 'Verify your Lithophane Studio account', html)

def send_reset_email(to_email: str, token: str):
    base_url = _cfg('SITE_BASE_URL', 'https://lithophane.qualwiz.com')
    link = f'{base_url}/reset-password/{token}'
    html = f"""<!DOCTYPE html><html><head><style>{_STYLE}</style></head><body>
<div class="wrap">
  <div class="header">
    <h1>🔦 Lithophane Studio</h1>
    <p>Reset your password</p>
  </div>
  <div class="body">
    <p>We received a request to reset the password for your account.</p>
    <p style="text-align:center"><a href="{link}" class="btn">Reset Password</a></p>
    <p>This link expires in <strong>1 hour</strong>. If you didn't request a reset, no action is needed.</p>
    <p style="font-size:12px;color:#5a5280;word-break:break-all">Or copy this link: {link}</p>
  </div>
  <div class="footer"><p>© Lithophane Studio · qualwiz.com</p></div>
</div></body></html>"""
    _send(to_email, 'Reset your Lithophane Studio password', html)
