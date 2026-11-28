"""Map of the rock-carving corpus: every traced figure (processed/catalog.json) at the place its source names.

    ..\\.venv\\Scripts\\python.exe sitemap.py geocode      # site names -> coordinates (OpenStreetMap Nominatim, cached)
    ..\\.venv\\Scripts\\python.exe sitemap.py build        # interactive map: runs/hellerisninger-map/index.html

The sources carry site, region and country but no coordinates. `geocode` cleans each site name (drops panel numbers,
Askeladden and RAÄ ids), then asks Nominatim for the most specific place it finds: the site itself, then the municipality
or locality named in the region, then the region. Every point records which of these it is (`precision`), so a figure
placed at a region centroid is never mistaken for a surveyed panel. Requests send only place names, one per second, and
are cached in runs/hellerisninger-map/geocode-cache.json.

`build` writes one marker per traced figure, spread in a small spiral around its place so figures from one panel do
not hide each other, clustered by Leaflet.markercluster. Popups show the traced silhouette, period, motif class, site,
precision, licence and source link. Exact panel coordinates (Askeladden, FMIS) are the next step: see `askeladden_id`.
"""
import argparse, json, math, re, sys, time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'runs' / 'hellerisninger-map'
CACHE = OUT / 'geocode-cache.json'
COUNTRY = {'NO': 'Norway', 'SE': 'Sweden', 'DK': 'Denmark', 'FI': 'Finland'}
GENERIC = {'norway', 'sweden', 'denmark', 'finland', 'norway (unspecified)', 'hällristning', 'helleristning'}
UA = {'User-Agent': 'rockart-corpus-map/0.1 (research prototype)'}


def sources():
    rows = {}
    for m in ('nordic_archives', 'norway_archives', 'wikimedia'):
        for line in (HERE / 'sources' / m / 'manifest.jsonl').read_text(encoding='utf-8').splitlines():
            d = json.loads(line); rows[d['source_id']] = d
    return rows


def ids(site):
    a = re.search(r'Askeladden\s*([0-9]+(?:-[0-9]+)?)', site); r = re.search(r'([A-ZÅÄÖ][a-zåäö]+)\s+(\d+:\d+)', site)
    return (a.group(1) if a else None), (f'{r.group(1)} {r.group(2)}' if r else None)


def clean(text):
    """a place name from a site or region label: no ids, panel numbers or descriptions"""
    t = re.split(r'\s+/\s+', text)[0]                                   # 'Fossum Tanum / Tanum 255:1' -> 'Fossum Tanum'
    t = t.split(' — ')[0].split(' – ')[0]                               # 'Hjemmeluft — Ole Pedersen 9' -> 'Hjemmeluft'
    t = re.sub(r'\b\d+:\d+\w*.*$', '', t)                               # panel numbers 'Bak Vehammaren 2:1a nr156'
    t = re.sub(r'(?i)\b(gnr|bnr)\s*\d+.*$', '', t)
    for _ in range(3):                                                   # 'Stein med helleristninger på Stein gård' -> 'Stein gård'
        t = re.sub(r'(?i)^(stein med |bergmaleri ved |helleristning(er)?( ved| på| i)? |hällristning(ar)?( med [^,]*)?( vid| på| i)? )', '', t)
    t = re.sub(r'(?i)(\. )?fornminne\.?$|,? mer informasjon', '', t)
    t = re.sub(r'\s*\(([^)]*)\)', lambda m: ', ' + m.group(1), t)                           # 'Evenhus (Frosta)' -> 'Evenhus, Frosta'
    t = re.sub(r'(\s+([A-Z]{0,2}\d+[a-zA-Z]?|[IVX]{1,4}))+$', '', t)          # trailing panel codes: 'Amtmannsnes 2B', 'Laxön D5', 'Bardal I'
    t = re.sub(r'\s+', ' ', t).strip(' ,.;')
    return t


PLACE = {'hamlet', 'village', 'farm', 'isolated_dwelling', 'locality', 'neighbourhood', 'suburb', 'quarter', 'place', 'island', 'islet',
         'peak', 'natural', 'bay', 'peninsula', 'archipelago', 'square', 'allotments', 'croft', 'plot'}
TOWN = {'town', 'city', 'municipality', 'borough', 'city_district', 'district', 'parish'}
AREA = {'county', 'state', 'region', 'province', 'country'}
RADIUS_KM = {'locality': 25., 'municipality': 45., 'region': 250.}     # how far a site match may lie from its anchor


def parts(region):
    """'Vestland — Bremanger (Vingen)' -> ['Vestland', 'Bremanger'], localities ['Vingen'], region aliases []"""
    raw = re.split(r' — | – ', region); plain = [clean(re.sub(r'\s*\([^)]*\)', '', x)) for x in raw]
    alias = [clean(x) for x in re.findall(r'\(([^)]*)\)', raw[0])]               # 'Västra Götalands län (Bohuslän)': the county's other name
    local = [clean(x) for r in raw[1:] for x in re.findall(r'\(([^)]*)\)', r)]   # 'Bremanger (Vingen)': a place inside the municipality
    return plain, local, alias


def anchors(d):
    """the known locality, municipality and region of a source, most specific first; a locality or municipality is
    asked for inside its region first ('Borg, Östergötlands län'), since many place names repeat across the country"""
    country = COUNTRY.get(d['country'], ''); plain, local, alias = parts(d['region'])
    reg = [r for r in plain[:1] + alias if r and r.lower() not in GENERIC]; out = []
    for x in local: out += [('locality', f'{x}, {m}, {country}') for m in plain[1:2]] + [('locality', f'{x}, {r}, {country}') for r in reg] + [('locality', f'{x}, {country}')]
    for m in plain[1:]: out += [('municipality', f'{m}, {r}, {country}') for r in reg] + [('municipality', f'{m}, {country}')]
    out += [('region', f'{r}, {country}') for r in reg]
    seen = set(); return [(p_, q) for p_, q in out if not (q in seen or seen.add(q))]


def sites(d):
    """site-level queries, most specific first: 'Aspeberget Tanum baksidan' -> 'Aspeberget, Tanum'; 'Ausevik, Flora' ->
    also 'Ausevik' on its own (Flora is no longer a municipality); the municipality in a site name comes last"""
    country = COUNTRY.get(d['country'], ''); plain, local, alias = parts(d['region']); s = clean(d['site'])
    if not s or not s[0].isupper() or s.lower() in GENERIC or s.lower() in (x.lower() for x in plain + local + alias): return []
    ctx = [m for m in local + plain[1:]] + [r for r in plain[:1] + alias if r and r.lower() not in GENERIC]
    names = [s]; w = s.split(); low = d['region'].lower() + ' ' + s.lower()
    for i in range(1, len(w)):                                          # 'Aspeberget Tanum baksidan': split at the parish name
        if w[i][0].isupper() and w[i].lower() in d['region'].lower() + ' ' + ' '.join(x.lower() for x in plain): names.insert(0, f"{' '.join(w[:i])}, {w[i]}"); break
    if ',' in s: names.append(s.split(',')[0].strip())                  # 'Ausevik, Flora' -> 'Ausevik'
    out = [f'{n}, {c}, {country}' for n in names for c in ctx] + [f'{n}, {country}' for n in names]
    seen = set(); return [q for q in out if not (q in seen or seen.add(q))]


def tail_place(d):
    """'Borge Lille, Fredrikstad' -> 'Fredrikstad': the municipality named inside a site label, a coarser fallback"""
    s = clean(d['site']); return s.split(',')[-1].strip() if ',' in s else None


def candidates(d):                       # kept for inspection: every query a source may send
    return [('site', q) for q in sites(d)] + anchors(d)


def km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0]); dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(min(1., math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def nominatim(q, cc, cache, session):
    key = f'{cc}|{q}'
    if key in cache: return cache[key]
    for attempt in range(3):
        try:
            r = session.get('https://nominatim.openstreetmap.org/search', params={'q': q, 'format': 'jsonv2', 'limit': 1, 'countrycodes': cc.lower()}, headers=UA, timeout=30)
            time.sleep(1.1)
            if r.status_code == 200:
                hit = r.json(); cache[key] = [float(hit[0]['lat']), float(hit[0]['lon']), hit[0].get('display_name', ''), hit[0].get('addresstype', '')] if hit else None
                return cache[key]
        except requests.RequestException:
            time.sleep(5)
    return None


def geocode(_):
    """anchor each source at its known locality, municipality or region; accept a site-level match only if it is a
    place (not a museum, road or building) and lies within RADIUS_KM of that anchor"""
    OUT.mkdir(parents=True, exist_ok=True); cache = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else {}
    rows = sources(); ses = requests.Session(); placed = {}; n = 0; rejected = 0
    for sid, d in rows.items():
        cc = d['country']; anc = None
        regional = None
        for prec, q in anchors(d)[::-1]:                                   # the region first, to check what lies inside it
            if prec != 'region': break
            hit = nominatim(q, cc, cache, ses)
            if hit and hit[3] in PLACE | TOWN | AREA: regional = hit
        for prec, q in anchors(d):
            hit = nominatim(q, cc, cache, ses)
            if not hit or hit[3] not in PLACE | TOWN | AREA: continue          # not a museum or a road
            if prec != 'region' and regional and km(hit, regional) > RADIUS_KM['region']: continue      # a namesake elsewhere
            anc = (prec, q, hit); break
        best = None
        for q in sites(d):
            hit = nominatim(q, cc, cache, ses)
            if not hit: continue
            kind = hit[3]; level = 'site' if kind in PLACE else 'municipality' if kind in TOWN else 'region' if kind in AREA else None
            named = level is None and anc is not None                     # a road, sign or heritage point named after the site:
            if named: level = 'site'                                       # right place if it lies close to the anchor
            if level is None or (anc and km(hit, anc[2]) > RADIUS_KM[anc[0]] * (.6 if named else 1.)): rejected += 1; continue
            best = (level, q, hit); break
        if best is None and tail_place(d):                               # a municipality named in the site label beats a whole region
            hit = nominatim(f"{tail_place(d)}, {COUNTRY.get(cc, '')}", cc, cache, ses)
            if hit and hit[3] in PLACE | TOWN and (not anc or km(hit, anc[2]) <= RADIUS_KM[anc[0]]) and (not anc or anc[0] == 'region'): best = ('municipality', tail_place(d), hit)
        pick = best or anc
        if pick: placed[sid] = dict(lat=pick[2][0], lon=pick[2][1], precision=pick[0], query=pick[1], match=pick[2][2], kind=pick[2][3])
        n += 1
        if n % 50 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding='utf-8'); print(f'{n}/{len(rows)} sources, {len(placed)} placed', flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding='utf-8')
    (OUT / 'places.json').write_text(json.dumps(placed, ensure_ascii=False, indent=1), encoding='utf-8')
    from collections import Counter
    print(f'placed {len(placed)} of {len(rows)} sources ({rejected} site matches rejected as too far from their anchor or not a place):',
          dict(Counter(p['precision'] for p in placed.values())))


def silhouette(path, keep=90):
    """the traced outline (100 x 100 box) as an SVG path, thinned to about `keep` points"""
    try: d = json.loads((HERE / path.replace('\\', '/')).read_text(encoding='utf-8'))
    except Exception: return ''
    rings = [d['exterior']] + d.get('holes', d.get('interiors', []))
    def ring(xy):
        step = max(1, len(xy) // keep); xy = xy[::step]
        return 'M' + 'L'.join(f'{x:.1f} {y:.1f}' for x, y in xy) + 'Z'
    return ''.join(ring(r) for r in rings if len(r) > 2)


def build(_):
    rows = sources(); placed = json.loads((OUT / 'places.json').read_text(encoding='utf-8'))
    cat = json.loads((HERE / 'processed' / 'catalog.json').read_text(encoding='utf-8'))
    groups = {}
    for m in cat:
        p = placed.get(m['source_id'])
        if p: groups.setdefault((round(p['lat'], 5), round(p['lon'], 5)), []).append(m)
    feats = []; golden = math.pi * (3 - math.sqrt(5))
    for (lat, lon), ms in groups.items():
        for i, m in enumerate(ms):          # sunflower rosette, about 15 m between neighbours: a place's figures side by side, not spread over the town
            r = 15 * math.sqrt(i); a = i * golden; dy = r * math.cos(a) / 111320; dx = r * math.sin(a) / (111320 * math.cos(math.radians(lat)))
            p = placed[m['source_id']]; src = rows.get(m['source_id'], {}); aid, raa = ids(m['site'])
            feats.append(dict(lat=round(lat + dy, 6), lon=round(lon + dx, 6), id=m['asset_id'], t=m['title'][:90], s=m['site'][:90], r=m['region'],
                              c=m['country'], p=m['period'], k=m['motif_class'], f=m.get('family', ''), pr=p['precision'], q=p['match'][:90],
                              u=m['source_url'], l=m['license'], a=aid, raa=raa, d=silhouette(m['polygon_path'])))
    unplaced = len(cat) - len(feats)
    from collections import Counter
    summary = dict(figures=len(cat), placed=len(feats), unplaced=unplaced, places=len(groups), by_country=dict(Counter(f['c'] for f in feats)),
                   by_precision=dict(Counter(f['pr'] for f in feats)), by_period=dict(Counter(f['p'] for f in feats)))
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    html = TEMPLATE.replace('__DATA__', json.dumps(feats, ensure_ascii=False, separators=(',', ':'))).replace('__SUMMARY__', json.dumps(summary))
    (OUT / 'index.html').write_text(html, encoding='utf-8'); print(json.dumps(summary, indent=1)); print(OUT / 'index.html')


TEMPLATE = r'''<!doctype html>
<html lang="no"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Helleristningar – kart</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js"></script>
<style>
:root{--ink:#1d1f21;--muted:#6b6f73;--stone:#2f7f86;--bronze:#b5562a;--other:#7d7f83;--card:#fff}
html,body{margin:0;height:100%;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:#eef0f1}
#map{position:absolute;inset:0}
.panel{position:absolute;z-index:1000;top:12px;left:12px;max-width:290px;background:var(--card);border-radius:10px;padding:12px 14px;box-shadow:0 2px 12px rgba(0,0,0,.18)}
.panel h1{font-size:16px;margin:0 0 4px}.panel p{margin:2px 0;color:var(--muted);font-size:12.5px}
.panel label{display:flex;align-items:center;gap:6px;margin:3px 0;font-size:13px;cursor:pointer}
.dot{width:11px;height:11px;border-radius:50%;display:inline-block}
.pop{display:flex;gap:10px;align-items:flex-start;min-width:250px}.pop svg{flex:0 0 96px;background:#f4f1ea;border-radius:6px}
.pop b{display:block;margin-bottom:2px}.pop small{color:var(--muted);display:block}
.marker-cluster div{background:rgba(29,31,33,.85)!important;color:#fff;font-weight:600}.marker-cluster{background:rgba(29,31,33,.22)!important}
@media (max-width:600px){.panel{max-width:none;left:8px;right:8px;top:8px;padding:10px 12px}}
</style></head><body><div id="map"></div>
<div class="panel"><h1>Helleristningar</h1><p id="count"></p>
<label><input type="checkbox" data-p="stone_age_hunter" checked><span class="dot" style="background:var(--stone)"></span>Steinalder (fangstkunst)</label>
<label><input type="checkbox" data-p="bronze_age_agrarian" checked><span class="dot" style="background:var(--bronze)"></span>Bronsealder (bergkunst)</label>
<label><input type="checkbox" data-p="other" checked><span class="dot" style="background:var(--other)"></span>Blanda / ukjend</label>
<label><input type="checkbox" id="grp" checked>Grupper nære figurar</label>
<p>Ein prikk per teikna figur, ved staden kjelda nemner. Ringar = presisjon: fylt = stad, open = kommune/region.</p></div>
<script>
const F=__DATA__, S=__SUMMARY__;
const COL={stone_age_hunter:'#2f7f86',bronze_age_agrarian:'#b5562a'};
const PER={stone_age_hunter:'Steinalder (fangstkunst)',bronze_age_agrarian:'Bronsealder (bergkunst)',mixed:'Blanda',unknown:'Ukjend'};
const PREC={site:'stad',locality:'lokalitet',municipality:'kommune',region:'region (grov)'};
const map=L.map('map',{preferCanvas:true,fadeAnimation:false}).setView([64.6,14.5],5);
const grey=L.tileLayer('https://cache.kartverket.no/v1/wmts/1.0.0/topograatone/default/webmercator/{z}/{y}/{x}.png',{maxZoom:18,attribution:'&copy; Kartverket'});
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap-bidragsytarar'});
grey.addTo(map); L.control.layers({'Kartverket gråtone (Noreg)':grey,'OpenStreetMap':osm},null,{position:'topright'}).addTo(map);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function popup(f){const ids=[f.a?'Askeladden '+esc(f.a):'',f.raa?'RAÄ '+esc(f.raa):''].filter(Boolean).join(' · ');
 return `<div class="pop"><svg viewBox="0 0 100 100" width="96" height="96"><path d="${f.d}" fill="#26282b" fill-rule="evenodd"/></svg><div><b>${esc(f.t)}</b>
 <small>${esc(f.s)} · ${esc(f.r)} (${esc(f.c)})</small><small>${esc(PER[f.p]||f.p)} · ${esc((f.k||'').replace(/_/g,' '))}</small>
 <small>Plassering: ${esc(PREC[f.pr]||f.pr)}${ids?' · '+ids:''}</small><small>${esc(f.l)} · <a href="${esc(f.u)}" target="_blank" rel="noopener">kjelde</a></small></div></div>`}
const groups={stone_age_hunter:[],bronze_age_agrarian:[],other:[]};
for(const f of F){const g=COL[f.p]?f.p:'other'; const c=COL[f.p]||'#7d7f83'; const exact=f.pr==='site'||f.pr==='locality';
 const m=L.circleMarker([f.lat,f.lon],{radius:6,color:c,weight:2,fillColor:c,fillOpacity:exact?.85:.15}); m.bindPopup(()=>popup(f),{maxWidth:340}); groups[g].push(m)}
const cluster=L.markerClusterGroup({maxClusterRadius:40,spiderfyOnMaxZoom:true,showCoverageOnHover:false,chunkedLoading:true});
const plain=L.layerGroup().addTo(map);
function refresh(){cluster.clearLayers(); plain.clearLayers(); const grp=document.getElementById('grp').checked; let n=0; document.querySelectorAll('[data-p]').forEach(cb=>{if(cb.checked){const g=groups[cb.dataset.p]; if(grp) cluster.addLayers(g); else g.forEach(m=>plain.addLayer(m)); n+=g.length}});
 document.getElementById('count').textContent=`${n} av ${S.figures} figurar på ${S.places} stader`+(S.unplaced?` · ${S.unplaced} utan stad`:'')}
document.querySelectorAll('[data-p],#grp').forEach(cb=>cb.addEventListener('change',refresh)); map.addLayer(cluster);
if(new URLSearchParams(location.search).has('dots')) document.getElementById('grp').checked=false; refresh();
</script></body></html>'''

if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); a.add_argument('cmd', choices=['geocode', 'build']); cmd = a.parse_args().cmd
    {'geocode': geocode, 'build': build}[cmd](None)
