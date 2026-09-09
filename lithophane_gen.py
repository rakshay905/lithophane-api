"""
Lithophane light box STL generator.
Generates 7 parts: base, top frame, 4 columns, 4 lithophane panels.
"""

import numpy as np
from PIL import Image
from stl import mesh
import io


def _make_mesh(verts, faces):
    m = mesh.Mesh(np.zeros(len(faces), dtype=mesh.Mesh.dtype))
    for i, f in enumerate(faces):
        for j in range(3):
            m.vectors[i][j] = verts[f[j]]
    return m


def _box_mesh(x0, y0, z0, x1, y1, z1):
    """Solid box from corner (x0,y0,z0) to (x1,y1,z1)."""
    verts = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ], dtype=float)
    faces = [
        [0,2,1],[0,3,2],  # bottom
        [4,5,6],[4,6,7],  # top
        [0,1,5],[0,5,4],  # front
        [2,3,7],[2,7,6],  # back
        [1,2,6],[1,6,5],  # right
        [3,0,4],[3,4,7],  # left
    ]
    return _make_mesh(verts, faces)


def _combine(*meshes):
    data = np.concatenate([m.data for m in meshes])
    combined = mesh.Mesh(data)
    return combined


def _crop_cover(img, target_w_mm, target_h_mm, offset_x=0.0, offset_y=0.0):
    """Cover-fit crop: fill panel, no stretch. offset_x/y in [-1, 1]."""
    iw, ih = img.size
    panel_ar = target_w_mm / target_h_mm
    img_ar   = iw / ih
    if img_ar >= panel_ar:
        # Image wider → fit height, crop width
        new_w = int(ih * panel_ar)
        left  = int((iw - new_w) / 2 * (1 + offset_x))
        left  = max(0, min(iw - new_w, left))
        img   = img.crop((left, 0, left + new_w, ih))
    else:
        # Image taller → fit width, crop height
        new_h = int(iw / panel_ar)
        top   = int((ih - new_h) / 2 * (1 - offset_y))
        top   = max(0, min(ih - new_h, top))
        img   = img.crop((0, top, iw, top + new_h))
    return img


def generate_lithophane_panel(image_bytes, width_mm, height_mm,
                               min_thick=0.8, max_thick=3.0, resolution=5,
                               offset_x=0.0, offset_y=0.0,
                               border_mm=0.0, support_tabs=False):
    """
    Convert image bytes → lithophane STL bytes.
    resolution  = pixels per mm (default 3).
    border_mm   > 0 → solid frame around image (Framed mode).
    support_tabs    → add 2 snap-off stabilising tabs at bottom edge (Tabs mode).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert('L')
    img = _crop_cover(img, width_mm, height_mm, offset_x, offset_y)
    px_w = max(10, int(width_mm * resolution))
    px_h = max(10, int(height_mm * resolution))
    img = img.resize((px_w, px_h), Image.LANCZOS)
    arr = np.array(img, dtype=float)  # 0=black, 255=white

    # Light pixels → thin, dark pixels → thick (invert)
    arr = 255.0 - arr
    # Normalize to [min_thick, max_thick]
    height_map = min_thick + (arr / 255.0) * (max_thick - min_thick)

    # ── Border frame: tapered/chamfered on all 4 edges (45° self-supporting) ──
    # Each border edge linearly ramps from 0 at the outer face to max_thick
    # at the inner boundary.  For 45° self-support: border_mm >= max_thick.
    # With typical defaults (border=2, max_thick=3) the slope is steeper than
    # 45°, so it's always safe to print standing up without supports.
    if border_mm > 0:
        bpx = max(1, int(border_mm * resolution))
        bpy = max(1, int(border_mm * resolution))

        r = np.arange(px_h, dtype=float)
        c = np.arange(px_w, dtype=float)

        # Normalised distance from each edge: 0 at the very edge, 1 at the
        # inner boundary where the border meets the image area.
        d_top    = np.clip((px_h - 1 - r) / bpy, 0, 1)[:, None]
        d_bot    = np.clip(r / bpy,               0, 1)[:, None]
        d_left   = np.clip(c / bpx,               0, 1)[None, :]
        d_right  = np.clip((px_w - 1 - c) / bpx, 0, 1)[None, :]

        # Minimum distance to any edge — 0 at corner/edge, 1 inside image
        d_min = np.minimum(np.minimum(d_top, d_bot), np.minimum(d_left, d_right))

        # Tapered border thickness: rises from 0 at outer edge to max_thick
        border_h = max_thick * d_min

        # Apply only in the border zone (d_min < 1); take max so image
        # content thicker than the taper is preserved unchanged.
        in_border = (d_min < 1.0)
        height_map = np.where(in_border, np.maximum(height_map, border_h), height_map)

    dx = width_mm / px_w
    dy = height_mm / px_h

    # Build top surface quads (variable height)
    triangles = []

    for j in range(px_h):
        for i in range(px_w):
            x0, x1 = i * dx, (i + 1) * dx
            y0, y1 = j * dy, (j + 1) * dy
            z00 = height_map[j, i]
            z10 = height_map[j, i + 1] if i + 1 < px_w else z00
            z01 = height_map[j + 1, i] if j + 1 < px_h else z00
            z11 = height_map[j + 1, i + 1] if (i + 1 < px_w and j + 1 < px_h) else z00

            # Two triangles per quad (top surface)
            triangles.append([[x0, y0, z00], [x1, y0, z10], [x1, y1, z11]])
            triangles.append([[x0, y0, z00], [x1, y1, z11], [x0, y1, z01]])

    # Flat bottom face
    triangles.append([[0, 0, 0], [width_mm, 0, 0], [width_mm, height_mm, 0]])
    triangles.append([[0, 0, 0], [width_mm, height_mm, 0], [0, height_mm, 0]])

    # Side walls
    # Front (y=0)
    for i in range(px_w):
        x0, x1 = i * dx, (i + 1) * dx
        z0 = height_map[0, i]
        z1 = height_map[0, i + 1] if i + 1 < px_w else z0
        triangles.append([[x0, 0, 0], [x1, 0, 0], [x1, 0, z1]])
        triangles.append([[x0, 0, 0], [x1, 0, z1], [x0, 0, z0]])

    # Back (y=height_mm)
    for i in range(px_w):
        x0, x1 = i * dx, (i + 1) * dx
        z0 = height_map[-1, i]
        z1 = height_map[-1, i + 1] if i + 1 < px_w else z0
        triangles.append([[x1, height_mm, 0], [x0, height_mm, 0], [x0, height_mm, z0]])
        triangles.append([[x1, height_mm, 0], [x0, height_mm, z0], [x1, height_mm, z1]])

    # Left (x=0)
    for j in range(px_h):
        y0, y1 = j * dy, (j + 1) * dy
        z0 = height_map[j, 0]
        z1 = height_map[j + 1, 0] if j + 1 < px_h else z0
        triangles.append([[0, y1, 0], [0, y0, 0], [0, y0, z0]])
        triangles.append([[0, y1, 0], [0, y0, z0], [0, y1, z1]])

    # Right (x=width_mm)
    for j in range(px_h):
        y0, y1 = j * dy, (j + 1) * dy
        z0 = height_map[j, -1]
        z1 = height_map[j + 1, -1] if j + 1 < px_h else z0
        triangles.append([[width_mm, y0, 0], [width_mm, y1, 0], [width_mm, y1, z1]])
        triangles.append([[width_mm, y0, 0], [width_mm, y1, z1], [width_mm, y0, z0]])

    tri_arr = np.array(triangles, dtype=float)
    m = mesh.Mesh(np.zeros(len(tri_arr), dtype=mesh.Mesh.dtype))
    for i, t in enumerate(tri_arr):
        m.vectors[i] = t

    # ── Support tabs: 2 snap-off rectangular feet at bottom edge (Y=0) ──
    # When panel stands upright for printing, these tabs sit flat on the bed.
    # Snap/cut off after printing. Score neck (0.5mm tall) creates weak point.
    if support_tabs:
        tab_depth = 12.0   # how far tab extends below panel (–Y direction)
        tab_w     = 20.0   # tab width in X
        tab_meshes = []
        for tab_cx in [width_mm / 3, 2 * width_mm / 3]:
            x0 = max(2.0, tab_cx - tab_w / 2)
            x1 = min(width_mm - 2.0, tab_cx + tab_w / 2)
            body = _box_mesh(x0, -tab_depth, 0, x1, -0.4,      max_thick)
            neck = _box_mesh(x0, -0.4,       0, x1,  0.0,      0.5)
            tab_meshes += [body, neck]
        m = _combine(m, *tab_meshes)

    buf = io.BytesIO()
    m.save('panel.stl', fh=buf)
    return buf.getvalue()


def generate_column(panel_h_mm, panel_thick_max=3.0, col_w=12.0,
                    slot_depth=3.5, fit_tol=0.3, peg_h=4.0, peg_w=6.0):
    """
    Corner column — square cross-section with panel grooves on RIGHT and BACK faces.
    Print 4×. FL and BR use standard orientation; FR and BL mirror-X in slicer.

    Groove width  = panel_thick_max + fit_tol  (snap-fit clearance)
    Groove depth  = slot_depth (how far panel edge goes into column, default 3.5 mm)
    Bottom peg   → inserts into base socket (peg_w × peg_w × peg_h)
    Top peg      → inserts into top-frame socket (same dimensions)
    Fit tolerance = 0.3 mm by default (socket = peg_w + 0.3 mm)
    """
    import manifold3d as mf

    slot_w  = panel_thick_max + fit_tol   # groove width (e.g. 3.0 + 0.3 = 3.3 mm)
    body_h  = panel_h_mm + 1.0            # 1 mm clearance above panel for slide-in
    top_peg_h = 3.0                        # top peg height (into top-frame socket)
    pm      = (col_w - peg_w) / 2         # peg margin (centers peg within col_w)

    # ── Square body (above bottom-peg zone) ──
    col = mf.Manifold.cube([col_w, col_w, body_h])
    col = col.translate([0, 0, peg_h])

    # ── Groove on RIGHT face (x = col_w): holds front/back panel edge ──
    #    depth = slot_depth into column; width = slot_w in Y, starts at y = 0
    g_r = mf.Manifold.cube([slot_depth + 0.1, slot_w, body_h + 2])
    g_r = g_r.translate([col_w - slot_depth, 0, peg_h - 1])
    col = col - g_r

    # ── Groove on BACK face (y = col_w): holds left/right panel edge ──
    #    depth = slot_depth into column; width = slot_w in X, starts at x = 0
    g_b = mf.Manifold.cube([slot_w, slot_depth + 0.1, body_h + 2])
    g_b = g_b.translate([0, col_w - slot_depth, peg_h - 1])
    col = col - g_b

    # ── Bottom peg (inserts into base socket) ──
    bot_peg = mf.Manifold.cube([peg_w, peg_w, peg_h])
    bot_peg = bot_peg.translate([pm, pm, 0])
    col = col + bot_peg

    # ── Top peg (inserts into top-frame socket) ──
    top_peg = mf.Manifold.cube([peg_w, peg_w, top_peg_h])
    top_peg = top_peg.translate([pm, pm, peg_h + body_h])
    col = col + top_peg

    return _manifold_to_stl_bytes(col)


def _manifold_to_stl_bytes(man):
    """Convert a manifold3d Manifold object to STL bytes via numpy-stl."""
    import manifold3d as mf
    m = man.to_mesh()
    v = np.array(m.vert_properties, dtype=float)
    t = np.array(m.tri_verts, dtype=int)
    out = mesh.Mesh(np.zeros(len(t), dtype=mesh.Mesh.dtype))
    for i, tri in enumerate(t):
        out.vectors[i] = v[tri]
    buf = io.BytesIO()
    out.save('x.stl', fh=buf)
    return buf.getvalue()


def generate_base(panel_w_mm, panel_h_mm, col_w=12.0, base_thick=5.0,
                  wall_h=8.0, peg_h=4.0, peg_w=6.0, light_pos='bottom',
                  hole_circ_r=0.0, hole_rect_w=0.0, hole_rect_h=0.0):
    """
    Tray-shaped base: flat floor (base_thick) + vertical walls (wall_h) rising upward.
    Column sockets sit at the top face of the walls.
    hole_circ_r   > 0  → circular cable hole through base floor, radius in mm
    hole_rect_w/h > 0  → rectangular slot through front wall, width × height in mm
    """
    import manifold3d as mf

    box_w   = panel_w_mm + col_w * 2
    box_d   = panel_w_mm + col_w * 2
    total_h = base_thick + wall_h

    # ── Full solid tray block ──
    base = mf.Manifold.cube([box_w, box_d, total_h])

    # ── Carve inner cavity from top to form tray walls ──
    # Leave the floor fully solid — LED sits inside the tray.
    # Cable exits via hole_circ_r or hole_rect slot, not a giant floor opening.
    inner_cut = mf.Manifold.cube([box_w - col_w*2, box_d - col_w*2, wall_h + 2])
    inner_cut = inner_cut.translate([col_w, col_w, base_thick - 1])
    base = base - inner_cut

    # ── Circular cable hole through base floor (centre) ──
    if hole_circ_r > 0:
        r = max(1.0, hole_circ_r)
        cyl = mf.Manifold.cylinder(base_thick + 2, r, circular_segments=40)
        cyl = cyl.translate([box_w / 2, box_d / 2, -1])
        base = base - cyl

        # Exit groove on the BOTTOM FACE: runs from hole centre to the front edge.
        # Wire lays in this recess so the base sits flat on a shelf.
        ch_w      = r * 2          # same width as hole diameter
        groove_d  = 2.5            # 2.5 mm deep from bottom face
        groove_l  = box_d / 2 + r + 1   # from front edge (-1) to just past hole centre
        groove = mf.Manifold.cube([ch_w, groove_l, groove_d + 1])
        groove = groove.translate([(box_w - ch_w) / 2, -1, -1])
        base = base - groove

    # ── Rectangular slot through front wall (y=0 face) ──
    if hole_rect_w > 0 and hole_rect_h > 0:
        rw = min(hole_rect_w, box_w - col_w * 2 - 4)
        rh = min(hole_rect_h, total_h)
        slot = mf.Manifold.cube([rw, col_w + 2, rh])
        slot = slot.translate([(box_w - rw) / 2, -1, 0])
        base = base - slot

    # ── Corner SOCKETS at top of walls for column bottom pegs ──
    sock_w = peg_w + 0.3
    sock_d = peg_h + 0.5
    pm     = (col_w - peg_w) / 2
    corners = [
        (pm,                 pm),
        (box_w - col_w + pm, pm),
        (pm,                 box_d - col_w + pm),
        (box_w - col_w + pm, box_d - col_w + pm),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.cube([sock_w, sock_w, sock_d])
        socket = socket.translate([cx, cy, total_h - sock_d])
        base = base - socket

    return _manifold_to_stl_bytes(base)


def generate_open_top_frame(panel_w_mm, panel_h_mm, col_w=12.0, wall_h=8.0,
                             peg_w=6.0, fit_tol=0.3, lip_h=2.0):
    """
    Open-top frame: wall ring (no solid top plate) with a 2mm inner lip at the top.
    Used in 'top panel' layout — horizontal lithophane panel drops in and rests on the lip.
    Corner sockets at z=0 accept column top pegs as usual.
    """
    import manifold3d as mf

    box_w     = panel_w_mm + col_w * 2
    box_d     = panel_w_mm + col_w * 2
    top_peg_h = 3.0
    sock_w    = peg_w + fit_tol
    sock_d    = top_peg_h + 0.2
    pm        = (col_w - peg_w) / 2

    # Full solid ring wall_h tall
    frame = mf.Manifold.cube([box_w, box_d, wall_h])

    # Carve inner opening from the bottom, leaving a lip_h ledge at the very top.
    # Panel drops into opening from above and rests on this ledge.
    inner_w = box_w - col_w * 2
    inner_d = box_d - col_w * 2
    inner_cut = mf.Manifold.cube([inner_w, inner_d, wall_h - lip_h + 2])
    inner_cut = inner_cut.translate([col_w, col_w, -1])
    frame = frame - inner_cut

    # Corner sockets at z=0 (bottom face) for column top pegs
    corners = [
        (pm,                 pm),
        (box_w - col_w + pm, pm),
        (pm,                 box_d - col_w + pm),
        (box_w - col_w + pm, box_d - col_w + pm),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.cube([sock_w, sock_w, sock_d])
        socket = socket.translate([cx, cy, 0])
        frame = frame - socket

    return _manifold_to_stl_bytes(frame)


def generate_back_panel(panel_w_mm, panel_h_mm, max_thick=3.0,
                        switch_hole_d=0.0, wire_hole_r=0.0,
                        cable_slot_w=0.0, cable_slot_h=10.0):
    """
    Solid opaque back service panel — same dimensions as the lithophane panels so it
    slots into the same column grooves.  Holes are optional:
      switch_hole_d  > 0  → circular push-button hole centred at 2/3 height
      wire_hole_r    > 0  → circular cable hole at 1/4 height, centred (LED wire)
      cable_slot_w   > 0  → rectangular U-slot cut into the bottom edge for cable routing
    """
    import manifold3d as mf

    panel = mf.Manifold.cube([panel_w_mm, panel_h_mm, max_thick])

    if switch_hole_d > 0:
        r = max(3.0, switch_hole_d / 2)
        cyl = mf.Manifold.cylinder(max_thick + 2, r, circular_segments=64)
        cyl = cyl.translate([panel_w_mm / 2, panel_h_mm * 2 / 3, -1])
        panel = panel - cyl

    if wire_hole_r > 0:
        r = max(2.0, wire_hole_r)
        cyl = mf.Manifold.cylinder(max_thick + 2, r, circular_segments=40)
        cyl = cyl.translate([panel_w_mm / 2, panel_h_mm / 4, -1])
        panel = panel - cyl

    if cable_slot_w > 0:
        sw = min(cable_slot_w, panel_w_mm - 10)
        sh = min(cable_slot_h, panel_h_mm / 3)
        slot = mf.Manifold.cube([sw, sh + 2, max_thick + 2])
        slot = slot.translate([(panel_w_mm - sw) / 2, -1, -1])
        panel = panel - slot

    return _manifold_to_stl_bytes(panel)


def generate_top_frame(panel_w_mm, panel_h_mm, col_w=12.0, frame_thick=4.0,
                       wall_h=8.0, peg_w=6.0, fit_tol=0.3, switch_hole_d=0.0):
    """
    Cap-shaped top frame: solid top plate (frame_thick) + walls hanging downward (wall_h).
    Column top pegs snap into sockets at the bottom face of the hanging walls.
    Solid top plate stops light from escaping upward.
    switch_hole_d > 0 → drill a centred hole through the top plate for a push-button switch.
      Common sizes: 12 mm (standard), 16 mm (large).
    """
    import manifold3d as mf

    box_w     = panel_w_mm + col_w * 2
    box_d     = panel_w_mm + col_w * 2
    total_h   = frame_thick + wall_h
    top_peg_h = 3.0
    sock_w    = peg_w + fit_tol
    sock_d    = top_peg_h + 0.2
    pm        = (col_w - peg_w) / 2

    # z=0         : bottom of hanging walls
    # z=wall_h    : underside of solid top plate
    # z=total_h   : top face

    # ── Full solid cap block ──
    frame = mf.Manifold.cube([box_w, box_d, total_h])

    # ── Carve inner cavity from bottom to form hanging walls ──
    inner_cut = mf.Manifold.cube([box_w - col_w*2, box_d - col_w*2, wall_h + 2])
    inner_cut = inner_cut.translate([col_w, col_w, -1])
    frame = frame - inner_cut

    # ── Corner sockets at bottom of walls for column top pegs ──
    corners = [
        (pm,                 pm),
        (box_w - col_w + pm, pm),
        (pm,                 box_d - col_w + pm),
        (box_w - col_w + pm, box_d - col_w + pm),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.cube([sock_w, sock_w, sock_d])
        socket = socket.translate([cx, cy, 0])
        frame = frame - socket

    # ── Switch hole centred through solid top plate ──
    if switch_hole_d > 0:
        r = switch_hole_d / 2
        hole = mf.Manifold.cylinder(frame_thick + 2, r, circular_segments=64)
        hole = hole.translate([box_w / 2, box_d / 2, wall_h - 1])
        frame = frame - hole

    return _manifold_to_stl_bytes(frame)
