#!/usr/bin/env python3
"""
Deploy lithophane frontend to Hostinger via FTP.

Before running:
  1. Set RENDER_URL to your actual Render app URL
  2. Run: python3 deploy_hostinger.py

The script:
  - Reads index.html from templates/
  - Replaces API_BASE with the Render URL
  - Uploads to Hostinger at /public_html/apps/lithophane/index.html
"""

import ftplib, re, os

RENDER_URL   = 'https://lithophane-api.onrender.com'   # ← set after Render deploy
FTP_HOST     = '89.116.133.175'
FTP_USER     = 'u472601164.qualwiz.com'
FTP_PASS     = 'AkshayRana@123'
REMOTE_PATH  = '/apps/lithophane/index.html'            # on Hostinger (no public_html prefix)

src = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')

with open(src, 'r', encoding='utf-8') as f:
    html = f.read()

# Inject the Render URL into API_BASE
html = re.sub(
    r"const API_BASE = '';.*",
    f"const API_BASE = '{RENDER_URL}';",
    html
)

# Also strip the Flask template route (/) — not needed on static hosting
html_bytes = html.encode('utf-8')

print(f'HTML size: {len(html_bytes):,} bytes')
print(f'Uploading to {FTP_HOST}{REMOTE_PATH} ...')

import io
ftp = ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS)
ftp.set_pasv(True)

# Ensure directory exists
try:
    ftp.mkd('/apps/lithophane')
except ftplib.error_perm:
    pass

ftp.storbinary('STOR ' + REMOTE_PATH, io.BytesIO(html_bytes))
ftp.quit()

print('Done! Site live at: https://qualwiz.com/apps/lithophane/')
