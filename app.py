from flask import Flask, request, jsonify, send_file, render_template, make_response, session
from flask_cors import CORS
from datetime import timedelta
import os, io, zipfile, uuid, time, threading
from lithophane_gen import (
    generate_lithophane_panel,
    generate_column,
    generate_base,
    generate_top_frame,
    generate_open_top_frame,
    generate_back_panel,
    generate_photo_frame,
    generate_frame_stand,
    generate_frame_integrated,
    generate_keychain,
)
import db
from auth import auth_bp, current_user, user_tier, can_generate, TIER_RANK, COOLDOWN, DAILY_LIMIT
import admin

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024   # 20 MB (4 images max)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
CORS(app, resources={r"/*": {"origins": "*"}})

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(admin.admin_bp)

# Init DB on startup
db.init_db()

import datetime as _dt

@app.template_filter('strftime')
def _strftime(ts):
    try:
        return _dt.datetime.utcfromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(ts)

@app.context_processor
def inject_user():
    u    = current_user()
    tier = user_tier(u)
    rank = TIER_RANK.get(tier, 0)
    announcement = db.get_setting('ANNOUNCEMENT') or ''
    return dict(
        current_user=u,
        user_tier=tier,
        tier_rank=rank,
        announcement=announcement,
    )


_MAINTENANCE_EXEMPT = {'/health', '/admin', '/sitemap.xml', '/robots.txt'}

@app.before_request
def check_maintenance():
    path = request.path
    if path.startswith('/admin') or path.startswith('/static') or path == '/health':
        return None
    if db.get_setting('MAINTENANCE_MODE') == '1':
        html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maintenance — Lithophane Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080810;color:#e4e4f0;font-family:-apple-system,'Segoe UI',sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:32px}
.icon{font-size:64px;margin-bottom:24px}
h1{font-size:28px;font-weight:800;background:linear-gradient(135deg,#7b68ff,#ff6fa3);
   -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px}
p{color:#9898b0;font-size:15px;max-width:420px;line-height:1.6}
.eta{margin-top:20px;background:#13131f;border:1px solid #23233a;border-radius:12px;
     padding:16px 28px;font-size:13px;color:#7b68ff}
</style></head>
<body>
  <div class="icon">🔧</div>
  <h1>Under Maintenance</h1>
  <p>We're making improvements to Lithophane Studio. We'll be back shortly.</p>
  <div class="eta">Check back soon</div>
</body></html>"""
        return make_response(html, 503)

# ── Simple in-memory rate limiter ──
# Render free tier: 1 worker, so this dict is shared across all requests.
_rl_store = {}   # ip -> [timestamp, ...]
_rl_lock  = threading.Lock()
_RL_LIMIT  = 6      # max generates per window
_RL_WINDOW = 900    # 15 minutes


def _rate_ok(ip: str) -> bool:
    now = time.time()
    with _rl_lock:
        times = [t for t in _rl_store.get(ip, []) if now - t < _RL_WINDOW]
        if len(times) >= _RL_LIMIT:
            return False
        times.append(now)
        _rl_store[ip] = times
    return True


def _client_ip() -> str:
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'

PANEL_LABELS        = ['front', 'back', 'left', 'right']
PANEL_LABELS_TOPBACK = ['front', 'top', 'left', 'right']  # 'top' replaces 'back'
DL_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DL_DIR, exist_ok=True)

# Job store: job_id -> status dict
JOBS = {}
JOBS_LOCK = threading.Lock()


def _set(job_id, step, pct, done=False, url=None, error=None, files=None):
    with JOBS_LOCK:
        JOBS[job_id] = {
            'step': step, 'pct': pct,
            'done': done, 'url': url, 'error': error,
            'files': files or [],
            'expires': time.time() + 3600,
        }


def _cleanup_old():
    now = time.time()
    # Expired jobs
    with JOBS_LOCK:
        for jid in list(JOBS):
            if JOBS[jid]['expires'] < now:
                del JOBS[jid]
    # Old download files
    for f in os.listdir(DL_DIR):
        p = os.path.join(DL_DIR, f)
        if os.path.isfile(p) and now - os.path.getmtime(p) > 3600:
            os.remove(p)


def _run_generation(job_id, params, images, offsets):
    try:
        panel_w        = params['panel_w']
        panel_h        = params['panel_h']
        min_thick      = params['min_thick']
        max_thick      = params['max_thick']
        resolution     = params['resolution']
        col_w          = params['col_w']
        fit_tol        = params['fit_tol']
        light_pos      = params['light_pos']
        hole_circ_r    = params['hole_circ_r']
        hole_rect_w    = params['hole_rect_w']
        hole_rect_h    = params['hole_rect_h']
        switch_hole_d  = params['switch_hole_d']
        border_mm      = params['border_mm']
        support_tabs   = params['support_tabs']
        print_mode     = params['print_mode']
        panel_layout   = params['panel_layout']       # '4panel' | 'topback'
        back_switch_d  = params['back_switch_d']      # topback mode: switch on back plate
        back_wire_r    = params['back_wire_r']        # topback mode: wire hole on back plate
        back_cable_w   = params['back_cable_w']       # topback mode: cable slot on back plate
        frame_only     = params.get('frame_only', False)
        image_only     = params.get('image_only', False)
        fillet_r       = params.get('fillet_r', 0.0)
        fillet_h_r     = params.get('fillet_h_r', 0.0)

        topback = (panel_layout == 'topback')
        active_labels = PANEL_LABELS_TOPBACK if topback else PANEL_LABELS

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:

            col_slot_depth = min(3.5, col_w * 0.30)

            if not image_only:
                _set(job_id, '📐 Cutting column grooves…', 8)
                col_stl = generate_column(
                    panel_h_mm=panel_h, panel_thick_max=max_thick,
                    col_w=col_w, fit_tol=fit_tol,
                    slot_depth=col_slot_depth, mirrored=False, fillet_r=fillet_r,
                )
                zf.writestr('column_fl_br_x2.stl', col_stl)
                col_mir_stl = generate_column(
                    panel_h_mm=panel_h, panel_thick_max=max_thick,
                    col_w=col_w, fit_tol=fit_tol,
                    slot_depth=col_slot_depth, mirrored=True, fillet_r=fillet_r,
                )
                zf.writestr('column_fr_bl_x2.stl', col_mir_stl)

                _set(job_id, '🧱 Building base plate…', 20)
                base_stl = generate_base(
                    panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                    light_pos=light_pos, hole_circ_r=hole_circ_r,
                    hole_rect_w=hole_rect_w, hole_rect_h=hole_rect_h,
                    fit_tol=fit_tol, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
                )
                zf.writestr('base.stl', base_stl)

                if topback:
                    _set(job_id, '🔲 Shaping open top frame…', 30)
                    top_stl = generate_open_top_frame(
                        panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                        fit_tol=fit_tol, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
                    )
                    zf.writestr('top_frame_open.stl', top_stl)

                    _set(job_id, '🔧 Building back service plate…', 36)
                    back_stl = generate_back_panel(
                        panel_w_mm=panel_w + 2*(col_slot_depth - fit_tol),
                        panel_h_mm=panel_h - 2*fit_tol,
                        max_thick=max_thick, border_mm=border_mm,
                        switch_hole_d=back_switch_d, wire_hole_r=back_wire_r,
                        cable_slot_w=back_cable_w,
                    )
                    zf.writestr('back_plate.stl', back_stl)
                else:
                    _set(job_id, '🔲 Shaping top frame…', 30)
                    top_stl = generate_top_frame(
                        panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                        fit_tol=fit_tol, switch_hole_d=switch_hole_d, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
                    )
                    zf.writestr('top_frame.stl', top_stl)
            else:
                _set(job_id, '🖼 Preparing image panels…', 20)

            if not frame_only:
                panel_labels_todo = [l for l in active_labels if l in images]
                n = len(panel_labels_todo)
                for i, label in enumerate(panel_labels_todo):
                    pct = 38 + int((i / n) * 47)
                    _set(job_id, f'🖼 Rendering {label} panel ({i+1}/{n})…', pct)
                    ox, oy = offsets.get(label, (0, 0))
                    pw = panel_w if label != 'top' else panel_w
                    ph = panel_h if label != 'top' else panel_w
                    if label == 'top':
                        w_mm = pw - 2 * fit_tol
                        h_mm = ph - 2 * fit_tol
                    else:
                        w_mm = pw + 2 * (col_slot_depth - fit_tol)
                        h_mm = ph - 2 * fit_tol
                    panel_stl = generate_lithophane_panel(
                        image_bytes=images[label],
                        width_mm=w_mm, height_mm=h_mm,
                        min_thick=min_thick, max_thick=max_thick,
                        resolution=resolution, offset_x=ox, offset_y=oy,
                        border_mm=border_mm,
                        support_tabs=(support_tabs and label != 'top'),
                    )
                    zf.writestr(f'panel_{label}.stl', panel_stl)

                for label in active_labels:
                    if label not in images:
                        desc = 'top (horizontal)' if label == 'top' else label
                        zf.writestr(f'panel_{label}_MISSING.txt',
                                    f'No image uploaded for {desc} panel.')

            _set(job_id, '📦 Packing ZIP file…', 90)

            if image_only:
                panel_list = '\n'.join(
                    f'  panel_{l}.stl' for l in active_labels if l in images
                ) or '  (no panels — no images were uploaded)'
                instructions = f"""LITHOPHANE PANELS — PRINT GUIDE
================================
Settings used:
  Panel size    : {panel_w:.0f} x {panel_h:.0f} mm
  Thickness     : {min_thick:.1f} mm (highlights) → {max_thick:.1f} mm (shadows)
  Resolution    : {resolution} px/mm
  Fit tolerance : {fit_tol:.2f} mm  (panel edge fits into column groove)

FILES:
{panel_list}

PANEL PRINTING — IMPORTANT:
  *** Print vertical panels STANDING UPRIGHT — NOT flat ***
  Layer lines run parallel to the light = no banding, maximum detail.
  Stand each panel on its bottom edge (~{max_thick:.0f} mm wide).
  Add a 15 mm BRIM in your slicer for bed adhesion when standing.

PRINT SETTINGS:
  Material     : White PLA or PETG (translucent gives brighter glow)
  Layer height : 0.10 mm for best detail
  Infill       : 100% (solid)
  No supports needed

INSTALLATION (into existing frame):
  1. Remove top frame from your light box.
  2. Slide the old panel(s) upward and out of the column slots.
  3. Slide new panel(s) downward into the column slots from the top.
  4. Re-press the top frame onto the column pegs.
"""
                zf.writestr('PANEL_PRINT_GUIDE.txt', instructions)
                zf.close()  # finalize central directory before reading buffer

                uid = uuid.uuid4().hex
                path = os.path.join(DL_DIR, uid + '.zip')
                with open(path, 'wb') as f:
                    f.write(zip_buf.getvalue())
                threading.Thread(target=_cleanup_old, daemon=True).start()
                with zipfile.ZipFile(path, 'r') as zf_read:
                    file_list = zf_read.namelist()
                _set(job_id, '✅ Done! Tap below to download.', 100,
                     done=True, url=f'/dl/{uid}', files=file_list)
                return

            peg_w = 6.0
            if topback:
                if frame_only:
                    files_section = (
                        "  base.stl              — Print 1x\n"
                        "  column_fl_br_x2.stl   — Print 2x (FL and BR corners)\n"
                        "  column_fr_bl_x2.stl   — Print 2x (FR and BL corners)\n"
                        "  top_frame_open.stl    — Print 1x\n"
                        "  back_plate.stl        — Print 1x\n"
                        "  (No image panels — print panels separately with your images)"
                    )
                else:
                    files_section = (
                        "  base.stl                   — Print 1x\n"
                        "  column_fl_br_x2.stl        — Print 2x (Front-Left and Back-Right corners)\n"
                        "  column_fr_bl_x2.stl        — Print 2x (Front-Right and Back-Left corners)\n"
                        "  top_frame_open.stl         — Print 1x (open ring, no top plate)\n"
                        "  back_plate.stl             — Print 1x (solid service panel)\n"
                        "  panel_front/left/right.stl — Print 1x each (vertical, stand upright)\n"
                        f"  panel_top.stl              — Print 1x (horizontal, print flat, {panel_w:.0f}x{panel_w:.0f} mm)"
                    )
                assembly_section = (
                    "  1. Base flat, sockets facing UP\n"
                    "  2. Press column bottom pegs into base sockets (firm push)\n"
                    "     FL corner: column_fl_br   FR corner: column_fr_bl\n"
                    "     BL corner: column_fr_bl   BR corner: column_fl_br\n"
                    "  3. Stand all panels upright inside the 4 columns\n"
                    "  4. Press open top frame down over column top pegs\n"
                    "  5. Drop panel_top.stl flat into the open frame from above\n"
                    "  6. Insert LED inside through the base"
                )
                back_info = (
                    f"  Back plate: solid opaque — switch hole {back_switch_d:.0f}mm, "
                    f"wire hole r={back_wire_r:.0f}mm, cable slot {back_cable_w:.0f}mm wide"
                )
            else:
                if frame_only:
                    files_section = (
                        "  base.stl             — Print 1x\n"
                        "  column_fl_br_x2.stl  — Print 2x (FL and BR corners)\n"
                        "  column_fr_bl_x2.stl  — Print 2x (FR and BL corners)\n"
                        "  top_frame.stl        — Print 1x\n"
                        "  (No image panels — print panels separately with your images)"
                    )
                else:
                    files_section = (
                        "  base.stl                        — Print 1x\n"
                        "  column_fl_br_x2.stl             — Print 2x (FL and BR corners)\n"
                        "  column_fr_bl_x2.stl             — Print 2x (FR and BL corners)\n"
                        "  top_frame.stl                   — Print 1x\n"
                        "  panel_front/back/left/right.stl — Print 1x each"
                    )
                assembly_section = (
                    "  1. Base flat, sockets facing UP\n"
                    "  2. Press column bottom pegs into base sockets (firm push)\n"
                    "     FL corner: column_fl_br   FR corner: column_fr_bl\n"
                    "     BL corner: column_fr_bl   BR corner: column_fl_br\n"
                    "  3. Stand all 4 panels upright and slide into column slots from the TOP\n"
                    "  4. Press top frame down over column top pegs\n"
                    f"  5. Insert LED at the {light_pos}"
                )
                back_info = ""

            instructions = f"""LITHOPHANE LIGHT BOX — ASSEMBLY GUIDE
======================================
Settings used:
  Panel size    : {panel_w:.0f} x {panel_h:.0f} mm
  Thickness     : {min_thick:.1f} mm (light areas) → {max_thick:.1f} mm (dark areas)
  Resolution    : {resolution} px/mm
  Column width  : {col_w:.0f} mm
  Fit tolerance : {fit_tol:.2f} mm
  Light source  : {light_pos}
  Layout        : {'Top Panel + 3 Sides (back = service plate)' if topback else '4 Side Panels'}
  Panel style   : {print_mode.upper()}{f' (border {border_mm:.1f} mm)' if print_mode == 'framed' else ''}
{back_info}

FILES:
{files_section}

PANEL PRINTING — IMPORTANT:
  *** Print vertical panels STANDING UPRIGHT — NOT flat ***
  Layer lines run parallel to the light = no banding, maximum detail.
  Stand each panel on its bottom edge (~{max_thick:.0f} mm wide).
  Add a 15 mm BRIM in your slicer for bed adhesion when standing.
{'  Top panel: print FLAT (lying down). It is viewed from above.' if topback else ''}{'  Border frame: solid outer ring stabilises the panel when standing.' if print_mode == 'framed' else ''}{'  Support tabs: snap/cut off with flush cutters after printing.' if print_mode == 'tabs' else ''}

PRINT SETTINGS:
  Material     : White PLA or PETG
  Layer height : 0.10 mm for panels, 0.20 mm for frame parts
  Infill       : 100% panels, 30% frame parts
  No supports needed

COLUMN ORIENTATION:
  TWO column files — print each 2x, NO slicer mirroring needed:
    column_fl_br_x2.stl → FL corner + BR corner
    column_fr_bl_x2.stl → FR corner + BL corner  (pre-mirrored in file)

  Each column has 2 panel slots (one per adjacent panel).
  Groove cross-section at each inner face = 1-0-1:
    1 = 0.5 mm outer wall  — exterior face is SOLID, clean appearance
    0 = panel slot          — panel slides in from the top during assembly
    1 = solid column body
  Panel outer face sits 0.5 mm inset from column outer face (slight recess).

HOW PARTS FIT:
  Column peg (diamond {peg_w:.0f} mm diag × 4 mm tall) → Base socket (diamond {peg_w + 2*fit_tol:.1f} mm diag × 4.5 mm deep)
  Panel edge ({max_thick:.1f} mm)            → Column groove ({max_thick+fit_tol:.1f} mm wide)
  Column top peg                             → Top frame socket

ASSEMBLY:
{assembly_section}

TIPS:
  Warm white 3000-4000K LED looks best.
  Print panels at exactly 100% scale.
  Too tight? Sand peg with 220-grit.
  Too loose? Regenerate with fit_tol={max(0.1, fit_tol-0.1):.2f}
"""
            zf.writestr('ASSEMBLY_INSTRUCTIONS.txt', instructions)

        uid = uuid.uuid4().hex
        path = os.path.join(DL_DIR, uid + '.zip')
        with open(path, 'wb') as f:
            f.write(zip_buf.getvalue())

        threading.Thread(target=_cleanup_old, daemon=True).start()
        with zipfile.ZipFile(path, 'r') as zf_read:
            file_list = zf_read.namelist()
        _set(job_id, '✅ Done! Tap below to download.', 100,
             done=True, url=f'/dl/{uid}', files=file_list)

    except Exception as e:
        import traceback
        _set(job_id, '❌ Error', 0, done=True,
             error=f'{e}\n{traceback.format_exc()}')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


_ALLOWED_IMG_MAGIC = {
    b'\xff\xd8\xff',           # JPEG
    b'\x89PNG',                # PNG
    b'GIF8',                   # GIF
    b'RIFF',                   # WEBP (starts with RIFF....WEBP)
    b'BM',                     # BMP (2 bytes)
}


def _is_valid_image(data: bytes) -> bool:
    for magic in _ALLOWED_IMG_MAGIC:
        if data[:len(magic)] == magic:
            return True
    return False


@app.route('/generate', methods=['POST'])
def generate():
    try:
        ip   = _client_ip()
        user = current_user()
        tier = user_tier(user)
        rank = TIER_RANK.get(tier, 0)

        # Hard server-protection rate limit (unchanged)
        if not _rate_ok(ip):
            return jsonify({'error': 'Too many requests — please wait a few minutes before generating again.'}), 429

        # Tier-based cooldown + daily limit
        allowed, reason, cd_secs = can_generate(user, ip)
        if not allowed:
            if reason == 'cooldown':
                cd_total = COOLDOWN(tier)
                tier_label = {'free': 'Free', 'anonymous': 'Free', 'plus': 'Plus', 'pro': 'Pro'}.get(tier, tier)
                return jsonify({
                    'error': f'Please wait {cd_secs}s before your next generation.',
                    'cooldown': cd_secs,
                    'cooldown_total': cd_total,
                    'tier': tier,
                    'tier_label': tier_label,
                    'upgrade_url': '/pricing',
                }), 429
            if reason == 'daily_limit':
                limit = DAILY_LIMIT(tier) or 3
                return jsonify({
                    'error': f'Daily limit reached ({limit}/day on {tier} plan). Upgrade for more.',
                    'daily_limit': limit,
                    'tier': tier,
                    'upgrade_url': '/pricing',
                }), 429

        panel_w        = float(request.form.get('panel_w', 100))
        panel_h        = float(request.form.get('panel_h', 100))
        min_thick      = float(request.form.get('min_thick', 0.8))
        max_thick      = float(request.form.get('max_thick', 3.0))
        resolution     = int(request.form.get('resolution', 3))
        col_w          = float(request.form.get('col_w', 12.0))
        fit_tol        = float(request.form.get('fit_tol', 0.3))
        light_pos      = request.form.get('light_pos', 'bottom')
        hole_circ_r    = float(request.form.get('hole_circ_r', 0))
        hole_rect_w    = float(request.form.get('hole_rect_w', 0))
        hole_rect_h    = float(request.form.get('hole_rect_h', 0))
        switch_hole_d  = float(request.form.get('switch_hole_d', 0))
        print_mode     = request.form.get('print_mode', 'standard')
        panel_layout   = request.form.get('panel_layout', '4panel')
        back_switch_d  = float(request.form.get('back_switch_d', 0))
        back_wire_r    = float(request.form.get('back_wire_r', 0))
        back_cable_w   = float(request.form.get('back_cable_w', 0))
        border_mm      = float(request.form.get('border_mm', 2.0)) if print_mode == 'framed' else 0.0
        support_tabs   = (print_mode == 'tabs')
        fillet_r       = float(request.form.get('fillet_r', 0))
        fillet_h_r     = float(request.form.get('fillet_h_r', 0))

        # ── Tier clamping ──
        if rank < 1:  # Free / anonymous
            panel_w = panel_h = 100.0
            resolution    = 3
            panel_layout  = '4panel'
            light_pos     = 'bottom'
            col_w         = 12.0
            fit_tol       = 0.3
            frame_only    = False
            hole_circ_r = hole_rect_w = hole_rect_h = switch_hole_d = 0.0
            fillet_r = fillet_h_r = 0.0
        elif rank < 2:  # Plus
            hole_circ_r = hole_rect_w = hole_rect_h = switch_hole_d = 0.0
            fillet_r = fillet_h_r = 0.0

        panel_w       = max(40, min(300, panel_w))
        panel_h       = max(40, min(300, panel_h))
        min_thick     = max(0.4, min(1.5, min_thick))
        max_thick     = max(2.0, min(5.0, max_thick))
        resolution    = max(2, min(5, resolution))
        col_w         = max(8, min(20, col_w))
        fit_tol       = max(0.1, min(0.5, fit_tol))
        hole_circ_r   = max(0, min(15, hole_circ_r))
        hole_rect_w   = max(0, min(50, hole_rect_w))
        hole_rect_h   = max(0, min(10, hole_rect_h))
        switch_hole_d  = max(0, min(25, switch_hole_d))
        back_switch_d  = max(0, min(25, back_switch_d))
        back_wire_r    = max(0, min(15, back_wire_r))
        back_cable_w   = max(0, min(60, back_cable_w))
        border_mm     = max(max_thick, min(5.0, border_mm)) if print_mode == 'framed' else 0.0
        fillet_r      = max(0, min(5.0, fillet_r))
        fillet_h_r    = max(0, min(5.0, fillet_h_r))
        if max_thick <= min_thick:
            max_thick = min_thick + 1.5
        if panel_layout not in ('4panel', 'topback'):
            panel_layout = '4panel'

        # Read image bytes — request context ends after this function returns
        topback = (panel_layout == 'topback')
        active_labels = PANEL_LABELS_TOPBACK if topback else PANEL_LABELS
        images, offsets = {}, {}
        for label in active_labels:
            f = request.files.get(f'img_{label}')
            if f and f.filename:
                data = f.read()
                if not _is_valid_image(data):
                    return jsonify({'error': f'File for {label} panel is not a valid image (JPEG/PNG/GIF/WEBP/BMP).'}), 400
                if len(data) > 10 * 1024 * 1024:
                    return jsonify({'error': f'{label} image exceeds 10 MB limit.'}), 400
                images[label] = data
            ox = float(request.form.get(f'ox_{label}', 0)) / 100.0
            oy = float(request.form.get(f'oy_{label}', 0)) / 100.0
            offsets[label] = (max(-1.0, min(1.0, ox)), max(-1.0, min(1.0, oy)))

        frame_only = request.form.get('frame_only', '0') == '1'
        image_only = request.form.get('image_only', '0') == '1'
        if frame_only and image_only:
            image_only = False  # frame_only takes precedence
        # Both image_only and frame_only require Plus or above
        if rank < 1:
            frame_only = False
            image_only = False

        if not frame_only and not images:
            return jsonify({'error': 'Upload at least one image.'}), 400

        params = dict(
            panel_w=panel_w, panel_h=panel_h, min_thick=min_thick,
            max_thick=max_thick, resolution=resolution, col_w=col_w,
            fit_tol=fit_tol, light_pos=light_pos, hole_circ_r=hole_circ_r,
            hole_rect_w=hole_rect_w, hole_rect_h=hole_rect_h,
            switch_hole_d=switch_hole_d, border_mm=border_mm,
            support_tabs=support_tabs, print_mode=print_mode,
            panel_layout=panel_layout, back_switch_d=back_switch_d,
            back_wire_r=back_wire_r, back_cable_w=back_cable_w,
            frame_only=frame_only, image_only=image_only,
            fillet_r=fillet_r, fillet_h_r=fillet_h_r,
        )

        # Log usage before spawning (counts against limit immediately)
        uid_for_log = user['id'] if user else None
        ip_for_log  = ip if not uid_for_log else None
        db.log_usage(uid_for_log, ip, 'lightbox')

        job_id = uuid.uuid4().hex
        _set(job_id, '⚙️ Starting generation…', 3)
        threading.Thread(
            target=_run_generation,
            args=(job_id, params, images, offsets),
            daemon=True,
        ).start()

        return jsonify({'job': job_id})

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/status/<job_id>')
def status(job_id):
    if not job_id.isalnum() or len(job_id) != 32:
        return jsonify({'error': 'invalid'}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify(job)


@app.route('/generate-part', methods=['POST'])
def generate_part_route():
    """Synchronously generate a single structural part and return the STL bytes."""
    # Pro-only feature
    _u = current_user()
    if TIER_RANK.get(user_tier(_u), 0) < 2:
        return jsonify({'error': 'Per-part download requires a Pro plan.', 'upgrade_url': '/pricing'}), 403
    try:
        part       = request.form.get('part', 'base')
        panel_w    = max(40, min(300, float(request.form.get('panel_w', 100))))
        panel_h    = max(40, min(300, float(request.form.get('panel_h', 100))))
        max_thick  = max(2.0, min(5.0, float(request.form.get('max_thick', 3.0))))
        col_w      = max(8,  min(20,  float(request.form.get('col_w', 12))))
        fit_tol    = max(0.1, min(0.5, float(request.form.get('fit_tol', 0.3))))
        switch_hole_d = max(0, min(25, float(request.form.get('switch_hole_d', 0))))
        back_switch_d = max(0, min(25, float(request.form.get('back_switch_d', 0))))
        back_wire_r   = max(0, min(15, float(request.form.get('back_wire_r', 0))))
        back_cable_w  = max(0, min(60, float(request.form.get('back_cable_w', 0))))
        light_pos  = request.form.get('light_pos', 'bottom')
        hole_circ_r   = max(0, min(15, float(request.form.get('hole_circ_r', 0))))
        hole_rect_w   = max(0, min(50, float(request.form.get('hole_rect_w', 0))))
        hole_rect_h   = max(0, min(10, float(request.form.get('hole_rect_h', 0))))
        fillet_r      = max(0, min(5.0, float(request.form.get('fillet_r', 0))))
        fillet_h_r    = max(0, min(5.0, float(request.form.get('fillet_h_r', 0))))
        col_slot_depth = min(3.5, col_w * 0.30)

        valid_parts = {'base', 'col_fl_br', 'col_fr_bl', 'top_frame', 'open_top_frame', 'back_plate'}
        if part not in valid_parts:
            return jsonify({'error': f'Unknown part: {part}'}), 400

        if part == 'base':
            stl = generate_base(
                panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                light_pos=light_pos, hole_circ_r=hole_circ_r,
                hole_rect_w=hole_rect_w, hole_rect_h=hole_rect_h,
                fit_tol=fit_tol, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
            )
            filename = 'base.stl'
        elif part == 'col_fl_br':
            stl = generate_column(
                panel_h_mm=panel_h, panel_thick_max=max_thick,
                col_w=col_w, fit_tol=fit_tol, slot_depth=col_slot_depth,
                mirrored=False, fillet_r=fillet_r,
            )
            filename = 'column_fl_br_x2.stl'
        elif part == 'col_fr_bl':
            stl = generate_column(
                panel_h_mm=panel_h, panel_thick_max=max_thick,
                col_w=col_w, fit_tol=fit_tol, slot_depth=col_slot_depth,
                mirrored=True, fillet_r=fillet_r,
            )
            filename = 'column_fr_bl_x2.stl'
        elif part == 'top_frame':
            stl = generate_top_frame(
                panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                fit_tol=fit_tol, switch_hole_d=switch_hole_d, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
            )
            filename = 'top_frame.stl'
        elif part == 'open_top_frame':
            stl = generate_open_top_frame(
                panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                fit_tol=fit_tol, fillet_r=fillet_r, fillet_h_r=fillet_h_r,
            )
            filename = 'open_top_frame.stl'
        elif part == 'back_plate':
            stl = generate_back_panel(
                panel_w_mm=panel_w + 2 * (col_slot_depth - fit_tol),
                panel_h_mm=panel_h - 2 * fit_tol,
                max_thick=max_thick,
                switch_hole_d=back_switch_d, wire_hole_r=back_wire_r,
                cable_slot_w=back_cable_w,
            )
            filename = 'back_plate.stl'

        resp = make_response(stl)
        resp.headers['Content-Type'] = 'application/octet-stream'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'detail': traceback.format_exc()}), 500


@app.route('/dl/<uid>')
def download(uid):
    if not uid.isalnum() or len(uid) != 32:
        return 'Not found', 404
    path = os.path.join(DL_DIR, uid + '.zip')
    if not os.path.isfile(path):
        return 'File expired or not found', 404
    return send_file(path, mimetype='application/zip',
                     as_attachment=True,
                     download_name='lithophane_lightbox.zip')


@app.route('/dl/<uid>/<filename>')
def download_part(uid, filename):
    import re
    if not uid.isalnum() or len(uid) != 32:
        return 'Not found', 404
    if not re.match(r'^[\w\-]+\.(stl|txt)$', filename):
        return 'Invalid filename', 400
    from auth import user_tier as _user_tier, TIER_RANK as _TIER_RANK
    u = None
    try:
        from auth import current_user as _cu
        u = _cu()
    except Exception:
        pass
    if _TIER_RANK.get(_user_tier(u), 0) < 2:
        return 'Pro plan required for individual file download', 403
    path = os.path.join(DL_DIR, uid + '.zip')
    if not os.path.isfile(path):
        return 'File expired or not found', 404
    with zipfile.ZipFile(path, 'r') as zf:
        try:
            data = zf.read(filename)
        except KeyError:
            return 'File not found in archive', 404
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=filename, mimetype='application/octet-stream')


@app.route('/frame')
def frame_page():
    resp = make_response(render_template('frame.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/generate-frame', methods=['POST'])
def generate_frame_route():
    try:
        ip = _client_ip()
        if not _rate_ok(ip):
            return jsonify({'error': 'Too many requests — wait a few minutes.'}), 429

        img_w      = max(40, min(250, float(request.form.get('img_w', 80))))
        img_h      = max(40, min(350, float(request.form.get('img_h', 100))))
        border_mm  = max(5, min(25, float(request.form.get('border_mm', 8))))
        min_thick  = max(0.4, min(1.5, float(request.form.get('min_thick', 0.8))))
        max_thick  = max(2.0, min(5.0, float(request.form.get('max_thick', 3.0))))
        resolution = max(2, min(4, int(request.form.get('resolution', 3))))
        angle_deg  = int(request.form.get('angle_deg', 90))
        if angle_deg not in (75, 90): angle_deg = 90
        if max_thick <= min_thick: max_thick = min_thick + 1.5

        f = request.files.get('image')
        if not f or not f.filename:
            return jsonify({'error': 'Upload an image.'}), 400
        img_bytes = f.read()
        if not _is_valid_image(img_bytes):
            return jsonify({'error': 'Not a valid image (JPEG/PNG/GIF/WEBP/BMP).'}), 400
        if len(img_bytes) > 10 * 1024 * 1024:
            return jsonify({'error': 'Image exceeds 10 MB.'}), 400

        job_id = uuid.uuid4().hex
        _set(job_id, '⚙️ Starting…', 3)

        def _run():
            try:
                _set(job_id, '🖼️ Generating frame + stand…', 30)
                total_w = img_w + 2 * border_mm
                total_h = img_h + 2 * border_mm
                frame_stl = generate_frame_integrated(
                    img_bytes, img_w, img_h, border_mm,
                    min_thick, max_thick, resolution, angle_deg,
                )
                _set(job_id, '📦 Packing ZIP…', 90)
                uid   = uuid.uuid4().hex
                zpath = os.path.join(DL_DIR, uid + '.zip')
                readme = (
                    f'LITHOPHANE PHOTO FRAME WITH STAND\n'
                    f'===================================\n'
                    f'Total size: {total_w:.1f} x {total_h:.1f} mm\n'
                    f'Stand angle: {angle_deg}\n\n'
                    f'FILES\n'
                    f'  frame_with_stand.stl  — ONE piece: print 1x\n\n'
                    f'PRINT SETTINGS\n'
                    f'  Filament: translucent PETG or white PLA\n'
                    f'  Layer: 0.1-0.15 mm for best detail\n'
                    f'  Walls: 2-3, Infill: 20%\n'
                    f'  Orientation: lay flat with the stand brace on the bed\n'
                    f'  Supports: light supports under the brace if needed\n\n'
                    f'USAGE\n'
                    f'  1. Set on desk — triangular brace props the frame upright.\n'
                    f'  2. Shine a phone screen or LED behind it to reveal the image.\n'
                )
                with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr('frame_with_stand.stl', frame_stl)
                    zf.writestr('README.txt', readme)
                _set(job_id, '✅ Done!', 100, done=True, url=f'/dl-frame/{uid}')
            except Exception as e:
                import traceback
                _set(job_id, '❌ Error', 0, done=True, error=f'{e}\n{traceback.format_exc()}')

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'job': job_id})

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/dl-frame/<uid>')
def dl_frame(uid):
    if not uid.isalnum() or len(uid) != 32:
        return 'Not found', 404
    path = os.path.join(DL_DIR, uid + '.zip')
    if not os.path.isfile(path):
        return 'File expired or not found', 404
    return send_file(path, mimetype='application/zip',
                     as_attachment=True,
                     download_name='litho_frame.zip')


@app.route('/keychain')
def keychain_page():
    resp = make_response(render_template('keychain.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/generate-keychain', methods=['POST'])
def generate_keychain_route():
    try:
        ip = _client_ip()
        if not _rate_ok(ip):
            return jsonify({'error': 'Too many requests — wait a few minutes.'}), 429

        shape      = request.form.get('shape', 'rectangle')
        VALID_SHAPES = ('rectangle', 'oval', 'circle', 'hexagon', 'shield',
                        'tag', 'heart', 'star', 'diamond', 'teardrop', 'arch')
        if shape not in VALID_SHAPES:
            shape = 'rectangle'
        width_mm   = max(25, min(100, float(request.form.get('width_mm', 50))))
        height_mm  = max(25, min(120, float(request.form.get('height_mm', 65))))
        border_mm  = max(2, min(12, float(request.form.get('border_mm', 4))))
        min_thick  = max(0.4, min(1.5, float(request.form.get('min_thick', 0.8))))
        max_thick  = max(2.0, min(5.0, float(request.form.get('max_thick', 3.0))))
        resolution = max(2, min(4, int(request.form.get('resolution', 2))))
        hole_d     = max(0, min(10, float(request.form.get('hole_d', 4.5))))
        if max_thick <= min_thick: max_thick = min_thick + 1.5

        f = request.files.get('image')
        if not f or not f.filename:
            return jsonify({'error': 'Upload an image.'}), 400
        img_bytes = f.read()
        if not _is_valid_image(img_bytes):
            return jsonify({'error': 'Not a valid image (JPEG/PNG/GIF/WEBP/BMP).'}), 400
        if len(img_bytes) > 10 * 1024 * 1024:
            return jsonify({'error': 'Image exceeds 10 MB.'}), 400

        job_id = uuid.uuid4().hex
        _set(job_id, '⚙️ Starting…', 3)

        def _run():
            try:
                _set(job_id, f'🔑 Generating {shape} keychain…', 30)
                stl = generate_keychain(
                    img_bytes, shape=shape,
                    width_mm=width_mm, height_mm=height_mm,
                    border_mm=border_mm, min_thick=min_thick,
                    max_thick=max_thick, resolution=resolution,
                    hole_d=hole_d,
                )
                _set(job_id, '📦 Packing…', 90)
                uid = uuid.uuid4().hex
                zpath = os.path.join(DL_DIR, uid + '.zip')
                readme = (
                    f'LITHOPHANE KEYCHAIN\n'
                    f'===================\n'
                    f'Shape: {shape}\n'
                    f'Size: {width_mm:.0f} x {height_mm:.0f} mm\n'
                    f'Thickness: {min_thick}-{max_thick} mm\n\n'
                    f'PRINT SETTINGS\n'
                    f'  Filament: translucent PETG or PLA (white/natural/clear)\n'
                    f'  Layer: 0.1-0.15 mm for best detail\n'
                    f'  Walls: 2-3\n'
                    f'  Infill: 100% (solid — thin part, no infill gap)\n'
                    f'  Supports: none needed (print flat, litho face up)\n'
                    f'  Orientation: flat on bed, smooth side down (litho side up)\n\n'
                    f'USAGE\n'
                    f'  Hold up to a light or backlit phone screen to see the image.\n'
                    f'  Attach keyring through the hole at the top.\n'
                )
                with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr('keychain.stl', stl)
                    zf.writestr('README.txt', readme)
                _set(job_id, '✅ Done!', 100, done=True, url=f'/dl-keychain/{uid}')
            except Exception as e:
                import traceback
                _set(job_id, '❌ Error', 0, done=True, error=f'{e}\n{traceback.format_exc()}')

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'job': job_id})

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/dl-keychain/<uid>')
def dl_keychain(uid):
    if not uid.isalnum() or len(uid) != 32:
        return 'Not found', 404
    path = os.path.join(DL_DIR, uid + '.zip')
    if not os.path.isfile(path):
        return 'File expired or not found', 404
    return send_file(path, mimetype='application/zip',
                     as_attachment=True,
                     download_name='litho_keychain.zip')


@app.route('/pricing')
def pricing():
    resp = make_response(render_template('pricing.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/dashboard')
def dashboard():
    user = current_user()
    if not user:
        return make_response('', 302, {'Location': '/login?next=/dashboard'})
    tier = user_tier(user)
    sub  = db.get_active_subscription(user['id'])
    ip   = _client_ip()
    used = db.count_today_usage(user_id=user['id'])
    limit = DAILY_LIMIT(tier)
    cd    = 0  # already shown via /me
    resp = make_response(render_template(
        'dashboard.html',
        user=user, tier=tier, sub=sub,
        used_today=used, daily_limit=limit,
    ))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/create-subscription', methods=['POST'])
def create_subscription():
    user = current_user()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    plan = request.json.get('plan') if request.is_json else request.form.get('plan', '')
    valid_plans = ('plus_monthly', 'plus_yearly', 'pro_monthly', 'pro_yearly')
    if plan not in valid_plans:
        return jsonify({'error': 'Invalid plan'}), 400

    plan_id_map = {
        'plus_monthly': db.get_setting('RZP_PLAN_PLUS_MONTHLY'),
        'plus_yearly':  db.get_setting('RZP_PLAN_PLUS_YEARLY'),
        'pro_monthly':  db.get_setting('RZP_PLAN_PRO_MONTHLY'),
        'pro_yearly':   db.get_setting('RZP_PLAN_PRO_YEARLY'),
    }
    rzp_plan_id = plan_id_map.get(plan, '')
    if not rzp_plan_id:
        return jsonify({'error': 'Plan not configured on server'}), 503

    try:
        import razorpay
        client = razorpay.Client(auth=(
            db.get_setting('RZP_KEY_ID'), db.get_setting('RZP_KEY_SECRET')
        ))
        sub = client.subscription.create({
            'plan_id':      rzp_plan_id,
            'total_count':  12,
            'quantity':     1,
            'notify_info': {
                'notify_phone': '',
                'notify_email': user['email'],
            },
        })
        return jsonify({'subscription_id': sub['id'], 'key': db.get_setting('RZP_KEY_ID')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    try:
        import razorpay, json
        payload   = request.get_data(as_text=True)
        signature = request.headers.get('X-Razorpay-Signature', '')
        secret    = db.get_setting('RZP_WEBHOOK_SECRET')
        if secret:
            client = razorpay.Client(auth=(
                db.get_setting('RZP_KEY_ID'), db.get_setting('RZP_KEY_SECRET')
            ))
            try:
                client.utility.verify_webhook_signature(payload, signature, secret)
            except Exception:
                return jsonify({'error': 'Invalid signature'}), 400

        data  = json.loads(payload)
        event = data.get('event', '')
        sub   = data.get('payload', {}).get('subscription', {}).get('entity', {})
        rzp_sub_id = sub.get('id', '')

        if event == 'subscription.activated':
            # Find user by email from notes or look up by sub id
            # We rely on /verify-payment for immediate activation; webhook is fallback
            pass
        elif event in ('subscription.charged',):
            # extend expiry — find sub in our DB
            existing = None
            with db._conn() as c:
                existing = c.execute(
                    "SELECT * FROM subscriptions WHERE razorpay_sub_id=?", (rzp_sub_id,)
                ).fetchone()
            if existing:
                plan = existing['plan']
                days = 365 if 'yearly' in plan else 31
                new_exp = int(time.time()) + days * 86400
                db.update_subscription_status(rzp_sub_id, 'active', new_exp)
        elif event in ('subscription.cancelled', 'subscription.expired'):
            db.update_subscription_status(rzp_sub_id, event.split('.')[1])

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/verify-payment', methods=['POST'])
def verify_payment():
    """Called by browser after Razorpay checkout succeeds."""
    user = current_user()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    try:
        import razorpay
        data        = request.json or {}
        rzp_pay_id  = data.get('razorpay_payment_id', '')
        rzp_sub_id  = data.get('razorpay_subscription_id', '')
        signature   = data.get('razorpay_signature', '')
        plan        = data.get('plan', 'pro_monthly')
        client = razorpay.Client(auth=(
            db.get_setting('RZP_KEY_ID'), db.get_setting('RZP_KEY_SECRET')
        ))
        client.utility.verify_payment_signature({
            'razorpay_payment_id':      rzp_pay_id,
            'razorpay_subscription_id': rzp_sub_id,
            'razorpay_signature':       signature,
        })
        days = 365 if 'yearly' in plan else 31
        expires_at = int(time.time()) + days * 86400
        db.create_subscription(
            user['id'], plan,
            razorpay_sub_id=rzp_sub_id,
            razorpay_payment_id=rzp_pay_id,
            expires_at=expires_at,
        )
        return jsonify({'status': 'ok', 'plan': plan})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/cancel-subscription', methods=['POST'])
def cancel_subscription():
    user = current_user()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    try:
        sub = db.get_active_subscription(user['id'])
        if sub and sub['razorpay_sub_id']:
            import razorpay
            client = razorpay.Client(auth=(
                db.get_setting('RZP_KEY_ID'), db.get_setting('RZP_KEY_SECRET')
            ))
            try:
                client.subscription.cancel(sub['razorpay_sub_id'], {'cancel_at_cycle_end': 1})
            except Exception:
                pass  # Still mark cancelled in our DB
        db.cancel_subscription_by_user(user['id'])
        return jsonify({'status': 'cancelled'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/submit-ticket', methods=['POST'])
def submit_ticket():
    user = current_user()
    data = request.get_json(silent=True) or {}
    email   = (data.get('email') or (user['email'] if user else '')).strip()
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    if not email or not subject or not message:
        return jsonify({'error': 'All fields are required'}), 400
    if len(subject) > 200 or len(message) > 5000:
        return jsonify({'error': 'Input too long'}), 400
    ticket_id = db.create_ticket(
        email=email,
        subject=subject,
        message=message,
        user_id=user['id'] if user else None,
    )
    return jsonify({'status': 'ok', 'ticket_id': ticket_id})


@app.route('/sitemap.xml')
def sitemap():
    base = (db.get_setting('SITE_BASE_URL') or 'https://lithophane.qualwiz.com').rstrip('/')
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><priority>1.0</priority><changefreq>weekly</changefreq></url>
  <url><loc>{base}/pricing</loc><priority>0.9</priority><changefreq>monthly</changefreq></url>
</urlset>'''
    resp = make_response(xml)
    resp.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return resp


@app.route('/robots.txt')
def robots():
    base = (db.get_setting('SITE_BASE_URL') or 'https://lithophane.qualwiz.com').rstrip('/')
    txt = f'User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /downloads/\nSitemap: {base}/sitemap.xml\n'
    resp = make_response(txt)
    resp.headers['Content-Type'] = 'text/plain'
    return resp


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7788, debug=False)
