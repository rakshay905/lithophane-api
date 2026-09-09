# Deploy to Render — Sep 8
# URLs stay the same. Render replaces Pi. Cloudflare DNS switches from tunnel → Render CNAME.

## Architecture after deploy

| What | Before | After |
|------|--------|-------|
| lithophane.qualwiz.com | CF Tunnel → Pi:7788 | CNAME → Render |
| qualwiz.com/apps/lithophane/ | not exist | static HTML on Hostinger |
| ytdl.qualwiz.com | CF Tunnel → Pi:9001 | CNAME → Render |
| freetoolkit.in ytdl | calls ytdl.qualwiz.com (Pi) | calls ytdl.qualwiz.com (Render) — no change needed in PHP |

---

## LITHOPHANE

### Step 1 — Push to GitHub
```bash
cd /home/akshayrana/lithophane_site
git init
git add app.py lithophane_gen.py requirements.txt render.yaml templates/index.html
git commit -m "Lithophane API for Render"
# Create GitHub repo named: lithophane-api
git remote add origin https://github.com/YOUR_USER/lithophane-api.git
git push -u origin main
```

### Step 2 — Deploy on Render
1. render.com → New Web Service → connect lithophane-api repo
2. render.yaml is auto-detected → Deploy
3. Wait ~3 min → service live at lithophane-api.onrender.com

### Step 3 — Add custom domain on Render
1. Render dashboard → lithophane-api → Settings → Custom Domains
2. Add: `lithophane.qualwiz.com`
3. Render gives you a CNAME value (e.g. `lithophane-api.onrender.com`)

### Step 4 — Update Cloudflare DNS
1. Cloudflare dashboard → qualwiz.com → DNS
2. Find record: `lithophane` CNAME → delete it (or edit it)
3. Add new: `lithophane` CNAME → `lithophane-api.onrender.com` (grey cloud, DNS only)
4. lithophane.qualwiz.com now points to Render — URL unchanged for users

### Step 5 — Deploy frontend to Hostinger (also accessible at qualwiz.com/apps/lithophane/)
Edit deploy_hostinger.py line 14 → set RENDER_URL = 'https://lithophane.qualwiz.com'
```bash
python3 /home/akshayrana/lithophane_site/deploy_hostinger.py
```

### Step 6 — Stop Pi service
```bash
sudo systemctl stop lithophane.service && sudo systemctl disable lithophane.service
```

---

## YTDL API

### Step 1 — Push to GitHub
```bash
cd /home/akshayrana/ytdl_api
git init
git add server.py requirements.txt render.yaml
git commit -m "ytdl API for Render"
# Create GitHub repo named: ytdl-api
git remote add origin https://github.com/YOUR_USER/ytdl-api.git
git push -u origin main
```

### Step 2 — Deploy on Render
1. render.com → New Web Service → connect ytdl-api repo
2. render.yaml auto-detected → Deploy

### Step 3 — Add custom domain on Render
1. Render → ytdl-api → Settings → Custom Domains → Add: `ytdl.qualwiz.com`

### Step 4 — Update Cloudflare DNS
1. Cloudflare → qualwiz.com → DNS
2. Find `ytdl` CNAME → change to point to `ytdl-api.onrender.com` (grey cloud)
3. ytdl.qualwiz.com now hits Render — freetoolkit.in PHP needs NO changes

### Step 5 — Stop Pi service
```bash
# find the ytdl systemd service name:
systemctl list-units | grep ytdl
sudo systemctl stop <name> && sudo systemctl disable <name>
```

---

## Cloudflare config.yml — remove these 4 lines after DNS switch
File: /etc/cloudflared/config.yml

DELETE:
  - hostname: ytdl.qualwiz.com
    service: http://localhost:9001

  - hostname: lithophane.qualwiz.com
    service: http://localhost:7788

Then: sudo systemctl restart cloudflared

---

## Notes
- Render free tier sleeps after 15 min idle → ~30s cold start
  → lithophane page already pings /health on load to pre-warm
- ytdl cold start is fast (no heavy libs)
- Both use free tier. If sluggish, upgrade to Starter ($7/mo = ₹590)
- Run disk cleanup before Sep 8:
  find /home/akshayrana/news_reel_pipeline/output/ -mtime +3 -delete
  find /home/akshayrana/news_long_pipeline/output/ -mtime +3 -delete

## Tunnel entries that remain after Sep 8 (legitimate Pi-only)
  octoprint.qualwiz.com → OctoPrint 3D printer
  pi.qualwiz.com        → SSH access
  cam.qualwiz.com       → Pi camera
  scraper.qualwiz.com   → App scraper
  meshy.qualwiz.com     → Meshy pipeline server
