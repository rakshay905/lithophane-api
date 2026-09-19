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

    # ── Border frame: raised solid frame with 45° chamfer at inner edge ──
    # Profile from outer edge inward:
    #   flat zone: max_thick all the way (solid opaque frame)
    #   taper zone (inner max_thick mm): slopes from max_thick down to min_thick
    # Border pixels are FORCED (not max'd) so the frame is always raised above the image.
    if border_mm > 0:
        bpx = max(1, int(border_mm * resolution))
        bpy = max(1, int(border_mm * resolution))

        r = np.arange(px_h, dtype=float)
        c = np.arange(px_w, dtype=float)

        d_top   = np.clip((px_h - 1 - r) / bpy, 0, 1)[:, None]
        d_bot   = np.clip(r / bpy,               0, 1)[:, None]
        d_left  = np.clip(c / bpx,               0, 1)[None, :]
        d_right = np.clip((px_w - 1 - c) / bpx, 0, 1)[None, :]
        # d_min: 0 at outer edge, 1 at inner image boundary
        d_min   = np.minimum(np.minimum(d_top, d_bot), np.minimum(d_left, d_right))

        # flat zone = outer (border_mm - max_thick) mm, taper zone = inner max_thick mm
        flat_ratio  = max(0.0, (border_mm - max_thick) / border_mm)
        taper_denom = max(1e-6, 1.0 - flat_ratio)

        # Taper drops from max_thick (at taper start) to min_thick (at image boundary)
        border_h = np.where(
            d_min < flat_ratio,
            max_thick,
            max_thick - (max_thick - min_thick) * (d_min - flat_ratio) / taper_denom
        )

        # FORCE: override image pixels inside border zone so frame is always raised
        in_border = (d_min < 1.0)
        height_map = np.where(in_border, border_h, height_map)

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
                    slot_depth=None, fit_tol=0.3, peg_h=4.0, peg_w=6.0,
                    outer_wall=0.5, mirrored=False, fillet_r=0.0):
    """
    Corner column — RIGHT and BACK face grooves with a thin outer wall (101 pattern).

    outer_wall (0.5 mm) = thin solid wall between the groove and each exterior face.
    Result: ALL exterior column faces are solid (111). No visible openings from outside.
    Groove opens only at the two interior-facing faces (1-0-1 pattern there).
    Panel slides in from the top; outer_wall does not block vertical entry.
    Panel outer face sits outer_wall mm inset from the column outer face (slight recess).

    mirrored=False → FL + BR positions (grooves on RIGHT and BACK inner faces)
    mirrored=True  → FR + BL positions (grooves on LEFT and BACK inner faces, pre-mirrored)
    Generate both STLs in code — no slicer mirroring needed by the user.

    Groove width = panel_thick_max + fit_tol
    Groove depth = min(3.5, col_w × 0.30)
    Panel width  = inner_span + 2*(slot_depth − fit_tol)
    """
    import manifold3d as mf

    if slot_depth is None:
        slot_depth = min(3.5, col_w * 0.30)

    slot_w    = panel_thick_max + fit_tol
    body_h    = panel_h_mm + 1.0
    top_peg_h = 3.0
    pm        = (col_w - peg_w) / 2

    # ── Square body (optionally with rounded outer edges) ──
    if fillet_r > 0.05:
        col = mf.Manifold.extrude(_rounded_rect_cs(col_w, col_w, fillet_r), body_h)
    else:
        col = mf.Manifold.cube([col_w, col_w, body_h])
    col = col.translate([0, 0, peg_h])

    # ── RIGHT groove ──
    g_r = mf.Manifold.cube([slot_depth + 0.1, slot_w, body_h + 2])
    g_r = g_r.translate([col_w - slot_depth, outer_wall, peg_h - 1])
    col = col - g_r

    # ── BACK groove ──
    g_b = mf.Manifold.cube([slot_w, slot_depth + 0.1, body_h + 2])
    g_b = g_b.translate([outer_wall, col_w - slot_depth, peg_h - 1])
    col = col - g_b

    # ── Diamond pegs (45°-rotated square) — all 4 faces at 45° when printed flat ──
    # half-diagonal = peg_w/2 → peg still clears column walls by pm on each side
    cs_peg = _diamond_cs(peg_w / 2)

    bot_peg = mf.Manifold.extrude(cs_peg, peg_h)
    bot_peg = bot_peg.translate([col_w / 2, col_w / 2, 0])
    col = col + bot_peg

    top_peg = mf.Manifold.extrude(cs_peg, top_peg_h)
    top_peg = top_peg.translate([col_w / 2, col_w / 2, peg_h + body_h])
    col = col + top_peg

    if mirrored:
        # Mirror on X axis (x → col_w − x): grooves move to LEFT+BACK inner faces
        # for FR and BL corner positions. All exterior faces remain solid.
        col = col.scale([-1, 1, 1]).translate([col_w, 0, 0])

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


def _diamond_cs(half_diag):
    """Diamond cross-section (square rotated 45°) centered at origin, half-diagonal = half_diag.
    All 4 faces are at 45° to the axes → prints support-free when column laid flat."""
    import manifold3d as mf
    side = half_diag * (2 ** 0.5)
    return mf.CrossSection.square([side, side], center=True).rotate(45)


def _rounded_rect_cs(w, d, r, segs=10):
    """Rounded rectangle cross-section (CCW winding) for extruding filleted boxes.
    r is clamped to fit safely inside the rectangle."""
    import manifold3d as mf, math
    r = min(float(r), w / 2 * 0.95, d / 2 * 0.95)
    if r <= 0.05:
        return mf.CrossSection.square([w, d])
    pts = []
    # Four corners in CCW order: BR → TR → TL → BL
    corners = [
        (w - r, r,     -math.pi / 2, 0),
        (w - r, d - r,  0,           math.pi / 2),
        (r,     d - r,  math.pi / 2, math.pi),
        (r,     r,      math.pi,     3 * math.pi / 2),
    ]
    for cx, cy, a0, a1 in corners:
        for i in range(segs + 1):
            a = a0 + i * (a1 - a0) / segs
            pts.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    return mf.CrossSection([pts])


def _rounded_prism(w, d, h, r_v, r_h, segs=6):
    """Rectangular prism with vertical corner fillet r_v AND top/bottom horizontal rim fillet r_h.
    Cross-section occupies (0,0)→(w,d), height 0→h — same footprint as Manifold.cube([w,d,h])."""
    import manifold3d as mf, math
    r_h = max(0.0, min(float(r_h), h * 0.40, w / 2 * 0.90, d / 2 * 0.90))
    if r_h <= 0.05:
        return mf.Manifold.extrude(_rounded_rect_cs(w, d, r_v), h) if r_v > 0.05 else mf.Manifold.cube([w, d, h])
    cs_full = _rounded_rect_cs(w, d, r_v)
    main_h = h - 2.0 * r_h
    body = mf.Manifold.extrude(cs_full, main_h).translate([0, 0, r_h]) if main_h > 0.001 else None
    slice_h = r_h / segs
    for i in range(segs):
        # Top cap: inset at top of slice (t=(i+1)/segs) — inset(t) = r_h*(1-sqrt(1-t²))
        t_top = (i + 1) / segs
        ins_t = r_h * (1.0 - math.sqrt(max(0.0, 1.0 - t_top * t_top)))
        s_top = mf.Manifold.extrude(
            _rounded_rect_cs(max(0.1, w - 2*ins_t), max(0.1, d - 2*ins_t), max(0, r_v - ins_t)),
            slice_h,
        ).translate([ins_t, ins_t, h - r_h + i * slice_h])
        body = s_top if body is None else (body + s_top)
        # Bottom cap: inset at bottom of slice (t'=i/segs) — inset(t') = r_h*(1-sqrt(1-(1-t')²))
        t_bot = i / segs
        ins_b = r_h * (1.0 - math.sqrt(max(0.0, 1.0 - (1.0 - t_bot) ** 2)))
        s_bot = mf.Manifold.extrude(
            _rounded_rect_cs(max(0.1, w - 2*ins_b), max(0.1, d - 2*ins_b), max(0, r_v - ins_b)),
            slice_h,
        ).translate([ins_b, ins_b, i * slice_h])
        body = body + s_bot
    return body


def generate_base(panel_w_mm, panel_h_mm, col_w=12.0, base_thick=5.0,
                  wall_h=8.0, peg_h=4.0, peg_w=6.0, light_pos='bottom',
                  hole_circ_r=0.0, hole_rect_w=0.0, hole_rect_h=0.0, fit_tol=0.3, fillet_r=0.0, fillet_h_r=0.0):
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

    # ── Full solid tray block (optionally rounded outer vertical edges) ──
    if fillet_h_r > 0.05:
        base = _rounded_prism(box_w, box_d, total_h, fillet_r, fillet_h_r)
    elif fillet_r > 0.05:
        base = mf.Manifold.extrude(_rounded_rect_cs(box_w, box_d, fillet_r), total_h)
    else:
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

    # ── Corner SOCKETS at top of walls for column bottom pegs (diamond shape) ──
    sock_d   = peg_h + 0.5
    cs_sock  = _diamond_cs(peg_w / 2 + fit_tol)
    half_cw  = col_w / 2
    corners  = [
        (half_cw,          half_cw),
        (box_w - half_cw,  half_cw),
        (half_cw,          box_d - half_cw),
        (box_w - half_cw,  box_d - half_cw),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.extrude(cs_sock, sock_d)
        socket = socket.translate([cx, cy, total_h - sock_d])
        base = base - socket

    return _manifold_to_stl_bytes(base)


def generate_open_top_frame(panel_w_mm, panel_h_mm, col_w=12.0, wall_h=8.0,
                             peg_w=6.0, fit_tol=0.3, lip_h=2.0, fillet_r=0.0, fillet_h_r=0.0):
    """
    Open-top frame: wall ring (no solid top plate) with a 2mm inner lip at the top.
    Used in 'top panel' layout — horizontal lithophane panel drops in and rests on the lip.
    Corner sockets at z=0 accept column top pegs as usual.
    """
    import manifold3d as mf

    box_w     = panel_w_mm + col_w * 2
    box_d     = panel_w_mm + col_w * 2
    top_peg_h = 3.0
    sock_d    = top_peg_h + 0.2

    if fillet_h_r > 0.05:
        frame = _rounded_prism(box_w, box_d, wall_h, fillet_r, fillet_h_r)
    elif fillet_r > 0.05:
        frame = mf.Manifold.extrude(_rounded_rect_cs(box_w, box_d, fillet_r), wall_h)
    else:
        frame = mf.Manifold.cube([box_w, box_d, wall_h])

    # Carve inner opening from the bottom, leaving a lip_h ledge at the very top.
    # Panel drops into opening from above and rests on this ledge.
    inner_w = box_w - col_w * 2
    inner_d = box_d - col_w * 2
    inner_cut = mf.Manifold.cube([inner_w, inner_d, wall_h - lip_h + 2])
    inner_cut = inner_cut.translate([col_w, col_w, -1])
    frame = frame - inner_cut

    # Corner sockets at z=0 (bottom face) for column top pegs (diamond shape)
    cs_sock = _diamond_cs(peg_w / 2 + fit_tol)
    half_cw = col_w / 2
    corners = [
        (half_cw,          half_cw),
        (box_w - half_cw,  half_cw),
        (half_cw,          box_d - half_cw),
        (box_w - half_cw,  box_d - half_cw),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.extrude(cs_sock, sock_d)
        socket = socket.translate([cx, cy, 0])
        frame = frame - socket

    return _manifold_to_stl_bytes(frame)


def generate_back_panel(panel_w_mm, panel_h_mm, max_thick=3.0,
                        border_mm=0.0,
                        switch_hole_d=0.0, wire_hole_r=0.0,
                        cable_slot_w=0.0, cable_slot_h=10.0):
    """
    Solid opaque back service panel — same dimensions as the lithophane panels so it
    slots into the same column grooves.  Holes are optional:
      switch_hole_d  > 0  → circular push-button hole centred at 2/3 height
      wire_hole_r    > 0  → circular cable hole at 1/4 height, centred (LED wire)
      cable_slot_w   > 0  → rectangular U-slot cut into the bottom edge for cable routing
      border_mm      > 0  → decorative frame border matching lithophane panel style
    """
    import manifold3d as mf

    panel = mf.Manifold.cube([panel_w_mm, panel_h_mm, max_thick])

    # Frame border: recess the center face to create a raised perimeter ring
    if border_mm > 0:
        recess = min(max_thick * 0.4, 1.2)
        iw = panel_w_mm - 2 * border_mm
        ih = panel_h_mm - 2 * border_mm
        if iw > 4 and ih > 4:
            cut = mf.Manifold.cube([iw, ih, recess + 1])
            cut = cut.translate([border_mm, border_mm, max_thick - recess])
            panel = panel - cut

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
                       wall_h=8.0, peg_w=6.0, fit_tol=0.3, switch_hole_d=0.0, fillet_r=0.0, fillet_h_r=0.0):
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
    sock_d    = top_peg_h + 0.2

    # z=0         : bottom of hanging walls
    # z=wall_h    : underside of solid top plate
    # z=total_h   : top face

    if fillet_h_r > 0.05:
        frame = _rounded_prism(box_w, box_d, total_h, fillet_r, fillet_h_r)
    elif fillet_r > 0.05:
        frame = mf.Manifold.extrude(_rounded_rect_cs(box_w, box_d, fillet_r), total_h)
    else:
        frame = mf.Manifold.cube([box_w, box_d, total_h])

    # ── Carve inner cavity from bottom to form hanging walls ──
    inner_cut = mf.Manifold.cube([box_w - col_w*2, box_d - col_w*2, wall_h + 2])
    inner_cut = inner_cut.translate([col_w, col_w, -1])
    frame = frame - inner_cut

    # ── Corner sockets at bottom of walls for column top pegs (diamond shape) ──
    cs_sock = _diamond_cs(peg_w / 2 + fit_tol)
    half_cw = col_w / 2
    corners = [
        (half_cw,          half_cw),
        (box_w - half_cw,  half_cw),
        (half_cw,          box_d - half_cw),
        (box_w - half_cw,  box_d - half_cw),
    ]
    for cx, cy in corners:
        socket = mf.Manifold.extrude(cs_sock, sock_d)
        socket = socket.translate([cx, cy, 0])
        frame = frame - socket

    # ── Switch hole centred through solid top plate ──
    if switch_hole_d > 0:
        r = switch_hole_d / 2
        hole = mf.Manifold.cylinder(frame_thick + 2, r, circular_segments=64)
        hole = hole.translate([box_w / 2, box_d / 2, wall_h - 1])
        frame = frame - hole

    return _manifold_to_stl_bytes(frame)


# ═══════════════════════════════════════════════════════════════════════
#  PHOTO FRAME + KEYCHAIN TOOLS
# ═══════════════════════════════════════════════════════════════════════

def _compute_height_map(image_bytes, width_mm, height_mm, min_thick=0.8, max_thick=3.0,
                         resolution=3, border_mm=0.0, offset_x=0.0, offset_y=0.0):
    """Extract height map (px_h × px_w) from image. Shared by frame and keychain."""
    img = Image.open(io.BytesIO(image_bytes)).convert('L')
    img = _crop_cover(img, width_mm, height_mm, offset_x, offset_y)
    px_w = max(10, int(width_mm * resolution))
    px_h = max(10, int(height_mm * resolution))
    img  = img.resize((px_w, px_h), Image.LANCZOS)
    arr  = np.array(img, dtype=float)
    arr  = 255.0 - arr
    hmap = min_thick + (arr / 255.0) * (max_thick - min_thick)

    if border_mm > 0:
        bpx = max(1, int(border_mm * resolution))
        bpy = max(1, int(border_mm * resolution))
        r   = np.arange(px_h, dtype=float)
        c   = np.arange(px_w, dtype=float)
        d_top   = np.clip((px_h - 1 - r) / bpy, 0, 1)[:, None]
        d_bot   = np.clip(r / bpy,               0, 1)[:, None]
        d_left  = np.clip(c / bpx,               0, 1)[None, :]
        d_right = np.clip((px_w - 1 - c) / bpx, 0, 1)[None, :]
        d_min   = np.minimum(np.minimum(d_top, d_bot), np.minimum(d_left, d_right))
        flat_r  = max(0.0, (border_mm - max_thick) / border_mm)
        t_denom = max(1e-6, 1.0 - flat_r)
        border_h = np.where(
            d_min < flat_r,
            max_thick,
            max_thick - (max_thick - min_thick) * (d_min - flat_r) / t_denom
        )
        hmap = np.where(d_min < 1.0, border_h, hmap)

    return hmap, px_w, px_h


def _build_panel_manifold(height_map, width_mm, height_mm):
    """Build a watertight manifold3d solid from a 2D height map (px_h × px_w array)."""
    import manifold3d as mf

    px_h, px_w = height_map.shape
    dx    = width_mm  / px_w
    dy    = height_mm / px_h
    cols  = px_w + 1
    rows  = px_h + 1
    n_top = cols * rows

    def top_idx(i, j): return j * cols + i
    def bot_idx(i, j): return n_top + j * cols + i

    top_v = np.zeros((rows * cols, 3), dtype=np.float32)
    for jv in range(rows):
        for iv in range(cols):
            z = height_map[min(jv, px_h - 1), min(iv, px_w - 1)]
            top_v[top_idx(iv, jv)] = [iv * dx, jv * dy, z]

    bot_v      = top_v.copy()
    bot_v[:, 2] = 0.0
    all_v      = np.concatenate([top_v, bot_v], axis=0)
    tris       = []

    # Top face: CCW from +Z
    for j in range(px_h):
        for i in range(px_w):
            v00, v10 = top_idx(i, j),   top_idx(i+1, j)
            v01, v11 = top_idx(i, j+1), top_idx(i+1, j+1)
            tris += [[v00, v10, v11], [v00, v11, v01]]

    # Bottom face: CW from +Z (CCW from -Z)
    for j in range(px_h):
        for i in range(px_w):
            v00, v10 = bot_idx(i, j),   bot_idx(i+1, j)
            v01, v11 = bot_idx(i, j+1), bot_idx(i+1, j+1)
            tris += [[v00, v11, v10], [v00, v01, v11]]

    # Front wall (j=0, normal=-Y)
    for i in range(px_w):
        vt0, vt1 = top_idx(i, 0),    top_idx(i+1, 0)
        vb0, vb1 = bot_idx(i, 0),    bot_idx(i+1, 0)
        tris += [[vb0, vb1, vt1], [vb0, vt1, vt0]]

    # Back wall (j=px_h, normal=+Y)
    for i in range(px_w):
        vt0, vt1 = top_idx(i, px_h),   top_idx(i+1, px_h)
        vb0, vb1 = bot_idx(i, px_h),   bot_idx(i+1, px_h)
        tris += [[vb1, vb0, vt0], [vb1, vt0, vt1]]

    # Left wall (i=0, normal=-X)
    for j in range(px_h):
        vt0, vt1 = top_idx(0, j),   top_idx(0, j+1)
        vb0, vb1 = bot_idx(0, j),   bot_idx(0, j+1)
        tris += [[vb1, vb0, vt0], [vb1, vt0, vt1]]

    # Right wall (i=px_w, normal=+X)
    for j in range(px_h):
        vt0, vt1 = top_idx(px_w, j),   top_idx(px_w, j+1)
        vb0, vb1 = bot_idx(px_w, j),   bot_idx(px_w, j+1)
        tris += [[vb0, vb1, vt1], [vb0, vt1, vt0]]

    tri_arr  = np.array(tris, dtype=np.uint32)
    mesh_obj = mf.Mesh(vert_properties=all_v, tri_verts=tri_arr)
    return mf.Manifold(mesh_obj)


def _keychain_shape_cs(shape, width_mm, height_mm):
    """CrossSection for keychain shape, centred at origin. All polygons CCW."""
    import manifold3d as mf, math

    hw, hh = width_mm / 2, height_mm / 2

    def arc_pts(cx, cy, r, a0, a1, n=10):
        return [(cx + r * math.cos(math.radians(a0 + (a1-a0)*i/(n-1))),
                 cy + r * math.sin(math.radians(a0 + (a1-a0)*i/(n-1))))
                for i in range(n)]

    if shape in ('oval', 'circle'):
        r  = max(hw, hh)
        cs = mf.CrossSection.circle(r, 64)
        if shape == 'oval' and abs(hw - hh) > 0.5:
            cs = cs.scale([hw / r, hh / r])
        elif shape == 'circle':
            rr = min(hw, hh)
            cs = mf.CrossSection.circle(rr, 64)
        return cs

    if shape == 'hexagon':
        pts = [(hw * math.cos(math.radians(30 + 60*i)),
                hh * math.sin(math.radians(30 + 60*i)))
               for i in range(6)]
        return mf.CrossSection([pts])

    if shape == 'shield':
        pts = [(-hw, 0.0), (0.0, -hh), (hw, 0.0), (hw, hh), (-hw, hh)]
        return mf.CrossSection([pts])

    if shape == 'tag':
        r_c = min(4.0, min(hw, hh) * 0.25)
        n_w = min(6.0, hw * 0.35)
        n_h = min(8.0, hh * 0.20)
        pts  = []
        pts += arc_pts(-hw + r_c, -hh + r_c, r_c, 180, 270)
        pts += arc_pts( hw - r_c, -hh + r_c, r_c, 270, 360)
        pts += arc_pts( hw - r_c,  hh - r_c, r_c,   0,  90)
        pts += [( n_w, hh), ( n_w, hh - n_h)]
        pts += arc_pts(0, hh - n_h, n_w, 0, 180, 12)
        pts += [(-n_w, hh - n_h), (-n_w, hh)]
        pts += arc_pts(-hw + r_c,  hh - r_c, r_c,  90, 180)
        return mf.CrossSection([pts])

    if shape == 'heart':
        # Parametric heart — traces CW, so reverse for CCW
        n = 80
        raw = []
        for i in range(n):
            t = 2 * math.pi * i / n
            x = 16 * math.sin(t) ** 3
            y = 13*math.cos(t) - 5*math.cos(2*t) - 2*math.cos(3*t) - math.cos(4*t)
            raw.append((x, y))
        xs = [p[0] for p in raw]; ys = [p[1] for p in raw]
        xr = max(xs) - min(xs); yr = max(ys) - min(ys)
        cx = (max(xs) + min(xs)) / 2; cy = (max(ys) + min(ys)) / 2
        sx = (2*hw) / xr; sy = (2*hh) / yr
        pts = [((p[0]-cx)*sx, (p[1]-cy)*sy) for p in raw]
        pts.reverse()
        return mf.CrossSection([pts])

    if shape == 'star':
        # 5-point star. Outer at 90°+72k, inner at 126°+72k. CCW (verified via shoelace).
        outer_r = min(hw, hh)
        inner_r = outer_r * 0.45
        pts = []
        for i in range(10):
            angle = math.radians(90 + 36 * i)
            r = outer_r if i % 2 == 0 else inner_r
            pts.append((r * math.cos(angle) * hw / outer_r,
                        r * math.sin(angle) * hh / outer_r))
        return mf.CrossSection([pts])

    if shape == 'diamond':
        # 4-point diamond (rotated square). CCW: left, bottom, right, top.
        pts = [(-hw, 0), (0, -hh), (hw, 0), (0, hh)]
        return mf.CrossSection([pts])

    if shape == 'teardrop':
        # Circular top, pointed bottom
        r_top = min(hw, hh * 0.5)
        cy_top = hh - r_top
        pts = []
        for i in range(33):
            a = math.radians(180 * i / 32)
            pts.append((r_top * math.cos(a), cy_top + r_top * math.sin(a)))
        pts.append((0.0, -hh))
        return mf.CrossSection([pts])

    if shape == 'arch':
        # Flat bottom, rounded top (door arch)
        pts = [(-hw, -hh), (hw, -hh)]
        for i in range(33):
            a = math.radians(180 * i / 32)
            pts.append((hw * math.cos(a), hh * math.sin(a)))
        return mf.CrossSection([pts])

    # Default / rectangle: rounded rect
    r_c = min(4.0, min(hw, hh) * 0.20)
    if r_c < 0.5:
        return mf.CrossSection.square([width_mm, height_mm], center=True)
    pts  = []
    pts += arc_pts(-hw + r_c, -hh + r_c, r_c, 180, 270)
    pts += arc_pts( hw - r_c, -hh + r_c, r_c, 270, 360)
    pts += arc_pts( hw - r_c,  hh - r_c, r_c,   0,  90)
    pts += arc_pts(-hw + r_c,  hh - r_c, r_c,  90, 180)
    return mf.CrossSection([pts])


def generate_photo_frame(image_bytes, width_mm=80.0, height_mm=100.0, border_mm=8.0,
                          min_thick=0.8, max_thick=3.0, resolution=3):
    """
    Lithophane photo frame panel.
    Returns (stl_bytes, total_w_mm, total_h_mm) for pairing with stand.
    """
    total_w = width_mm + 2 * border_mm
    total_h = height_mm + 2 * border_mm
    stl = generate_lithophane_panel(
        image_bytes, total_w, total_h,
        min_thick=min_thick, max_thick=max_thick,
        resolution=resolution, border_mm=border_mm,
    )
    return stl, total_w, total_h


def generate_frame_stand(frame_total_w, frame_total_h, max_thick, angle_deg=90):
    """
    Desk stand for lithophane photo frame (friction-grip channel design).
    angle_deg: 90 = frame vertical, 75 = 15° lean-back.
    """
    import manifold3d as mf, math

    stand_w = min(frame_total_w * 0.65, 80.0)
    wall_t  = 3.5
    lip_t   = 3.0
    foot_t  = 4.5
    foot_d  = 48.0
    stand_h = min(frame_total_h * 0.55, 72.0)
    notch_h = 18.0
    slot_d  = max_thick + 0.6

    total_slot_y = lip_t + slot_d + wall_t

    # Foot plate
    foot = mf.Manifold.cube([stand_w, total_slot_y + foot_d, foot_t])

    # Channel block (front-lip + slot + back-wall stub)
    ch_block = mf.Manifold.cube([stand_w, total_slot_y, notch_h + foot_t])
    slot_cut = mf.Manifold.cube([stand_w + 2, slot_d, notch_h + 2])
    slot_cut = slot_cut.translate([-1, lip_t, foot_t])
    ch_block = ch_block - slot_cut

    # Back wall
    back = mf.Manifold.cube([stand_w, wall_t, stand_h + foot_t])
    back = back.translate([0, total_slot_y - wall_t, 0])

    # Diagonal brace rib
    brace_y = total_slot_y + foot_d * 0.50
    brace   = mf.Manifold.cube([stand_w, wall_t, stand_h * 0.6 + foot_t])
    brace   = brace.translate([0, brace_y, 0])

    stand = foot + ch_block + back + brace

    if angle_deg < 89:
        tilt  = 90 - angle_deg
        stand = stand.rotate([tilt, 0, 0])
        verts = np.array(stand.to_mesh().vert_properties)
        min_z = float(verts[:, 2].min())
        if min_z < 0:
            stand = stand.translate([0, 0, -min_z])

    stand = stand.translate([(frame_total_w - stand_w) / 2, 0, 0])
    return _manifold_to_stl_bytes(stand)


def generate_frame_integrated(image_bytes, width_mm=80.0, height_mm=100.0, border_mm=8.0,
                               min_thick=0.8, max_thick=3.0, resolution=3, angle_deg=90):
    """
    One-piece lithophane photo frame with a triangular stand brace fused to the back.
    angle_deg: 90 = frame stands vertical, 75 = leans ~15° back.
    Returns STL bytes (single printable piece).
    """
    import manifold3d as mf, math

    total_w = width_mm + 2 * border_mm
    total_h = height_mm + 2 * border_mm

    hmap, _, _ = _compute_height_map(
        image_bytes, total_w, total_h,
        min_thick=min_thick, max_thick=max_thick,
        resolution=resolution, border_mm=border_mm,
    )
    panel = _build_panel_manifold(hmap, total_w, total_h)
    # Panel occupies X=[0..total_w], Y=[0..total_h], Z=[0..max_thick]
    # Z=0 is the flat back face; stand grows into Z<0

    stand_d  = min(total_h * 0.42, 90.0)
    attach_h = min(total_h * 0.42, 80.0)
    stand_w  = min(total_w * 0.55, 65.0)
    if angle_deg < 89:
        lean    = 90 - angle_deg
        stand_d = stand_d * (1.0 + math.sin(math.radians(lean)) * 0.5)

    sx = float((total_w - stand_w) / 2)
    sw = float(stand_w)
    sd = float(stand_d)
    ah = float(attach_h)

    # Triangular prism stand. Vertices:
    #   A1/A2 = frame bottom-back (Y=0, Z=0)
    #   C1/C2 = upper attachment (Y=ah, Z=0)
    #   B1/B2 = stand tip on desk (Y=0, Z=-sd)
    # All faces wound for outward normals (verified with cross products).
    verts = np.array([
        [sx,    0,   0],   # 0 = A1
        [sx,    ah,  0],   # 1 = C1
        [sx,    0,  -sd],  # 2 = B1
        [sx+sw, 0,   0],   # 3 = A2
        [sx+sw, ah,  0],   # 4 = C2
        [sx+sw, 0,  -sd],  # 5 = B2
    ], dtype=np.float32)
    tris = np.array([
        [0, 1, 2],          # left face  → -X
        [3, 5, 4],          # right face → +X
        [0, 2, 5], [0, 5, 3],  # bottom  → -Y
        [0, 3, 4], [0, 4, 1],  # back    → +Z
        [2, 1, 4], [2, 4, 5],  # hypotenuse → outward
    ], dtype=np.uint32)
    stand = mf.Manifold(mf.Mesh(vert_properties=verts, tri_verts=tris))

    # Thin foot pad at the stand tip for desk stability
    foot_d = min(sd * 0.18, 12.0)
    foot   = mf.Manifold.cube([sw, 3.5, foot_d + 0.2])
    foot   = foot.translate([sx, 0, -sd - foot_d])

    combined = panel + stand + foot
    if angle_deg < 89:
        tilt = float(90 - angle_deg)
        combined = combined.rotate([tilt, 0, 0])
        m_verts = np.array(combined.to_mesh().vert_properties)
        min_z = float(m_verts[:, 2].min())
        if min_z < 0:
            combined = combined.translate([0, 0, -min_z])
    return _manifold_to_stl_bytes(combined)


def generate_keychain(image_bytes, shape='rectangle', width_mm=50.0, height_mm=65.0,
                       border_mm=4.0, min_thick=0.8, max_thick=3.0,
                       resolution=2, hole_d=4.5):
    """
    Shaped lithophane keychain with keyring tab above the shape.
    shape: rectangle | oval | circle | hexagon | shield | tag | heart | star |
           diamond | teardrop | arch
    hole_d: keyring hole diameter mm (0 = no hole).
    Returns STL bytes.
    """
    import manifold3d as mf

    hmap, px_w, px_h = _compute_height_map(
        image_bytes, width_mm, height_mm,
        min_thick=min_thick, max_thick=max_thick,
        resolution=resolution, border_mm=border_mm,
    )

    panel = _build_panel_manifold(hmap, width_mm, height_mm)

    cs          = _keychain_shape_cs(shape, width_mm, height_mm)
    shape_solid = mf.Manifold.extrude(cs, max_thick + 2)
    shape_solid = shape_solid.translate([width_mm / 2, height_mm / 2, 0])

    keychain = panel ^ shape_solid

    if hole_d > 0.5:
        r      = hole_d / 2
        # Solid tab above the shape — hole lives here, never inside the image area
        tab_w  = max(hole_d * 2.5, 10.0)
        tab_h  = hole_d + 5.0
        tab    = mf.Manifold.cube([tab_w, tab_h, max_thick])
        tab    = tab.translate([width_mm / 2 - tab_w / 2, height_mm, 0])
        # Drill hole through tab centre
        cyl    = mf.Manifold.cylinder(max_thick + 4, r, circular_segments=32)
        cyl    = cyl.translate([width_mm / 2, height_mm + tab_h / 2, -2])
        tab    = tab - cyl
        keychain = keychain + tab

    return _manifold_to_stl_bytes(keychain)
