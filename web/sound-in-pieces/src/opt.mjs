// puzzle.glb from the private sources (SIP_SRC or runs/sound-in-pieces) -> public/models/puzzle.js, a base64 module,
// and its print texture -> public/img/print-1600.webp, the designer's default print
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { prune, dedup, weld, textureCompress, meshopt } from '@gltf-transform/functions';
import { MeshoptEncoder } from 'meshoptimizer';
import sharp from 'sharp';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
const here = p => fileURLToPath(new URL(p, import.meta.url));
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });
const doc = await io.read(join(process.env.SIP_SRC || here('../../../runs/sound-in-pieces/'), 'puzzle.glb'));
const root = doc.getRoot();
const print = root.listMaterials().find(m => m.getName() === 'print')?.getBaseColorTexture();
if (print) await sharp(Buffer.from(print.getImage())).resize(1600, 1600).webp({ quality: 82 }).toFile(here('../public/img/print-1600.webp'));
for (const n of root.listNodes()) if (n.getName() === 'table') { n.getMesh()?.dispose(); n.dispose(); }
const lin = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255).map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const look = { 'birch': ['#d9c4a1', 0.72], 'frame birch': ['#cfb68c', 0.68], 'laser edge': ['#4a3524', 0.85], 'print': [null, 0.5] };
for (const m of root.listMaterials()) {
  const l = look[m.getName()]; if (!l) continue;
  if (l[0]) m.setBaseColorFactor([...lin(l[0]), 1]);
  m.setRoughnessFactor(l[1]); m.setMetallicFactor(0); m.setDoubleSided(false);
}
await MeshoptEncoder.ready;
await doc.transform(dedup(), weld(), textureCompress({ encoder: sharp, targetFormat: 'jpeg', resize: [2048, 2048], quality: 86 }), meshopt({ encoder: MeshoptEncoder, level: 'medium' }), prune());
writeFileSync(here('../public/models/puzzle.js'), `export default "${Buffer.from(await io.writeBinary(doc)).toString('base64')}";\n`);
console.log('nodes', root.listNodes().length, 'materials', root.listMaterials().map(m => m.getName()).join(','));
