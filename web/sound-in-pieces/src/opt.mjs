import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { prune, dedup, weld, textureCompress, meshopt } from '@gltf-transform/functions';
import { MeshoptEncoder } from 'meshoptimizer';
import sharp from 'sharp';
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });
// the product GLB from the private sources (SIP_SRC or runs/sound-in-pieces) -> public/models/puzzle.js, a base64 module
import { writeFileSync } from 'node:fs';
const SRC = process.env.SIP_SRC || new URL('../../../runs/sound-in-pieces/', import.meta.url).pathname;
const doc = await io.read(SRC.replace(/\/?$/, '/') + 'puzzle.glb');
const root = doc.getRoot();
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
writeFileSync(new URL('../public/models/puzzle.js', import.meta.url), `export default "${Buffer.from(await io.writeBinary(doc)).toString('base64')}";\n`);
console.log('nodes', root.listNodes().length, 'materials', root.listMaterials().map(m => m.getName()).join(','));
