"""Blender side of product_render.py: extrude every cut piece, print the picture on top, put the puzzle in its frame
tray and render the finished product. Run by product_render.py as

    blender -b --factory-startup --python blender_puzzle.py -- <scene.json>

Units are metres in Blender (the JSON is in millimetres). Every piece is its own object, named piece-001 ...
"""
import json, math, random, sys
import bpy, bmesh
from mathutils import Vector

cfg = json.load(open(sys.argv[sys.argv.index('--') + 1]))
S = .001
W, H, T, FW, BT = cfg['W'], cfg['H'], cfg['thickness'], cfg['frame'], cfg['backing']

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


# ---------------------------------------------------------------- materials

def node_mat(name):
    m = bpy.data.materials.new(name); m.use_nodes = True; nt = m.node_tree
    for n in list(nt.nodes): nt.nodes.remove(n)
    out = nt.nodes.new('ShaderNodeOutputMaterial'); bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled'); nt.links.new(bsdf.outputs[0], out.inputs[0])
    return m, nt, bsdf


def print_material(path):
    m, nt, bsdf = node_mat('print')
    tex = nt.nodes.new('ShaderNodeTexImage'); tex.image = bpy.data.images.load(path); tex.interpolation = 'Cubic'
    uv = nt.nodes.new('ShaderNodeUVMap'); nt.links.new(uv.outputs[0], tex.inputs[0]); nt.links.new(tex.outputs[0], bsdf.inputs['Base Color'])
    bsdf.inputs['Roughness'].default_value = .42
    return m


def wood_material(name, base, dark, bands=0., rough=.7):
    """birch: noise-broken grain along x; bands > 0 adds plywood plies along z (cut edges)"""
    m, nt, bsdf = node_mat(name)
    tc = nt.nodes.new('ShaderNodeTexCoord'); mp = nt.nodes.new('ShaderNodeMapping'); nt.links.new(tc.outputs['Object'], mp.inputs[0])
    wave = nt.nodes.new('ShaderNodeTexWave'); wave.wave_type = 'BANDS'; wave.bands_direction = 'Z' if bands else 'X'
    wave.inputs['Scale'].default_value = bands if bands else 9.; wave.inputs['Distortion'].default_value = 2. if not bands else .3
    wave.inputs['Detail'].default_value = 3.; nt.links.new(mp.outputs[0], wave.inputs[0])
    ramp = nt.nodes.new('ShaderNodeValToRGB'); ramp.color_ramp.elements[0].color = (*dark, 1); ramp.color_ramp.elements[1].color = (*base, 1)
    nt.links.new(wave.outputs['Fac'], ramp.inputs[0]); nt.links.new(ramp.outputs[0], bsdf.inputs['Base Color'])
    bsdf.inputs['Roughness'].default_value = rough
    return m


M_PRINT = print_material(cfg['texture'])
M_EDGE = wood_material('laser edge', (0.24, 0.13, 0.06), (0.11, 0.055, 0.025), bands=1.0 / (T * S / 5), rough=.8)     # charred plies
M_BACK = wood_material('birch', (0.80, 0.66, 0.47), (0.72, 0.58, 0.40))
M_FRAME = wood_material('frame birch', (0.74, 0.58, 0.39), (0.68, 0.52, 0.34))      # subtle grain
M_TABLE, _nt, _b = node_mat('table'); _b.inputs['Base Color'].default_value = (.045, .046, .05, 1); _b.inputs['Roughness'].default_value = .65


# ---------------------------------------------------------------- geometry

def ccw(xy):
    a = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(xy, xy[1:] + xy[:1]))
    return xy if a > 0 else xy[::-1]


def prism(name, xy_mm, z0_mm, t_mm, top_mat, side_mat, bottom_mat, uv_top=True):
    """extruded polygon; top face UV maps board millimetres onto the printed picture"""
    pts = ccw([(x, H - y) for x, y in xy_mm])                     # SVG y runs down
    bm = bmesh.new(); bot = [bm.verts.new((x * S, y * S, z0_mm * S)) for x, y in pts]; top = [bm.verts.new((x * S, y * S, (z0_mm + t_mm) * S)) for x, y in pts]
    ft = bm.faces.new(top); fb = bm.faces.new(bot[::-1]); ft.material_index = 0; fb.material_index = 2
    n = len(pts)
    for i in range(n):
        f = bm.faces.new((bot[i], bot[(i + 1) % n], top[(i + 1) % n], top[i])); f.material_index = 1; f.smooth = True
    for e in bm.edges:                                            # crisp rims, smooth curved walls
        if len(e.link_faces) == 2 and any(f.material_index != 1 for f in e.link_faces): e.smooth = False
    uv = bm.loops.layers.uv.new('UVMap')
    for f in bm.faces:
        for loop in f.loops:
            x, y = loop.vert.co.x / S, loop.vert.co.y / S
            loop[uv].uv = (x / W, y / H) if uv_top else (x / (W + 2 * FW), y / (H + 2 * FW))
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free()
    for mat in (top_mat, side_mat, bottom_mat): me.materials.append(mat)
    ob = bpy.data.objects.new(name, me); scene.collection.objects.link(ob)
    return ob


def ring(name, outer_mm, inner_mm, z0_mm, t_mm, mat):
    """frame: outer loop and inner loop bridged on top and bottom, walls on both sides"""
    def loop(xy, z): return [bm.verts.new((x * S, (H - y) * S, z * S)) for x, y in xy]
    bm = bmesh.new(); o = ccw(outer_mm); i_ = ccw(inner_mm)
    ob_, ot = loop(o, z0_mm), loop(o, z0_mm + t_mm); ib, it = loop(i_, z0_mm), loop(i_, z0_mm + t_mm)
    def edges(vs): return [bm.edges.new((a, b)) for a, b in zip(vs, vs[1:] + vs[:1])]
    eo_t, ei_t, eo_b, ei_b = edges(ot), edges(it), edges(ob_), edges(ib)
    bmesh.ops.bridge_loops(bm, edges=eo_t + ei_t); bmesh.ops.bridge_loops(bm, edges=eo_b + ei_b)
    bmesh.ops.bridge_loops(bm, edges=eo_t + eo_b); bmesh.ops.bridge_loops(bm, edges=ei_t + ei_b)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free(); me.materials.append(mat)
    ob = bpy.data.objects.new(name, me); scene.collection.objects.link(ob); return ob


pieces = []
for i, xy in enumerate(cfg['pieces']):
    pieces.append(prism(f'piece-{i + 1:03d}', [tuple(p) for p in xy], 0., T, M_PRINT, M_EDGE, M_BACK))
frame = ring('frame', [tuple(p) for p in cfg['frame_outer']], [tuple(p) for p in cfg['frame_inner']], 0., T, M_FRAME)
backing = prism('backing', [tuple(p) for p in cfg['frame_outer']], -BT, BT, M_BACK, M_EDGE, M_BACK, uv_top=False)
bpy.ops.mesh.primitive_plane_add(size=3., location=(W * S / 2, H * S / 2, -BT * S))
table = bpy.context.active_object; table.name = 'table'; table.data.materials.append(M_TABLE)


# ---------------------------------------------------------------- light, camera, render

world = bpy.data.worlds.new('world'); scene.world = world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs[0].default_value = (.035, .037, .042, 1); world.node_tree.nodes['Background'].inputs[1].default_value = 1.
bpy.ops.object.light_add(type='AREA', location=(W * S * .2, -H * S * .3, 1.1)); key = bpy.context.active_object
key.data.size = 1.2; key.data.energy = cfg.get('key_watts', 120); key.rotation_euler = (math.radians(28), 0, math.radians(-25))
bpy.ops.object.light_add(type='SUN', location=(0, 0, 2)); sun = bpy.context.active_object
sun.data.energy = cfg.get('sun_strength', 1.1); sun.data.angle = math.radians(3); sun.rotation_euler = (math.radians(40), math.radians(-18), math.radians(35))

scene.render.engine = 'CYCLES'
try:
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for kind in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = kind; prefs.get_devices()
            if any(d.type == kind for d in prefs.devices):
                for d in prefs.devices: d.use = d.type == kind
                scene.cycles.device = 'GPU'; break
        except TypeError:
            continue
except Exception as e:
    print('GPU setup failed, rendering on CPU:', e)
scene.cycles.samples = cfg.get('samples', 96); scene.cycles.use_denoising = True
scene.view_settings.exposure = cfg.get('exposure', -1.0)


def look_at(cam, target):
    d = Vector(target) - cam.location; cam.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()


def render(name, loc, target, lens=50., res=(2400, 1600), ortho=None, dof=None):
    cam_data = bpy.data.cameras.new(name); cam = bpy.data.objects.new(name, cam_data); scene.collection.objects.link(cam)
    cam.location = loc; look_at(cam, target); cam_data.lens = lens; cam_data.clip_start = .005
    if ortho: cam_data.type = 'ORTHO'; cam_data.ortho_scale = ortho
    if dof: cam_data.dof.use_dof = True; cam_data.dof.focus_distance = (Vector(target) - Vector(loc)).length; cam_data.dof.aperture_fstop = dof
    scene.camera = cam; scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.filepath = str(cfg['out'] + '/' + name + '.png'); bpy.ops.render.render(write_still=True)
    print('rendered', scene.render.filepath, flush=True)


cx, cy = W * S / 2, H * S / 2
if 'gltf' in cfg:                                               # assembled model, before any piece is moved
    bpy.ops.export_scene.gltf(filepath=cfg['gltf'], export_format='GLB')
    print('exported', cfg['gltf'], flush=True)
if 'stl' in cfg:
    for ob in scene.objects: ob.select_set(ob.name.startswith('piece-') or ob.name in ('frame', 'backing'))
    bpy.ops.wm.stl_export(filepath=cfg['stl'], export_selected_objects=True, apply_modifiers=True, global_scale=1000.)
    print('exported', cfg['stl'], flush=True)

views = cfg.get('views', ['top', 'hero', 'detail'])
if 'top' in views:
    render('render-top', (cx, cy, 1.5), (cx, cy, 0.), ortho=max(W, H) * S + 2 * FW * S + .01, res=(2400, 2400))
if 'hero' in views:
    render('render-hero', (cx + .12, cy - .62, .52), (cx, cy - .01, 0.), lens=55., dof=8.)
if 'detail' in views:
    # lift a handful of pieces out of one corner and lay them on the table beside the tray
    rnd = random.Random(7); corner = Vector((W * S * .78, H * S * .74, 0)); near = sorted(pieces, key=lambda o: (o.data.vertices[0].co - corner).length)[:cfg.get('lifted', 9)]
    for j, ob in enumerate(near):
        mid = sum((v.co for v in ob.data.vertices), Vector()) / len(ob.data.vertices)
        dest = Vector((W * S + FW * S + .035 + (j % 3) * .045, H * S * .45 + (j // 3) * .05, 0))
        ob.location = dest - Vector((mid.x, mid.y, 0)); ob.rotation_euler = (0, 0, rnd.uniform(-.6, .6))
    near[0].location.z += T * S * 2.5; near[0].rotation_euler = (rnd.uniform(.25, .4), rnd.uniform(-.2, .2), .3)       # one tilted, showing the cut edge
    render('render-detail', (cx + .30, cy - .16, .20), (cx + .13, cy + .07, 0.), lens=60., dof=5.6)
print('done', flush=True)
