// Hero: the assembled board as a live object. Drag to turn it, tap a piece to lift it out.
import {
  WebGLRenderer, Scene, PerspectiveCamera, Group, Raycaster, Vector2, Vector3, Box3, Sphere, Quaternion,
  DirectionalLight, PMREMGenerator, SRGBColorSpace, NeutralToneMapping, PCFShadowMap, Color,
} from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

const fig = document.querySelector('.hero figure');
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;

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
  // each piece lifts and tips about its own centre, along its own axis
  rig.updateMatrixWorld(true);
  pieces.forEach((p, i) => {
    const a = i * 2.39996;
    Object.assign(p.userData, { lift: 0, to: 0, c: board.worldToLocal(new Box3().setFromObject(p).getCenter(new Vector3())), axis: new Vector3(Math.cos(a), 0, Math.sin(a)), p0: p.position.clone(), q0: p.quaternion.clone() });
  });
  const q = new Quaternion(), v3 = new Vector3();
  const sphere = new Box3().setFromObject(board).getBoundingSphere(new Sphere());

  const cam = new PerspectiveCamera(26, 1, 0.01, 20);
  const PITCH = 0.95; // camera elevation, radians
  const frame = () => {
    const w = fig.clientWidth, h = fig.clientHeight;
    renderer.setSize(w, h, false);
    cam.aspect = w / h;
    const v = (cam.fov * Math.PI) / 360, hz = Math.atan(Math.tan(v) * cam.aspect);
    const d = (sphere.radius * (cam.aspect < 1 ? 1.04 : 0.9)) / Math.sin(Math.min(v, hz));
    cam.position.set(0, Math.sin(PITCH) * d, Math.cos(PITCH) * d);
    cam.lookAt(0, 0, 0);
    cam.updateProjectionMatrix();
  };

  const cv = renderer.domElement;
  cv.className = 'gl';
  cv.setAttribute('aria-label', 'The assembled board in 3D. Drag to turn it, tap a piece to lift it.');
  fig.append(cv);
  frame();
  new ResizeObserver(frame).observe(fig);

  // turn with a drag (horizontal on touch so the page still scrolls), tap a piece to lift it
  let yaw = -0.42, tilt = 0, spin = 0, drag = null, touched = false, amp = 1;
  const ray = new Raycaster(), ndc = new Vector2();
  cv.addEventListener('pointerdown', e => {
    drag = { x: e.clientX, y: e.clientY, moved: 0, mouse: e.pointerType === 'mouse' };
    if (drag.mouse) cv.setPointerCapture(e.pointerId);
    cv.classList.add('down');
  });
  cv.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
    spin = dx * 0.0065; yaw += spin; touched = true;
    if (drag.mouse) tilt = Math.max(-0.32, Math.min(0.32, tilt + dy * 0.004));
  });
  const end = e => {
    if (drag && drag.moved < 6 && e.type === 'pointerup') {
      const r = cv.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(ndc, cam);
      let o = ray.intersectObject(board, true)[0]?.object;
      while (o && !/^piece-/.test(o.name)) o = o.parent;
      if (o) { o.userData.to = o.userData.to ? 0 : 1; touched = true; }
    }
    drag = null;
    cv.classList.remove('down');
  };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);

  let on = true, t0 = performance.now();
  const loop = now => {
    if (!on) return;
    requestAnimationFrame(loop);
    if (!drag) { yaw += spin; spin *= 0.94; }
    if (touched || still) amp *= 0.92;
    const sway = Math.sin((now - t0) * 0.00035) * 0.09 * amp;
    rig.rotation.set(tilt, yaw + sway, 0, 'YXZ');
    for (const p of pieces) {
      const u = p.userData;
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
