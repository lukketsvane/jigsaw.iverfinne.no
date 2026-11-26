"""PhyloPic ingestion: licence-filtered animal silhouettes with per-asset provenance and SHA-256.

Standard library only, so it can run under ``python -I``.

Rules enforced here:
  * only licences on the allow-list in configs/taxonomy.json (CC0 1.0, Public Domain Mark 1.0) are fetched. The API's
    licence filters are used AND the licence link of every returned record is re-checked;
  * a class is a set of clades; clade names are resolved to node UUIDs once and pinned in nodes.json;
  * an image that falls into two different classes is dropped as ambiguous, never guessed;
  * originals are written once to originals/<uuid>.png and never modified; the manifest stores the SHA-256
    of the bytes on disk, the file URL, the creator and the licence.

PhyloPic's API is documented at https://api-docs.phylopic.org/v2/ (the API specification itself is CC0). The API
host's robots.txt disallows crawlers; this module is an API client that only calls documented endpoints, with a
small worker pool and a generic User-Agent.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

API = 'https://api.phylopic.org'
SITE = 'https://www.phylopic.org'
UA = 'silhouette-dataset-builder/0.1'
ACCEPT = 'application/vnd.phylopic.v2+json'


class Api:
    def __init__(self, pause=0.12):
        self.pause = pause
        self.calls = 0
        self.build = None

    def raw(self, url, accept=None, timeout=60):
        err = None
        for attempt in range(5):
            try:
                headers = {'User-Agent': UA}
                if accept: headers['Accept'] = accept
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                    self.calls += 1
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 410): raise
                err = e
            except Exception as e:                         # network hiccup: back off and retry
                err = e
            time.sleep(1.5 * (attempt + 1))
        raise err

    def get(self, path, **q):
        if self.build is not None and 'build' not in q and path != '/': q['build'] = self.build
        url = API + path + ('?' + urllib.parse.urlencode(q) if q else '')
        out = json.loads(self.raw(url, ACCEPT))
        time.sleep(self.pause)
        return out

    def start(self):
        root = self.get('/')
        self.build = root['build']
        return root


def node_names(node):
    """all names of a node as plain lower-case strings (scientific and vernacular parts only), canonical first"""
    out = []
    for n in node.get('names', []):
        out.append(' '.join(p['text'] for p in n if p.get('class') in ('scientific', 'vernacular')).strip().lower())
    return out


def resolve_node(api, name, accepted=None):
    """scientific name -> node. The node's canonical (first) name must equal the query. A node that only lists the
    query as a synonym is accepted only when configs/taxonomy.json names that canonical name under
    accepted_synonyms (a bare synonym match once resolved 'Proboscidea' to a family of worms)."""
    q = name.lower()
    found = []
    page = 0
    while page < 4:
        d = api.get('/nodes', filter_name=q, embed_items='true', page=page)
        items = d.get('_embedded', {}).get('items', [])
        found += items
        if 'next' not in d.get('_links', {}) or d['_links']['next'] is None: break
        page += 1
    canon = [e for e in found if node_names(e)[:1] == [q]]
    syn = [e for e in found if q in node_names(e)]
    syn = [e for e in syn if accepted and node_names(e)[0] == accepted.get(name)]
    if canon: node, how = canon[0], 'canonical'
    elif syn: node, how = syn[0], 'accepted synonym'
    else: raise LookupError(f'no PhyloPic node with canonical name {name!r} (add an accepted_synonyms entry if a synonym is right)')
    return {'query': name, 'uuid': node['uuid'], 'canonical': node_names(node)[0], 'match': how,
            'names': node_names(node)[:6]}


def clade_images(api, uuid):
    """all licence-filtered image records in a clade"""
    out = []
    page = 0
    while True:
        d = api.get('/images', filter_clade=uuid, filter_license_by='false', filter_license_nc='false',
                    filter_license_sa='false', embed_items='true', page=page)
        items = d.get('_embedded', {}).get('items', [])
        out += items
        nxt = d.get('_links', {}).get('next')
        if not items or not nxt: break
        page += 1
    return out


def pick_raster(rec, want=1024):
    """the raster file whose larger side is closest to `want` pixels"""
    best = None
    for f in rec['_links'].get('rasterFiles', []):
        try: w, h = (int(float(v)) for v in f['sizes'].split('x'))
        except Exception: continue
        key = abs(max(w, h) - want)
        if best is None or key < best[0]: best = (key, f['href'], w, h)
    return best[1:] if best else None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def ingest(taxonomy_path, out_dir, workers=4, want=1024, log=print):
    out_dir = Path(out_dir)
    originals = out_dir / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    tax = json.loads(Path(taxonomy_path).read_text(encoding='utf-8'))
    allow = tax['license_allow']
    api = Api()
    root = api.start()
    log(f"PhyloPic API {root.get('version')} build {api.build} ({root.get('buildTimestamp')})")

    nodes_path = out_dir / 'nodes.json'
    pinned = json.loads(nodes_path.read_text(encoding='utf-8')) if nodes_path.exists() else {}

    def node(name):
        if name not in pinned:
            pinned[name] = resolve_node(api, name, tax.get('accepted_synonyms'))
            log(f"  node {name:18s} -> {pinned[name]['uuid']} {pinned[name]['canonical']!r} ({pinned[name]['match']})")
        return pinned[name]

    cache = {}

    def images(name):
        if name not in cache: cache[name] = clade_images(api, node(name)['uuid'])
        return cache[name]

    chosen = {}                                             # uuid -> (record, class, family, sub)
    dropped = []
    per_class = {}
    for c in tax['classes']:
        recs = {}
        for clade in c['clades']:
            for r in images(clade): recs[r['uuid']] = r
        for clade in c.get('exclude', []):
            for r in images(clade): recs.pop(r['uuid'], None)
        sub = {}
        for s, clades in c.get('sub', {}).items():
            for clade in clades:
                for r in images(clade):
                    if r['uuid'] in recs: sub[r['uuid']] = s
        kept = 0
        for u, r in recs.items():
            lic = r['_links'].get('license', {}).get('href')
            if lic not in allow:
                dropped.append({'uuid': u, 'class': c['name'], 'reason': f'licence not allowed: {lic}'}); continue
            if u in chosen and chosen[u][1] != c['name']:
                dropped.append({'uuid': u, 'class': c['name'], 'reason': f'also in class {chosen[u][1]}: ambiguous'})
                chosen[u] = (chosen[u][0], None, None, None); continue
            chosen[u] = (r, c['name'], c['family'], sub.get(u)); kept += 1
        per_class[c['name']] = kept
        log(f"class {c['name']:18s} {kept:5d} licence-clean images")
    nodes_path.write_text(json.dumps(pinned, indent=1), encoding='utf-8')
    chosen = {u: v for u, v in chosen.items() if v[1] is not None}

    def fetch(item):
        u, (r, cls, fam, sub) = item
        L = r['_links']
        pick = pick_raster(r, want)
        if pick is None: return {'uuid': u, 'class': cls, 'reason': 'no raster file'}, None
        href, w, h = pick
        path = originals / f'{u}.png'
        if not path.exists():
            try: data = api.raw(href)
            except Exception as e: return {'uuid': u, 'class': cls, 'reason': f'download failed: {e}'}, None
            tmp = path.with_suffix('.part')
            tmp.write_bytes(data)
            tmp.replace(path)
        lic = L['license']['href']
        creator = r.get('attribution') or L.get('contributor', {}).get('title')
        return None, {
            'asset_id': f'pp_{u}', 'source_id': u, 'common_name': cls, 'sub_class': sub, 'family': fam,
            'taxon': L.get('specificNode', {}).get('title'), 'general_taxon': (L.get('generalNode') or {}).get('title'),
            'source': 'phylopic', 'source_url': f'{SITE}/images/{u}', 'api_url': API + L['self']['href'],
            'file_url': href, 'file_px': [w, h], 'vector_url': (L.get('vectorFile') or {}).get('href'),
            'creator': creator, 'attribution': r.get('attribution'),
            'contributor': L.get('contributor', {}).get('title'),
            'license': allow[lic], 'license_url': lic, 'sha256': sha256_file(path), 'bytes': path.stat().st_size,
            'original_path': f'originals/{u}.png', 'created': r.get('created'), 'phylopic_build': api.build,
            'retrieved': datetime.now(timezone.utc).isoformat(timespec='seconds')}

    records = []
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for k, (bad, rec) in enumerate(ex.map(fetch, sorted(chosen.items()))):
            if bad: dropped.append(bad)
            else: records.append(rec)
            if (k + 1) % 250 == 0: log(f'  fetched {k + 1}/{len(chosen)} ({time.time() - t0:.0f}s)')
    records.sort(key=lambda r: r['asset_id'])
    (out_dir / 'manifest_raw.json').write_text(json.dumps(records, indent=1), encoding='utf-8')
    summary = {'api_version': root.get('version'), 'build': api.build, 'build_timestamp': root.get('buildTimestamp'),
               'license_allow': allow, 'records': len(records), 'dropped': len(dropped), 'per_class_listed': per_class,
               'bytes': sum(r['bytes'] for r in records), 'api_calls': api.calls,
               'finished': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    (out_dir / 'ingest_log.json').write_text(json.dumps({'summary': summary, 'dropped': dropped}, indent=1), encoding='utf-8')
    log(json.dumps(summary))
    return summary


if __name__ == '__main__':
    import sys
    here = Path(__file__).resolve().parents[1]
    ingest(here / 'configs' / 'taxonomy.json', here / 'data_cache' / 'phylopic',
           workers=int(sys.argv[1]) if len(sys.argv) > 1 else 4)
