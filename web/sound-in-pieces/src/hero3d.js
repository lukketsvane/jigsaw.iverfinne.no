// Hero: the assembled board as a live object.
// One finger turns it (the page still scrolls vertically), two fingers pinch, twist and move it,
// a tap lifts a piece and plays it, a double tap puts everything back. Left alone, a piece twitches now and then.
import {
  WebGLRenderer, Scene, PerspectiveCamera, Group, Raycaster, Vector2, Vector3, Box3, Sphere, Quaternion, Plane,
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
  board.position.sub(new Box3().setFromObject(board).getCenter(new Vector3()));
  const rig = new Group();
  rig.add(board);
  scene.add(rig);

  const pieces = [];
  board.traverse(o => {
    if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
    if (/^piece-/.test(o.name)) pieces.push(o);
  });
  // each piece lifts and tips about its own centre, along its own axis; its note falls as it grows
  rig.updateMatrixWorld(true);
  const bb = new Box3(), sz = new Vector3();
  pieces.forEach((p, i) => {
    const a = i * 2.39996;
    bb.setFromObject(p).getSize(sz);
    Object.assign(p.userData, { lift: 0, to: 0, tw: 0, size: sz.x * sz.z, c: board.worldToLocal(bb.getCenter(new Vector3())), axis: new Vector3(Math.cos(a), 0, Math.sin(a)), p0: p.position.clone(), q0: p.quaternion.clone() });
  });
  const SCALE = [1, 9 / 8, 5 / 4, 3 / 2, 5 / 3];
  [...pieces].sort((a, b) => b.userData.size - a.userData.size).forEach((p, r, all) => {
    const deg = Math.round(14 * r / (all.length - 1));
    p.userData.f = 196 * 2 ** Math.floor(deg / 5) * SCALE[deg % 5];
  });
  const q = new Quaternion(), v3 = new Vector3();
  const sphere = new Box3().setFromObject(board).getBoundingSphere(new Sphere());

  const cam = new PerspectiveCamera(26, 1, 0.01, 20);
  const PITCH = 0.95; // camera elevation, radians
  let d0 = 1, zoom = 1;
  const place = () => {
    const d = d0 / zoom;
    cam.position.set(0, Math.sin(PITCH) * d, Math.cos(PITCH) * d);
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

  const cv = renderer.domElement;
  cv.className = 'gl';
  cv.setAttribute('aria-label', 'The assembled board in 3D. Drag to turn it, pinch to look closer, tap a piece to lift it.');
  fig.append(cv);
  frame();
  new ResizeObserver(frame).observe(fig);

  const HOME = -0.42;
  let yaw = HOME, tilt = 0, spin = 0, amp = 1, touched = false, home = false, last = performance.now();
  const pan = new Vector3(), ray = new Raycaster(), ndc = new Vector2(), ground = new Plane(new Vector3(0, 1, 0), 0), va = new Vector3(), vb = new Vector3();
  const aim = (x, y) => { const r = cv.getBoundingClientRect(); ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1); ray.setFromCamera(ndc, cam); };
  const at = (x, y, out) => { aim(x, y); return ray.ray.intersectPlane(ground, out); };
  const fence = () => { const m = sphere.radius * 0.85 * (1 - 1 / zoom), l = Math.hypot(pan.x, pan.z); if (l > m) { pan.x *= m / l; pan.z *= m / l; } };
  const turn = (da, cx, cz) => { // the board turns about a point on the table, so whatever is under the fingers stays there
    yaw += da;
    const c = Math.cos(da), s = Math.sin(da), x = pan.x - cx, z = pan.z - cz;
    pan.x = cx + c * x + s * z; pan.z = cz - s * x + c * z;
  };
  const zoomAt = (x, y, k) => {
    const A = at(x, y, va); zoom = clamp(zoom * k, 0.8, 3.2); place();
    const B = at(x, y, vb); if (A && B) { pan.x += B.x - A.x; pan.z += B.z - A.z; }
    fence();
  };
  const lift = (x, y) => {
    aim(x, y);
    let o = ray.intersectObject(board, true)[0]?.object;
    while (o && !/^piece-/.test(o.name)) o = o.parent;
    if (!o) return;
    const u = o.userData; u.to = u.to >= 1 ? 0 : 1; u.tw = 0;
    window.blip?.(u.f, u.to);
  };
  const reset = () => { home = true; for (const p of pieces) { p.userData.to = 0; p.userData.tw = 0; } };

  // one pointer turns (and tilts, with a mouse); two pinch, twist and move; a tap lifts; a double tap resets
  const pts = new Map();
  let one = null, two = null, multi = false, tap = 0;
  const pair = () => { const [a, b] = [...pts.values()]; return { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, d: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, a: Math.atan2(b[1] - a[1], b[0] - a[0]) }; };
  cv.addEventListener('pointerdown', e => {
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (e.pointerType === 'mouse') cv.setPointerCapture(e.pointerId);
    if (pts.size === 1) { one = { x: e.clientX, y: e.clientY, moved: 0, mouse: e.pointerType === 'mouse' }; multi = false; }
    else { one = null; multi = true; two = pts.size === 2 ? pair() : null; }
    home = false; last = performance.now();
    cv.classList.add('down');
  });
  cv.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (one) {
      const dx = e.clientX - one.x, dy = e.clientY - one.y;
      one.x = e.clientX; one.y = e.clientY; one.moved += Math.abs(dx) + Math.abs(dy);
      spin = (dx * 0.0065) / Math.sqrt(zoom); turn(spin, 0, 0); touched = true;
      if (one.mouse) tilt = clamp(tilt + dy * 0.004, -0.32, 0.32);
    } else if (two && pts.size === 2) {
      const n = pair();
      zoomAt(two.x, two.y, n.d / two.d);
      const A = at(two.x, two.y, va), B = at(n.x, n.y, vb);
      if (A && B) { pan.x += B.x - A.x; pan.z += B.z - A.z; turn(-Math.atan2(Math.sin(n.a - two.a), Math.cos(n.a - two.a)), B.x, B.z); }
      fence(); two = n; spin = 0; touched = true;
    }
    last = performance.now();
  });
  const end = e => {
    if (!pts.delete(e.pointerId)) return;
    if (one && !multi && one.moved < 6 && e.type === 'pointerup') {
      if (e.timeStamp - tap < 320) { reset(); tap = 0; } else { tap = e.timeStamp; lift(e.clientX, e.clientY); }
    }
    one = null; two = pts.size === 2 ? pair() : null;
    if (!pts.size) cv.classList.remove('down');
    last = performance.now();
  };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
  // the page must not scroll or zoom under two fingers; Safari's own pinch is turned off here
  const hold = e => { if (e.touches.length > 1) e.preventDefault(); };
  cv.addEventListener('touchstart', hold, { passive: false });
  cv.addEventListener('touchmove', hold, { passive: false });
  let gz = 1;
  cv.addEventListener('gesturestart', e => { e.preventDefault(); gz = 1; });
  cv.addEventListener('gesturechange', e => { e.preventDefault(); if (pts.size < 2) { zoomAt(e.clientX, e.clientY, e.scale / gz); gz = e.scale; touched = true; } }); // trackpad pinch in Safari
  cv.addEventListener('wheel', e => { if (!e.ctrlKey) return; e.preventDefault(); zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.01)); touched = true; }, { passive: false }); // trackpad pinch elsewhere

  let on = true, t0 = performance.now();
  const loop = now => {
    if (!on) return;
    requestAnimationFrame(loop);
    if (!one && !two) { if (Math.abs(spin) > 1e-5) turn(spin, 0, 0); spin *= 0.94; }
    if (home) {
      const to = HOME + 2 * Math.PI * Math.round((yaw - HOME) / (2 * Math.PI));
      yaw += (to - yaw) * 0.12; tilt *= 0.88; pan.multiplyScalar(0.88); zoom += (1 - zoom) * 0.12; place();
      if (Math.abs(to - yaw) + Math.abs(zoom - 1) + pan.length() < 0.002) { yaw = to; zoom = 1; pan.set(0, 0, 0); place(); home = false; }
    }
    if (touched || still) amp *= 0.92;
    if (!still && now - last > 6000 && Math.random() < 0.004) { // left alone, the board is not quite still
      const u = pieces[(Math.random() * pieces.length) | 0].userData;
      if (!u.to) { u.to = 0.16; u.tw = now + 120 + Math.random() * 260; }
    }
    const sway = Math.sin((now - t0) * 0.00035) * 0.09 * amp;
    rig.position.copy(pan);
    rig.rotation.set(tilt, yaw + sway, 0, 'YXZ');
    for (const p of pieces) {
      const u = p.userData;
      if (u.tw && now > u.tw) { u.to = 0; u.tw = 0; }
      if (u.lift !== u.to) {
        u.lift += (u.to - u.lift) * (still ? 1 : 0.14);
        if (Math.abs(u.to - u.lift) < 0.002) u.lift = u.to;
        q.setFromAxisAngle(u.axis, u.lift * 0.5);
        p.quaternion.copy(q).multiply(u.q0);
        p.position.copy(v3.copy(u.p0).sub(u.c).applyQuaternion(q)).add(u.c);
        p.position.y += u.lift * 0.09;
      }
    }
    renderer.render(scene, cam);
  };
  new IntersectionObserver(([e]) => { const was = on; on = e.isIntersecting; if (on && !was) requestAnimationFrame(loop); }).observe(fig);
  requestAnimationFrame(loop);
  requestAnimationFrame(() => fig.classList.add('live'));
}

main().catch(() => {}); // the still render stays if WebGL or the model is unavailable
