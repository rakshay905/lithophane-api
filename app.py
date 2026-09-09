from flask import Flask, request, jsonify, send_file, render_template, make_response
from flask_cors import CORS
import os, io, zipfile, uuid, time, threading
from lithophane_gen import (
    generate_lithophane_panel,
    generate_column,
    generate_base,
    generate_top_frame,
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024 * 1024
CORS(app, resources={r"/*": {"origins": "*"}})

PANEL_LABELS = ['front', 'back', 'left', 'right']
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
        panel_w      = params['panel_w']
        panel_h      = params['panel_h']
        min_thick    = params['min_thick']
        max_thick    = params['max_thick']
        resolution   = params['resolution']
        col_w        = params['col_w']
        fit_tol      = params['fit_tol']
        light_pos    = params['light_pos']
        hole_circ_r  = params['hole_circ_r']
        hole_rect_w  = params['hole_rect_w']
        hole_rect_h  = params['hole_rect_h']
        switch_hole_d = params['switch_hole_d']
        border_mm    = params['border_mm']
        support_tabs = params['support_tabs']
        print_mode   = params['print_mode']

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

            _set(job_id, '🔲 Shaping top frame…', 30)
            top_stl = generate_top_frame(
                panel_w_mm=panel_w, panel_h_mm=panel_h, col_w=col_w,
                fit_tol=fit_tol, switch_hole_d=switch_hole_d,
            )
            zf.writestr('top_frame.stl', top_stl)

            panel_labels_todo = [l for l in PANEL_LABELS if l in images]
            n = len(panel_labels_todo)
            for i, label in enumerate(panel_labels_todo):
                pct = 35 + int((i / n) * 50)
                _set(job_id, f'🖼 Rendering {label} panel ({i+1}/{n})…', pct)
                ox, oy = offsets.get(label, (0, 0))
                panel_stl = generate_lithophane_panel(
                    image_bytes=images[label],
                    width_mm=panel_w, height_mm=panel_h,
                    min_thick=min_thick, max_thick=max_thick,
                    resolution=resolution, offset_x=ox, offset_y=oy,
                    border_mm=border_mm, support_tabs=support_tabs,
                )
                zf.writestr(f'panel_{label}.stl', panel_stl)

            for label in PANEL_LABELS:
                if label not in images:
                    zf.writestr(f'panel_{label}_MISSING.txt',
                                f'No image uploaded for {label} panel.')

            _set(job_id, '📦 Packing ZIP file…', 90)

            peg_w = 6.0
            instructions = f"""LITHOPHANE LIGHT BOX — ASSEMBLY GUIDE
======================================
Settings used:
  Panel size    : {panel_w:.0f} x {panel_h:.0f} mm
  Thickness     : {min_thick:.1f} mm (light areas) → {max_thick:.1f} mm (dark areas)
  Resolution    : {resolution} px/mm
  Column width  : {col_w:.0f} mm
  Fit tolerance : {fit_tol:.2f} mm
  Light source  : {light_pos}
  Panel style   : {print_mode.upper()}{f' (border {border_mm:.1f} mm)' if print_mode == 'framed' else ''}

FILES:
  base.stl        — Print 1x
  column_x4.stl   — Print 4x (see orientation below)
  top_frame.stl   — Print 1x
  panel_front/back/left/right.stl — Print 1x each

PANEL PRINTING — IMPORTANT:
  *** Print panels STANDING UPRIGHT — NOT flat ***
  Layer lines run parallel to the light = no banding, maximum detail.
  Stand each panel on its bottom edge (~{max_thick:.0f} mm wide).
  Add a 15 mm BRIM in your slicer for bed adhesion when standing.
{'  Border frame: solid outer ring stabilises the panel when standing.' if print_mode == 'framed' else ''}{'  Support tabs: snap/cut off with flush cutters after printing.' if print_mode == 'tabs' else ''}

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
  1. Base flat, sockets facing UP
  2. Press column bottom pegs into base sockets (firm push)
  3. Slide panels in from TOP of assembled columns
  4. Press top frame down over column top pegs
  5. Insert LED at the {light_pos}

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


@app.route('/generate', methods=['POST'])
def generate():
    try:
        panel_w      = float(request.form.get('panel_w', 100))
        panel_h      = float(request.form.get('panel_h', 100))
        min_thick    = float(request.form.get('min_thick', 0.8))
        max_thick    = float(request.form.get('max_thick', 3.0))
        resolution   = int(request.form.get('resolution', 3))
        col_w        = float(request.form.get('col_w', 12.0))
        fit_tol      = float(request.form.get('fit_tol', 0.3))
        light_pos    = request.form.get('light_pos', 'bottom')
        hole_circ_r  = float(request.form.get('hole_circ_r', 0))
        hole_rect_w  = float(request.form.get('hole_rect_w', 0))
        hole_rect_h  = float(request.form.get('hole_rect_h', 0))
        switch_hole_d = float(request.form.get('switch_hole_d', 0))
        print_mode   = request.form.get('print_mode', 'standard')
        border_mm    = float(request.form.get('border_mm', 2.0)) if print_mode == 'framed' else 0.0
        support_tabs = (print_mode == 'tabs')

        panel_w   = max(40, min(300, panel_w))
        panel_h   = max(40, min(300, panel_h))
        min_thick = max(0.4, min(1.5, min_thick))
        max_thick = max(2.0, min(5.0, max_thick))
        resolution = max(2, min(5, resolution))
        col_w     = max(8, min(20, col_w))
        fit_tol   = max(0.1, min(0.5, fit_tol))
        hole_circ_r  = max(0, min(15, hole_circ_r))
        hole_rect_w  = max(0, min(50, hole_rect_w))
        hole_rect_h  = max(0, min(10, hole_rect_h))
        switch_hole_d = max(0, min(25, switch_hole_d))
        border_mm = max(1.0, min(5.0, border_mm)) if print_mode == 'framed' else 0.0
        if max_thick <= min_thick:
            max_thick = min_thick + 1.5

        # Read image bytes now (request context ends after this function returns)
        images, offsets = {}, {}
        for label in PANEL_LABELS:
            f = request.files.get(f'img_{label}')
            if f and f.filename:
                images[label] = f.read()
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
