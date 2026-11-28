import bpy, json, hashlib, math, runpy, subprocess, sys, tempfile
from pathlib import Path
from mathutils import Vector

args = sys.argv[sys.argv.index('--')+1:]
source, output = map(Path, args[:2])
output.mkdir(parents=True, exist_ok=True)
cfg_path = source / 'scene.json'
cfg = json.loads(cfg_path.read_text(encoding='utf-8-sig'))
texture = Path(args[2]) if len(args) > 2 else source / 'print-texture.png'
cfg.update(texture=str(texture), out=str(output), views=[], samples=512)
for key in ('gltf', 'stl'):
    cfg.pop(key, None)
with tempfile.TemporaryDirectory() as temp:
    config = Path(temp) / 'scene.json'
    config.write_text(json.dumps(cfg), encoding='utf-8')
    sys.argv = [sys.argv[0], '--', str(config)]
    runpy.run_path(str(Path(__file__).with_name('blender_puzzle.py')), run_name='__main__')

scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
scene.unit_settings.length_unit = 'MILLIMETERS'
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
prefs = bpy.context.preferences.addons['cycles'].preferences
backend = 'CPU'
for kind in ('OPTIX', 'CUDA'):
    try:
        prefs.compute_device_type = kind
        prefs.refresh_devices()
        for device in prefs.devices:
            device.use = device.type == kind
        if prefs.has_active_device():
            scene.cycles.device = 'GPU'
            backend = kind
            break
    except (RuntimeError, TypeError, ValueError):
        continue
scene.cycles.samples = 512
scene.cycles.use_adaptive_sampling = True
scene.cycles.adaptive_threshold = .01
scene.cycles.use_denoising = True
scene.cycles.denoiser = 'OPENIMAGEDENOISE'
scene.render.use_persistent_data = True
scene.view_settings.view_transform = 'AgX'
scene.view_settings.exposure = -1.25
scene.render.image_settings.file_format = 'PNG'
scene.render.resolution_percentage = 100
scene.render.film_transparent = False

def collection(name, objects):
    coll = bpy.data.collections.new(name)
    scene.collection.children.link(coll)
    for obj in objects:
        for old in list(obj.users_collection):
            old.objects.unlink(obj)
        coll.objects.link(obj)
    return coll

pieces = [o for o in scene.objects if o.name.startswith('piece-')]
assert len(pieces) == len(cfg['pieces'])
collection('01 | Puzzle pieces', pieces)
collection('02 | Frame and backing', [bpy.data.objects[n] for n in ('frame','backing')])
for obj in list(scene.objects):
    if obj.type == 'LIGHT':
        bpy.data.objects.remove(obj, do_unlink=True)
table = bpy.data.objects['table']
table.data.materials[0].node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (.67,.65,.60,1)
collection('03 | Studio surface', [table])
lights = collection('04 | Studio lighting', [])
cameras = collection('05 | Campaign cameras', [])
cx, cy = cfg['W']*.0005, cfg['H']*.0005
target = Vector((cx,cy,.002))

def aim(obj, point):
    obj.rotation_euler = (Vector(point)-obj.location).to_track_quat('-Z','Y').to_euler()

def light(name, location, watts, size, color):
    data=bpy.data.lights.new(name,'AREA')
    data.energy=watts
    data.shape='DISK'
    data.size=size
    data.color=color
    obj=bpy.data.objects.new(name,data)
    lights.objects.link(obj)
    obj.location=location
    aim(obj,target)

light('Key | softbox', (cx-.35,cy-.30,.70), 55, .55, (1.0,.94,.86))
light('Fill | softbox', (cx+.46,cy-.05,.47), 25, .48, (.87,.94,1.0))
light('Rim | softbox', (cx+.05,cy+.42,.55), 40, .42, (1.0,1.0,1.0))

def camera(name, location, point, lens=65, ortho=None):
    data=bpy.data.cameras.new(name)
    data.lens=lens
    data.clip_start=.001
    if ortho:
        data.type='ORTHO'
        data.ortho_scale=ortho
    obj=bpy.data.objects.new(name,data)
    cameras.objects.link(obj)
    obj.location=location
    aim(obj,point)
    return obj

hero=camera('01 | Hero', (cx+.20,cy-.78,.80), target, 65)
top=camera('02 | Top', (cx,cy,1.0), target, ortho=.48)
low=camera('03 | Low angle', (cx+.37,cy-.70,.22), target, 65)
detail=camera('04 | Cut detail', (cx+.22,cy-.16,.17), (cx+.12,cy-.085,.004), 85)
scene.camera=hero
scene.render.resolution_x=2400
scene.render.resolution_y=1800
scene.render.filepath='//renders/hero.png'
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.spaces.active.region_3d.view_perspective='CAMERA'
            area.spaces.active.shading.type='SOLID'
            area.spaces.active.shading.color_type='MATERIAL'
            area.spaces.active.clip_end=100
scene['source_commit']=subprocess.check_output(['git','-C',str(Path(__file__).parent),'rev-parse','HEAD'],text=True).strip()
scene['geometry_sha256']=hashlib.sha256(cfg_path.read_bytes()).hexdigest()
scene['piece_count']=len(pieces)
scene['render_backend']=backend
bpy.ops.file.pack_all()
bpy.ops.wm.save_as_mainfile(filepath=str(output/'aphex.blend'))
print('STUDIO_SAVED',len(pieces),backend,scene['source_commit'],flush=True)
scene.cycles.samples=24
scene.cycles.adaptive_threshold=.05
scene.render.resolution_x=1200
scene.render.resolution_y=900
scene.render.filepath=str(output/'hero-preview.png')
bpy.ops.render.render(write_still=True)
print('PREVIEW_SAVED',scene.render.filepath,flush=True)
