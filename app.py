from flask import Flask, request, jsonify, send_file, render_template, make_response
from flask_cors import CORS
import os, io, zipfile, uuid, time, threading
from lithophane_gen import (
    generate_lithophane_panel,
    generate_column,
    generate_base,
    generate_top_frame,
    generate_open_top_frame,
    generate_back_panel,
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024   # 20 MB (4 images max)
CORS(app, resources={r"/*": {"origins": "*"}})

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


def _set(job_id, step, pct, done=False, url=None, error=None):
    with JOBS_LOCK:
        JOBS[job_id] = {
            'step': step, 'pct': pct,
            'done': done, 'url': url, 'error': error,
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

        topback = (panel_layout == 'topback')
        active_labels = PANEL_LABELS_TOPBACK if topback else PANEL_LABELS

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:

            _set(job_id, '📐 Cutting column grooves…', 8)
            col_stl = generate_column(
                panel_h_mm=panel_h, panel_thick_max=max_thick,
                col_w=col_w, fit_tol=fit_tol,
            )
            zf.writestr('column_x4.stl', col_stl)

            _set(job_id, '🧱 Building base plate…', 20)
            base_stl = generate_base(
                panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                light_pos=light_pos, hole_circ_r=hole_circ_r,
                hole_rect_w=hole_rect_w, hole_rect_h=hole_rect_h,
            )
            zf.writestr('base.stl', base_stl)

            if topback:
                _set(job_id, '🔲 Shaping open top frame…', 30)
                top_stl = generate_open_top_frame(
                    panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w, fit_tol=fit_tol,
                )
                zf.writestr('top_frame_open.stl', top_stl)

                _set(job_id, '🔧 Building back service plate…', 36)
                back_stl = generate_back_panel(
                    panel_w_mm=panel_w, panel_h_mm=panel_h, max_thick=max_thick,
                    border_mm=border_mm,
                    switch_hole_d=back_switch_d, wire_hole_r=back_wire_r,
                    cable_slot_w=back_cable_w,
                )
                zf.writestr('back_plate.stl', back_stl)
            else:
                _set(job_id, '🔲 Shaping top frame…', 30)
                top_stl = generate_top_frame(
                    panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                    fit_tol=fit_tol, switch_hole_d=switch_hole_d,
                )
                zf.writestr('top_frame.stl', top_stl)

            panel_labels_todo = [l for l in active_labels if l in images]
            n = len(panel_labels_todo)
            for i, label in enumerate(panel_labels_todo):
                pct = 38 + int((i / n) * 47)
                _set(job_id, f'🖼 Rendering {label} panel ({i+1}/{n})…', pct)
                ox, oy = offsets.get(label, (0, 0))
                # Top panel is horizontal (square, same width as box interior)
                pw = panel_w if label != 'top' else panel_w
                ph = panel_h if label != 'top' else panel_w
                panel_stl = generate_lithophane_panel(
                    image_bytes=images[label],
                    width_mm=pw, height_mm=ph,
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

            peg_w = 6.0
            if topback:
                files_section = (
                    "  base.stl              — Print 1x\n"
                    "  column_x4.stl         — Print 4x (see orientation below)\n"
                    "  top_frame_open.stl    — Print 1x (open ring, no top plate)\n"
                    "  back_plate.stl        — Print 1x (solid service panel)\n"
                    "  panel_front/left/right.stl — Print 1x each (vertical, stand upright)\n"
                    f"  panel_top.stl         — Print 1x (horizontal, print flat, {panel_w:.0f}x{panel_w:.0f} mm)"
                )
                assembly_section = (
                    "  1. Base flat, sockets facing UP\n"
                    "  2. Press column bottom pegs into base sockets (firm push)\n"
                    "  3. Slide front, left, right panels in from top of columns\n"
                    "  4. Slide back_plate.stl into the back column grooves\n"
                    "  5. Press open top frame down over column top pegs\n"
                    "  6. Drop panel_top.stl flat into the open frame opening from above\n"
                    "  7. Insert LED inside the tray through the base"
                )
                back_info = (
                    f"  Back plate: solid opaque — switch hole {back_switch_d:.0f}mm, "
                    f"wire hole r={back_wire_r:.0f}mm, cable slot {back_cable_w:.0f}mm wide"
                )
            else:
                files_section = (
                    "  base.stl        — Print 1x\n"
                    "  column_x4.stl   — Print 4x (see orientation below)\n"
                    "  top_frame.stl   — Print 1x\n"
                    "  panel_front/back/left/right.stl — Print 1x each"
                )
                assembly_section = (
                    "  1. Base flat, sockets facing UP\n"
                    "  2. Press column bottom pegs into base sockets (firm push)\n"
                    "  3. Slide panels in from TOP of assembled columns\n"
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
  Grooves on RIGHT face and BACK face.
  FL corner — standard   |  BR corner — rotate 180 Z
  FR corner — mirror X   |  BL corner — mirror X
  PrusaSlicer: right-click → Mirror X
  Cura: Scale → Mirror X

HOW PARTS FIT:
  Column peg ({peg_w:.0f}x{peg_w:.0f}x4 mm) → Base socket ({peg_w+fit_tol:.1f}x{peg_w+fit_tol:.1f}x4.5 mm)
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
        _set(job_id, '✅ Done! Tap below to download.', 100,
             done=True, url=f'/dl/{uid}')

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
        ip = _client_ip()
        if not _rate_ok(ip):
            return jsonify({'error': 'Too many requests — please wait a few minutes before generating again.'}), 429

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
        border_mm     = max(1.0, min(5.0, border_mm)) if print_mode == 'framed' else 0.0
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

        if not images:
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
        )

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7788, debug=False)
