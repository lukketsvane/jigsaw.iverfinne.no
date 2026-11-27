// Hero: the assembled board as a live object, which stays where it is.
// It leans a little towards the cursor (or the finger). Right-drag, or two fingers, orbit the camera; pinch zooms, within limits.
// A tap lifts a piece and plays it; a long press or double tap brings a piece to the middle and the rest make room.
// Left alone, a piece twitches now and then.
import {
  WebGLRenderer, Scene, PerspectiveCamera, Group, Raycaster, Vector2, Vector3, Box3, Sphere, Quaternion,
  DirectionalLight, PMREMGenerator, SRGBColorSpace, NeutralToneMapping, PCFShadowMap, Color,
} from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

const fig = document.querySelector('.hero figure');
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

async function main() {
  const renderer = new WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = NeutralToneMapping;
  renderer.toneMappingExposure = 1;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFShadowMap;

  const scene = new Scene();
  scene.background = new Color(0x000000);
  scene.environment = new PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;

  const key = new DirectionalLight(0xffffff, 2.2);
  key.position.set(-0.55, 0.9, -0.35); // from behind, so lifted pieces throw their shadow towards the viewer
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, { left: -0.32, right: 0.32, top: 0.32, bottom: -0.32, near: 0.2, far: 2.5 });
  key.shadow.camera.updateProjectionMatrix();
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.0015;
  scene.add(key);

  // the model ships as a base64 module (.glb is not a type every host serves)
  const b64 = (await import(new URL('../models/puzzle.js', import.meta.url).href)).default;
  const bin = new Uint8Array(b64.length * 3 / 4 - (b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0));
  { const raw = atob(b64); for (let i = 0; i < bin.length; i++) bin[i] = raw.charCodeAt(i); }
  const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).parseAsync(bin.buffer, '');
  const board = gltf.scene;
  for (const name of ['frame', 'backing']) { const o = board.getObjectByName(name); o?.parent.remove(o); } // the pieces alone, on black
  board.position.sub(new Box3().setFromObject(board).getCenter(new Vector3()));
  const rig = new Group();
  rig.add(board);
  scene.add(rig);

  const pieces = [];
  board.traverse(o => {
    if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
    if (/^piece-\d+$/.test(o.name)) pieces.push(o); // the piece groups, not their print, edge and back meshes
  });
  // each piece lifts and tips about its own centre, along its own axis; its note falls as it grows
  rig.updateMatrixWorld(true);
  const bb = new Box3(), sz = new Vector3();
  pieces.forEach((p, i) => {
    const a = i * 2.39996;
    bb.setFromObject(p).getSize(sz);
    Object.assign(p.userData, {
      lift: 0, to: 0, tw: 0, size: sz.x * sz.z, c: board.worldToLocal(bb.getCenter(new Vector3())), axis: new Vector3(Math.cos(a), 0, Math.sin(a)),
      p0: p.position.clone(), q0: p.quaternion.clone(), s0: p.scale.clone(),
      off: new Vector3(), offTo: new Vector3(), s: 1, sTo: 1, rot: 0, rotTo: 0, at: 0, k: (Math.sin(i * 12.9898) * 43758.5453) % 1,
    });
  });
  const SCALE = [1, 9 / 8, 5 / 4, 3 / 2, 5 / 3];
  [...pieces].sort((a, b) => b.userData.size - a.userData.size).forEach((p, r, all) => {
    const deg = Math.round(14 * r / (all.length - 1));
    p.userData.f = 196 * 2 ** Math.floor(deg / 5) * SCALE[deg % 5];
  });
  const q = new Quaternion(), qs = new Quaternion(), v3 = new Vector3(), UP = new Vector3(0, 1, 0);
  const sphere = new Box3().setFromObject(board).getBoundingSphere(new Sphere());
  const mid = board.worldToLocal(new Vector3()); // the middle of the board, where a chosen piece goes

  // the camera orbits the board's centre; zoom stays between a little further out than the start and not too close
  const cam = new PerspectiveCamera(26, 1, 0.01, 20);
  const EL = 0.95, EL_LO = 0.3, EL_HI = 1.5, Z_LO = 0.85, Z_HI = 2;
  let d0 = 1, zoom = 1, el = EL, az = 0;
  const place = () => {
    const d = d0 / zoom, c = Math.cos(el);
    cam.position.set(Math.sin(az) * c * d, Math.sin(el) * d, Math.cos(az) * c * d);
    cam.lookAt(0, 0, 0);
    cam.updateMatrixWorld();
  };
  const frame = () => {
    const w = fig.clientWidth, h = fig.clientHeight;
    renderer.setSize(w, h, false);
    cam.aspect = w / h;
    const v = (cam.fov * Math.PI) / 360, hz = Math.atan(Math.tan(v) * cam.aspect);
    d0 = (sphere.radius * (cam.aspect < 1 ? 1.04 : 0.9)) / Math.sin(Math.min(v, hz));
    cam.updateProjectionMatrix();
    place();
  };
  const orbit = (dx, dy) => { az -= dx * 0.008; el = clamp(el + dy * 0.006, EL_LO, EL_HI); place(); };
  const scale = k => { zoom = clamp(zoom * k, Z_LO, Z_HI); place(); };

  const cv = renderer.domElement;
  cv.className = 'gl';
  cv.setAttribute('aria-label', 'The assembled board in 3D. It leans towards the cursor; right-drag or two fingers orbit, pinch zooms, tap a piece to lift it, press and hold or double-click one to bring it to the middle.');
  fig.append(cv);
  frame();
  new ResizeObserver(frame).observe(fig);

  const YAW = -0.42;
  let amp = 1, touched = false, home = false, last = performance.now();
  const ray = new Raycaster(), ndc = new Vector2(), look = new Vector2(), lookTo = new Vector2();
  const lean = new Quaternion(), qy = new Quaternion(), right = new Vector3(), upc = new Vector3();
  const aim = (x, y) => { const r = cv.getBoundingClientRect(); ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1); ray.setFromCamera(ndc, cam); };
  const gaze = (x, y) => { const r = cv.getBoundingClientRect(); lookTo.set(clamp(((x - r.left) / r.width) * 2 - 1, -1, 1), clamp(-(((y - r.top) / r.height) * 2 - 1), -1, 1)); };
  const pick = (x, y) => {
    aim(x, y);
    let o = ray.intersectObject(board, true)[0]?.object;
    while (o && !/^piece-\d+$/.test(o.name)) o = o.parent;
    return o;
  };
  const lift = (x, y) => {
    const o = pick(x, y); if (!o) return;
    const u = o.userData; u.to = u.to >= 1 ? 0 : 1; u.tw = 0;
    window.blip?.(u.f, u.to);
  };
  // a chosen piece glides to the middle, grows, floats and turns; the rest make room, nearest first
  let chosen = null;
  const gather = f => {
    const t = performance.now();
    chosen = f;
    for (const p of pieces) {
      const u = p.userData, dx = u.c.x - mid.x, dz = u.c.z - mid.z, r = Math.hypot(dx, dz) || 1e-3;
      u.tw = 0; u.at = f ? t + r * 1400 : t; u.rot = Math.atan2(Math.sin(u.rot), Math.cos(u.rot));
      if (!f) { u.to = 0; u.offTo.set(0, 0, 0); u.sTo = 1; u.rotTo = 0; }
      else if (p === f) { u.to = 0; u.at = t; u.offTo.set(mid.x - u.c.x, 0.08, mid.z - u.c.z); u.sTo = 2.2; }
      else { const push = 0.02 + 0.085 * Math.exp(-r / 0.1); u.offTo.set((dx / r) * push, -0.004, (dz / r) * push); u.sTo = 1; u.rotTo = u.k * 0.5 * push / 0.125; }
    }
  };
  const summon = (x, y) => { // long press or double tap: choose a piece, let it go, or put everything back
    const o = pick(x, y);
    if (o && o !== chosen) { gather(o); window.blip?.(o.userData.f, 1); }
    else if (chosen) { window.blip?.(chosen.userData.f, 0); gather(null); }
    else { home = true; gather(null); }
  };

  // left button or one finger: the board only leans towards it; right button or two fingers: orbit and pinch
  const pts = new Map();
  let one = null, two = null, rmb = null, multi = false, tap = 0, press = 0;
  const pair = () => { const [a, b] = [...pts.values()]; return { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, d: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1 }; };
  cv.addEventListener('pointerdown', e => {
    clearTimeout(press); home = false; touched = true; last = performance.now();
    if (e.pointerType === 'mouse' && e.button === 2) { rmb = { x: e.clientX, y: e.clientY }; cv.setPointerCapture(e.pointerId); cv.classList.add('orbit'); return; }
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (e.pointerType === 'mouse') cv.setPointerCapture(e.pointerId);
    if (pts.size === 1) {
      one = { x: e.clientX, y: e.clientY, moved: 0 }; multi = false; gaze(e.clientX, e.clientY);
      press = setTimeout(() => { if (one && !multi && one.moved < 8) { one.long = true; summon(one.x, one.y); } }, 450);
    } else { one = null; multi = true; two = pts.size === 2 ? pair() : null; cv.classList.add('orbit'); }
  });
  cv.addEventListener('pointermove', e => {
    if (rmb) { orbit(e.clientX - rmb.x, e.clientY - rmb.y); rmb.x = e.clientX; rmb.y = e.clientY; last = performance.now(); return; }
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (one) {
      one.moved += Math.abs(e.clientX - one.x) + Math.abs(e.clientY - one.y); one.x = e.clientX; one.y = e.clientY;
      if (one.moved >= 8) clearTimeout(press);
      gaze(e.clientX, e.clientY);
    } else if (two && pts.size === 2) {
      const n = pair();
      scale(n.d / two.d); orbit(n.x - two.x, n.y - two.y);
      two = n;
    }
    last = performance.now();
  });
  const end = e => {
    if (rmb && e.pointerType === 'mouse' && (e.button === 2 || e.type === 'pointercancel')) { rmb = null; cv.classList.remove('orbit'); return; }
    if (!pts.delete(e.pointerId)) return;
    clearTimeout(press);
    if (one && !multi && !one.long && one.moved < 6 && e.type === 'pointerup') {
      if (e.timeStamp - tap < 320) { summon(e.clientX, e.clientY); tap = 0; } else { tap = e.timeStamp; lift(e.clientX, e.clientY); }
    }
    one = null; two = pts.size === 2 ? pair() : null;
    if (!pts.size) { cv.classList.remove('orbit'); if (e.pointerType !== 'mouse') lookTo.set(0, 0); }
    last = performance.now();
  };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
  addEventListener('pointermove', e => { if (e.pointerType === 'mouse' && !rmb && !pts.size) gaze(e.clientX, e.clientY); }, { passive: true }); // the cursor anywhere on the page
  document.documentElement.addEventListener('mouseleave', () => lookTo.set(0, 0));
  cv.addEventListener('contextmenu', e => e.preventDefault()); // right-drag and long press are ours, not the menu's
  // two fingers belong to the board: the page must not scroll or zoom under them; Safari's own pinch is off here
  const block = e => { if (e.touches.length > 1) e.preventDefault(); };
  cv.addEventListener('touchstart', block, { passive: false });
  cv.addEventListener('touchmove', block, { passive: false });
  let gz = 1;
  cv.addEventListener('gesturestart', e => { e.preventDefault(); gz = 1; });
  cv.addEventListener('gesturechange', e => { e.preventDefault(); if (pts.size < 2) { scale(e.scale / gz); gz = e.scale; touched = true; } }); // trackpad pinch in Safari
  cv.addEventListener('wheel', e => { if (!e.ctrlKey) return; e.preventDefault(); scale(Math.exp(-e.deltaY * 0.01)); touched = true; }, { passive: false }); // trackpad pinch elsewhere

  let on = true, t0 = performance.now();
  const loop = now => {
    if (!on) return;
    requestAnimationFrame(loop);
    if (home) { // double tap or long press on nothing: the camera goes back to where it started
      const to = 2 * Math.PI * Math.round(az / (2 * Math.PI));
      az += (to - az) * 0.12; el += (EL - el) * 0.12; zoom += (1 - zoom) * 0.12; place();
      if (Math.abs(to - az) + Math.abs(EL - el) + Math.abs(zoom - 1) < 0.002) { az = to; el = EL; zoom = 1; place(); home = false; }
    }
    if (touched || still) amp *= 0.92;
    if (!still && now - last > 6000 && Math.random() < 0.004) { // left alone, the board is not quite still
      const u = pieces[(Math.random() * pieces.length) | 0].userData;
      if (!u.to) { u.to = 0.16; u.tw = now + 120 + Math.random() * 260; }
    }
    // the lean: a few degrees about the camera's own axes, so it always turns towards the cursor on screen
    look.lerp(lookTo, still ? 1 : 0.06);
    right.setFromMatrixColumn(cam.matrixWorld, 0); upc.setFromMatrixColumn(cam.matrixWorld, 1);
    lean.setFromAxisAngle(upc, look.x * 0.12).multiply(qy.setFromAxisAngle(right, -look.y * 0.12));
    rig.quaternion.copy(lean).multiply(qy.setFromAxisAngle(UP, YAW + Math.sin((now - t0) * 0.00035) * 0.09 * amp));
    for (const p of pieces) {
      const u = p.userData;
      if (u.tw && now > u.tw) { u.to = 0; u.tw = 0; }
      if (p === chosen && !still) { u.rotTo += 0.004; u.offTo.y = 0.08 + 0.005 * Math.sin(now * 0.0021); }
      const go = now >= u.at, g = still ? 1 : 0.07;
      if (u.lift === u.to && (!go || (u.s === u.sTo && u.rot === u.rotTo && u.off.equals(u.offTo)))) continue;
      u.lift += (u.to - u.lift) * (still ? 1 : 0.14);
      if (Math.abs(u.to - u.lift) < 0.002) u.lift = u.to;
      if (go) {
        u.off.lerp(u.offTo, g); if (u.off.distanceToSquared(u.offTo) < 1e-10) u.off.copy(u.offTo);
        u.s += (u.sTo - u.s) * g; if (Math.abs(u.sTo - u.s) < 1e-4) u.s = u.sTo;
        u.rot += (u.rotTo - u.rot) * g; if (Math.abs(u.rotTo - u.rot) < 1e-4) u.rot = u.rotTo;
      }
      // turn about its own centre, grow about it, then move: tip (lift) inside spin (rot)
      q.setFromAxisAngle(u.axis, u.lift * 0.5);
      qs.setFromAxisAngle(UP, u.rot).multiply(q);
      p.quaternion.copy(qs).multiply(u.q0);
      p.scale.copy(u.s0).multiplyScalar(u.s);
      p.position.copy(v3.copy(u.p0).sub(u.c).applyQuaternion(qs).multiplyScalar(u.s)).add(u.c).add(u.off);
      p.position.y += u.lift * 0.09;
    }
    renderer.render(scene, cam);
  };
  new IntersectionObserver(([e]) => { const was = on; on = e.isIntersecting; if (on && !was) requestAnimationFrame(loop); }).observe(fig);
  requestAnimationFrame(loop);
  requestAnimationFrame(() => fig.classList.add('live'));
}

main().catch(() => {}); // the still render stays if WebGL or the model is unavailable
