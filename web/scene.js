import * as THREE from "/vendor/three.module.min.js";

const canvas = document.getElementById("stage");
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const { damp } = THREE.MathUtils;
const TAU = Math.PI * 2;

const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setClearColor(0x000000, 0);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 60);
const CAMERA_HOME = new THREE.Vector3(0, 2.7, 5.9);
const LOOK = new THREE.Vector3(0, -0.12, 0);
camera.position.copy(CAMERA_HOME);
camera.lookAt(LOOK);

function studio() {
  const room = new THREE.Scene();
  room.add(new THREE.Mesh(
    new THREE.SphereGeometry(20, 32, 32),
    new THREE.MeshBasicMaterial({ color: 0x0b0c12, side: THREE.BackSide }),
  ));
  const panels = [
    { size: [9, 3], position: [0, 8, 1], rotation: [Math.PI / 2, 0, 0], color: 0xffffff, power: 2.4 },
    { size: [2, 8], position: [-9, 1, 2], rotation: [0, Math.PI / 2, 0], color: 0x6ee7c8, power: 0.9 },
    { size: [2, 8], position: [9, 1, 2], rotation: [0, -Math.PI / 2, 0], color: 0xf5b38a, power: 0.9 },
    { size: [10, 2.5], position: [0, 2, -10], rotation: [0, 0, 0], color: 0x8b93ff, power: 1.1 },
    { size: [5, 1.2], position: [0, 3, 10], rotation: [0, Math.PI, 0], color: 0xffffff, power: 0.8 },
  ];
  for (const { size, position, rotation, color, power } of panels) {
    const material = new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide });
    material.color.multiplyScalar(power);
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(...size), material);
    mesh.position.set(...position);
    mesh.rotation.set(...rotation);
    room.add(mesh);
  }
  const generator = new THREE.PMREMGenerator(renderer);
  const texture = generator.fromScene(room, 0.03).texture;
  generator.dispose();
  return texture;
}

function radialTexture(stops) {
  const size = 256;
  const surface = document.createElement("canvas");
  surface.width = size;
  surface.height = size;
  const context = surface.getContext("2d");
  const gradient = context.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  for (const [offset, color] of stops) gradient.addColorStop(offset, color);
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(surface);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

scene.environment = studio();

const glow = radialTexture([[0, "rgba(255,255,255,1)"], [0.18, "rgba(255,255,255,0.55)"], [0.45, "rgba(255,255,255,0.12)"], [1, "rgba(255,255,255,0)"]]);
const shadow = radialTexture([[0, "rgba(0,0,0,0.7)"], [0.35, "rgba(0,0,0,0.3)"], [0.7, "rgba(0,0,0,0.06)"], [1, "rgba(0,0,0,0)"]]);

const RADIUS = 1.18;
const MODELS = [
  { color: 0x6ee7c8, geometry: new THREE.SphereGeometry(0.46, 96, 96), angle: -TAU / 3 },
  { color: 0x8b93ff, geometry: new THREE.IcosahedronGeometry(0.58, 0), angle: TAU / 3, flat: true },
  { color: 0xf5b38a, geometry: new THREE.TorusGeometry(0.38, 0.15, 64, 160), angle: 0 },
];

const rig = new THREE.Group();
scene.add(rig);

const models = MODELS.map((spec, index) => {
  const tint = new THREE.Color(spec.color);
  const holder = new THREE.Group();
  holder.position.set(Math.sin(spec.angle) * RADIUS, 0, Math.cos(spec.angle) * RADIUS);

  const glass = new THREE.Mesh(spec.geometry, new THREE.MeshPhysicalMaterial({
    color: tint.clone().lerp(new THREE.Color(0xffffff), 0.55),
    metalness: 0,
    roughness: 0.07,
    transmission: 1,
    thickness: 1.1,
    ior: 1.5,
    attenuationColor: tint,
    attenuationDistance: 1.4,
    clearcoat: 1,
    clearcoatRoughness: 0.04,
    iridescence: 0.3,
    iridescenceIOR: 1.3,
    specularIntensity: 1,
    envMapIntensity: 1.35,
    emissive: tint,
    emissiveIntensity: 0,
    flatShading: Boolean(spec.flat),
  }));
  if (index === 2) glass.rotation.x = Math.PI / 2.6;

  const halo = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glow,
    color: tint,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  }));
  halo.scale.setScalar(2.1);

  holder.add(glass, halo);
  rig.add(holder);

  const linkGeometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const link = new THREE.Line(linkGeometry, new THREE.LineBasicMaterial({ color: tint, transparent: true, opacity: 0.1, depthWrite: false }));
  rig.add(link);

  const pulse = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glow,
    color: tint,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  }));
  pulse.scale.setScalar(0.32);
  rig.add(pulse);

  return {
    spec, tint, holder, glass, halo, link, pulse,
    scale: 1, haloOpacity: 0, glowMix: 0, linkOpacity: 0.1, pulseT: index / 3,
  };
});

const core = new THREE.Group();
const coreBall = new THREE.Mesh(
  new THREE.SphereGeometry(0.075, 32, 32),
  new THREE.MeshBasicMaterial({ color: 0xffffff }),
);
const coreGlow = new THREE.Sprite(new THREE.SpriteMaterial({
  map: glow,
  color: 0xc9ccff,
  transparent: true,
  opacity: 0.85,
  depthWrite: false,
  blending: THREE.AdditiveBlending,
}));
coreGlow.scale.setScalar(0.9);
const coreLight = new THREE.PointLight(0xaab0ff, 5, 4.5, 2);
core.add(coreBall, coreGlow, coreLight);
rig.add(core);

const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(3.3, 3.3),
  new THREE.MeshBasicMaterial({ map: shadow, transparent: true, depthWrite: false }),
);
floor.rotation.x = -Math.PI / 2;
floor.position.y = -0.82;
scene.add(floor);

const orbit = new THREE.Mesh(
  new THREE.RingGeometry(RADIUS - 0.006, RADIUS + 0.006, 256),
  new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.07, side: THREE.DoubleSide, depthWrite: false }),
);
orbit.rotation.x = -Math.PI / 2;
orbit.position.y = -0.8;
scene.add(orbit);

const key = new THREE.DirectionalLight(0xffffff, 0.8);
key.position.set(3, 6, 5);
scene.add(key, new THREE.AmbientLight(0x8890ff, 0.08));

const state = {
  phase: "idle",
  selected: -1,
  since: 0,
  spin: 0,
  rotation: 0,
  pointerX: 0,
  pointerY: 0,
  hero: document.body.dataset.mode !== "chat",
};

window.addEventListener("pointermove", (event) => {
  state.pointerX = event.clientX / window.innerWidth - 0.5;
  state.pointerY = event.clientY / window.innerHeight - 0.5;
});

window.addEventListener("router:phase", (event) => {
  const { phase, model } = event.detail || {};
  state.phase = phase === "error" ? "idle" : phase;
  state.selected = phase === "done" ? model : state.selected;
  if (phase === "routing" || phase === "idle" || phase === "error") state.selected = -1;
  state.since = performance.now() / 1000;
  if (reduced) renderOnce();
  else start();
});

new MutationObserver(() => {
  state.hero = document.body.dataset.mode !== "chat";
}).observe(document.body, { attributes: true, attributeFilter: ["data-mode"] });

function nearestAngle(current, target) {
  return target + Math.round((current - target) / TAU) * TAU;
}

const worldTarget = new THREE.Vector3();
const linkPoint = new THREE.Vector3();

function update(dt, time, still) {
  const selected = state.phase === "done" && state.selected >= 0 && state.selected < models.length
    ? models[state.selected]
    : null;
  if (state.phase === "done" && time - state.since > 4) {
    state.phase = "idle";
  }
  const routing = state.phase === "routing" || state.phase === "writing";

  if (selected) {
    const target = nearestAngle(state.rotation, -selected.spec.angle);
    state.rotation = still ? target : damp(state.rotation, target, 3.2, dt);
  } else if (!still) {
    state.spin = damp(state.spin, routing ? 1.1 : 0.16, 2, dt);
    state.rotation += state.spin * dt;
  }
  rig.rotation.y = state.rotation;

  const pulseRate = routing ? 7 : 1.6;
  const breathe = still ? 0 : Math.sin(time * pulseRate);
  const coreHome = new THREE.Vector3(0, 0.02 + (still ? 0 : Math.sin(time * 1.1) * 0.04), 0);
  if (selected) {
    worldTarget.copy(selected.holder.position).multiplyScalar(0.58);
    worldTarget.y += 0.05;
  } else {
    worldTarget.copy(coreHome);
  }
  const follow = still ? 1 : 1 - Math.exp(-4 * dt);
  core.position.lerp(worldTarget, follow);
  coreGlow.scale.setScalar((selected ? 1.1 : 0.9) + breathe * (routing ? 0.18 : 0.06));
  coreLight.intensity = (routing ? 7 : 5) + breathe * 1.5;
  coreLight.color.lerp(selected ? selected.tint : new THREE.Color(0xaab0ff), still ? 1 : 0.06);

  models.forEach((model, index) => {
    const isSelected = selected === model;
    const bob = still ? 0 : Math.sin(time * 1.2 + index * 2.1) * 0.06;
    model.holder.position.y = bob;
    if (!still) {
      model.glass.rotation.y += dt * (0.25 + index * 0.08);
      if (index === 1) model.glass.rotation.x += dt * 0.12;
    }

    const sweep = routing ? Math.max(0, Math.sin(time * 5 - index * (TAU / 3))) : 0;
    const scaleTarget = selected ? (isSelected ? 1.14 : 0.9) : 1 + sweep * 0.05;
    const haloTarget = selected ? (isSelected ? 0.55 : 0) : routing ? sweep * 0.35 : 0.08;
    const glowTarget = selected ? (isSelected ? 1 : 0) : routing ? sweep : 0;
    const linkTarget = selected ? (isSelected ? 0.55 : 0.04) : routing ? 0.18 + sweep * 0.3 : 0.1;
    const rate = still ? 1 : 1 - Math.exp(-6 * dt);
    model.scale += (scaleTarget - model.scale) * rate;
    model.haloOpacity += (haloTarget - model.haloOpacity) * rate;
    model.glowMix += (glowTarget - model.glowMix) * rate;
    model.linkOpacity += (linkTarget - model.linkOpacity) * rate;

    model.glass.scale.setScalar(model.scale);
    model.halo.material.opacity = model.haloOpacity;
    model.glass.material.emissiveIntensity = model.glowMix * 0.35;
    model.glass.material.envMapIntensity = selected && !isSelected ? 0.8 : 1.35;

    const positions = model.link.geometry.attributes.position;
    positions.setXYZ(0, core.position.x, core.position.y, core.position.z);
    positions.setXYZ(1, model.holder.position.x, model.holder.position.y, model.holder.position.z);
    positions.needsUpdate = true;
    model.link.material.opacity = model.linkOpacity;

    if (routing && !still) {
      model.pulseT = (model.pulseT + dt * 0.9) % 1;
      linkPoint.copy(core.position).lerp(model.holder.position, model.pulseT);
      model.pulse.position.copy(linkPoint);
      model.pulse.material.opacity = Math.sin(model.pulseT * Math.PI) * 0.9;
    } else {
      model.pulse.material.opacity *= still ? 0 : 0.85;
    }
  });

  const parallax = state.hero && !still ? 1 : 0;
  camera.position.x = damp(camera.position.x, CAMERA_HOME.x + state.pointerX * 0.9 * parallax, 3, still ? 10 : dt);
  camera.position.y = damp(camera.position.y, CAMERA_HOME.y - state.pointerY * 0.5 * parallax, 3, still ? 10 : dt);
  camera.lookAt(LOOK);
}

let running = false;
let last = 0;

function tick(now) {
  if (!running) return;
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  update(dt, now / 1000, false);
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

function start() {
  if (reduced || running || document.hidden) return;
  running = true;
  last = performance.now();
  requestAnimationFrame(tick);
}

function renderOnce() {
  update(1, performance.now() / 1000, true);
  renderer.render(scene, camera);
}

function resize() {
  const width = canvas.clientWidth || 1;
  const height = canvas.clientHeight || 1;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  if (reduced || !running) renderOnce();
}

new ResizeObserver(resize).observe(canvas);

document.addEventListener("visibilitychange", () => {
  if (document.hidden) running = false;
  else start();
});

resize();
if (reduced) renderOnce();
else start();
