"""Expand template.html into the static site: index.html, manifest, service worker, vercel.json."""
import os
B = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(B, '..', 'public')
D = os.environ.get('SIP_SRC', os.path.join(B, '..', '..', '..', 'runs', 'sound-in-pieces'))  # private sources: renders, cut field, GLB
import hashlib, json, os, re
from melody import melody_chords

IMG = json.load(open(f'{B}/images.json'))


def esc(s):
    return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


def srcset(name):
    m = IMG[name]
    return ', '.join(f'img/{name}-{w}.webp {w}w' for w in m['ws'])


def img(args):
    parts = [a.strip() for a in args.split('|')] + [''] * 5
    name, sizes, alt, cls, mode = parts[:5]
    m = IMG[name]
    mid = min(m['ws'], key=lambda w: abs(w - 1200))
    classes = cls.split()
    if 'nofade' in classes:
        classes.remove('nofade')
    else:
        classes.insert(0, 'f')
    load = 'loading="eager" fetchpriority="high"' if mode == 'eager' else 'loading="lazy"'
    return (f'<img src="img/{name}-{mid}.webp" srcset="{srcset(name)}" sizes="{sizes}" width="{m["w"]}" '
            f'height="{m["h"]}" alt="{esc(alt)}" class="{" ".join(classes)}" {load} decoding="async">')


def preload(args):
    name, sizes = [a.strip() for a in args.split('|')]
    return (f'<link rel="preload" as="image" href="img/{name}-1400.webp" imagesrcset="{srcset(name)}" '
            f'imagesizes="{sizes}" fetchpriority="high">')


t = open(f'{B}/template.html').read()
t = t.replace('{{melody}}', json.dumps(melody_chords(f'{SITE}/media/melody.mid'), separators=(',', ':')))
t = t.replace('{{splash}}', open(f'{B}/splash-links.html').read())
t = re.sub(r'\{\{preload ([^}]+)\}\}', lambda m: preload(m.group(1)), t)
t = re.sub(r'\{\{img ([^}]+)\}\}', lambda m: img(m.group(1)), t)
assert '{{' not in t, re.findall(r'\{\{[^}]*\}\}', t)
# "~" marks a no-break space in text; never touch <style>/<script>
chunks = re.split(r'(<style>.*?</style>|<script>.*?</script>)', t, flags=re.S)
t = ''.join(c if c.startswith(('<style>', '<script>')) else c.replace('~', '&nbsp;') for c in chunks)
open(f'{SITE}/index.html', 'w').write(t)

# designer: design.html as is; designer.js = head + web/fossilnet geometry, layout, partition and seams + tail
import re, shutil
shutil.copy(f'{B}/design.html', f'{SITE}/design.html')
_src = open(os.path.join(B, '..', '..', 'fossilnet', 'index.html')).read()
_cut = lambda a, b: _src[_src.index(a):_src.index(b)]
_geo = re.sub(r'function chaikin\(.*?\n}\n', '', _cut('// ---------------------------------------------------------------- small geometry', '// ---------------------------------------------------------------- creatures'), flags=re.S)
open(f'{SITE}/js/designer.js', 'w').write(open(f'{B}/designer_head.js').read()
    + '// the geometry, layout, partition and seams below are web/fossilnet/index.html, unchanged\n' + _geo
    + _cut('// ---------------------------------------------------------------- creatures', 'const NA = ANCH.length')
    + _cut('// ---------------------------------------------------------------- layout (field.pack', '// ---------------------------------------------------------------- engraving')
    + open(f'{B}/designer_tail.js').read())

manifest = {
    'name': 'Sound in Pieces', 'short_name': 'In Pieces',
    'description': 'SOUND IN PIECES / APHEX 398. A framed jigsaw concept: 183 logo-derived pieces. Unofficial design concept.',
    'id': './', 'start_url': './', 'scope': './', 'display': 'standalone', 'orientation': 'any',
    'background_color': '#000000', 'theme_color': '#000000', 'categories': ['design', 'art'],
    'icons': [
        {'src': 'icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
        {'src': 'icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
        {'src': 'icons/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        {'src': 'icons/favicon.svg', 'sizes': 'any', 'type': 'image/svg+xml'},
    ],
}
json.dump(manifest, open(f'{SITE}/manifest.webmanifest', 'w'), indent=1)

# service worker: precache the shell, cache images/fonts as they are used, leave video to the network
core = ['./', 'index.html', 'manifest.webmanifest', 'data/pieces.json',
        'fonts/plex-mono-400.woff2', 'icons/favicon.svg',
        'icons/apple-touch-icon.png', 'icons/icon-192.png', 'img/hero-800.webp', 'img/hero-1400.webp',
        'js/hero.js', 'models/puzzle.js', 'img/grain.png',
        'design.html', 'js/designer.js', 'js/pathnet-logo.js', 'img/print-1600.webp']
h = hashlib.sha1()
for f in core[1:]:
    h.update(open(f'{SITE}/{f}', 'rb').read())
ver = h.hexdigest()[:10]
open(f'{SITE}/sw.js', 'w').write(f"""const V='sip-{ver}',CORE={json.dumps(core)};
self.addEventListener('install',e=>e.waitUntil(caches.open(V).then(c=>c.addAll(CORE.map(u=>new Request(u,{{cache:'reload'}})))).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==V).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{{
  const r=e.request,u=new URL(r.url);
  if(r.method!=='GET'||u.origin!==location.origin||u.pathname.includes('/media/'))return;
  if(r.mode==='navigate'){{
    e.respondWith(fetch(r).then(res=>{{const c=res.clone();caches.open(V).then(x=>x.put(r,c));return res}}).catch(()=>caches.match(r).then(m=>m||caches.match('./'))));
    return}}
  e.respondWith(caches.match(r).then(m=>m||fetch(r).then(res=>{{if(res.ok){{const c=res.clone();caches.open(V).then(x=>x.put(r,c))}}return res}})));
}});
""")

json.dump({'outputDirectory': 'public', 'headers': [
    {'source': '/sw.js', 'headers': [{'key': 'Cache-Control', 'value': 'no-cache'}]},
    {'source': '/manifest.webmanifest', 'headers': [{'key': 'Content-Type', 'value': 'application/manifest+json'}]},
    {'source': '/(js|data|models)/(.*)', 'headers': [{'key': 'Cache-Control', 'value': 'public, max-age=0, must-revalidate'}]},  # not hashed: always revalidate
    {'source': '/(img|fonts|media|icons|splash)/(.*)', 'headers': [{'key': 'Cache-Control', 'value': 'public, max-age=3600'}]},
]}, open(os.path.join(B, '..', 'vercel.json'), 'w'), indent=1)

size = os.path.getsize(f'{SITE}/index.html')
print('index.html', size // 1024, 'KB; sw', ver)
