import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const views = {
  orbit: [69, 58, 105],
  side: [0, 22, 123],
  top: [0, 139, 0.1],
};
const forward = new THREE.Vector3(1, 0, 0);

function addEnvironment(scene, { width, height, depth, feed_point: feedPoint, feed_radius: feedRadius }) {
  const glassShape = new THREE.BoxGeometry(width, height, depth);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(glassShape),
    new THREE.LineBasicMaterial({ color: 0x91c8bc, transparent: true, opacity: 0.49 }),
  );
  glassShape.dispose();
  edges.position.y = height / 2;
  scene.add(edges);

  const floor = new THREE.Mesh(
    new THREE.BoxGeometry(width, 1.2, depth),
    new THREE.MeshStandardMaterial({ color: 0x284843, roughness: 0.94 }),
  );
  floor.position.y = -0.6;
  scene.add(floor);

  const surface = new THREE.Mesh(
    new THREE.PlaneGeometry(width, depth),
    new THREE.MeshBasicMaterial({
      color: 0x82b8ac, transparent: true, opacity: 0.075, side: THREE.DoubleSide, depthWrite: false,
    }),
  );
  surface.rotation.x = -Math.PI / 2;
  surface.position.y = height;
  scene.add(surface);

  const feedGeometry = new THREE.BufferGeometry().setFromPoints(
    Array.from({ length: 48 }, (_, index) => {
      const angle = index / 48 * Math.PI * 2;
      return new THREE.Vector3(Math.cos(angle) * feedRadius, 0, Math.sin(angle) * feedRadius);
    }),
  );
  const feedMaterial = new THREE.LineBasicMaterial({ color: 0xe6b674, transparent: true, opacity: 0.52 });
  const feedRing = new THREE.LineLoop(feedGeometry, feedMaterial);
  feedRing.position.set(...feedPoint);
  feedRing.visible = false;
  scene.add(feedRing);
  return feedRing;
}

function createFishParts() {
  const body = new THREE.SphereGeometry(1, 32, 24);
  const position = body.attributes.position;
  const colors = [];
  const stripe = new THREE.Color(0x315f70);
  const scales = new THREE.Color(0x9ae9d8);
  for (let index = 0; index < position.count; index += 1) {
    const x = position.getX(index);
    const y = position.getY(index);
    const taper = 0.85 + x * 0.23;
    position.setXYZ(index, x * 2.65, y * 1.12 * taper, position.getZ(index) * 0.72 * taper);
    const blend = Math.max(0, Math.min(1, 0.5 + 1.05 * Math.sin((y + 0.03) * 16)));
    const color = stripe.clone().lerp(scales, x > 0.62 ? 0.72 : blend);
    colors.push(color.r, color.g, color.b);
  }
  body.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  body.computeVertexNormals();

  function shape(points) {
    const outline = new THREE.Shape();
    outline.moveTo(...points[0]);
    for (const point of points.slice(1)) outline.lineTo(...point);
    outline.closePath();
    return new THREE.ShapeGeometry(outline);
  }

  return {
    body,
    tail: shape([[0, 0], [-2.2, 1.25], [-1.65, 0], [-2.2, -1.13], [0, 0]]),
    dorsal: shape([[-1.78, 0.44], [-0.88, 1.3], [0.23, 0.76], [-1.78, 0.44]]),
    eye: new THREE.SphereGeometry(0.24, 10, 8),
    bodyMaterial: new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.59 }),
    grayBody: new THREE.MeshStandardMaterial({ color: 0x949b9d, roughness: 0.72 }),
    finMaterial: new THREE.MeshBasicMaterial({ color: 0x71c9bc, side: THREE.DoubleSide }),
    grayFin: new THREE.MeshBasicMaterial({ color: 0x777e80, side: THREE.DoubleSide }),
    eyeMaterial: new THREE.MeshBasicMaterial({ color: 0x12222d }),
  };
}

function createFish(parts) {
  const group = new THREE.Group();
  const body = new THREE.Mesh(parts.body, parts.bodyMaterial);
  const tail = new THREE.Mesh(parts.tail, parts.finMaterial);
  tail.position.x = -2.35;
  const dorsal = new THREE.Mesh(parts.dorsal, parts.finMaterial);
  group.add(body, tail, dorsal);
  for (const side of [-1, 1]) {
    const eye = new THREE.Mesh(parts.eye, parts.eyeMaterial);
    eye.position.set(1.64, 0.19, side * 0.57);
    group.add(eye);
  }
  return {
    group, body, tail, dorsal,
    target: new THREE.Vector3(),
    orientation: new THREE.Quaternion(),
    speed: 0, lost: false,
  };
}

export function createTank(container, dimensions) {
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.7));
  renderer.domElement.setAttribute("aria-label", "斑馬魚即時位置的 3D 魚缸");
  container.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 1, 340);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.minDistance = 44;
  controls.maxDistance = 205;
  const feedRing = addEnvironment(scene, dimensions);
  const ambient = new THREE.HemisphereLight(0xe2fcff, 0x457a65, 1.55);
  const lamp = new THREE.DirectionalLight(0xd9ffee, 2.35);
  lamp.position.set(20, 70, 36);
  const rim = new THREE.DirectionalLight(0x7ab9ff, 0.7);
  rim.position.set(-34, 22, -20);
  const highlight = new THREE.Mesh(
    new THREE.RingGeometry(3.64, 3.82, 40),
    new THREE.MeshBasicMaterial({ color: 0xffdb88, depthTest: false, depthWrite: false, transparent: true, opacity: 0.9 }),
  );
  highlight.visible = false;
  highlight.renderOrder = 4;
  scene.add(ambient, lamp, rim, highlight);
  const parts = createFishParts();
  const fishById = new Map();
  const direction = new THREE.Vector3();
  let runId;
  let lightOn = true;
  let feeding = false;
  let selectedId = "";
  let inViewport = true;
  const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
  let animationTime = performance.now();

  function setView(view) {
    controls.target.set(0, dimensions.height * 0.44, 0);
    camera.position.set(...views[view]);
    controls.update();
  }

  new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect;
    if (!width || !height) return;
    camera.aspect = width / height;
    camera.fov = Math.max(34, 44 / camera.aspect);
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  }).observe(container);
  new IntersectionObserver(([entry]) => { inViewport = entry.isIntersecting; }).observe(container);

  function update(state) {
    if (state.run_id !== runId) {
      for (const fish of fishById.values()) scene.remove(fish.group);
      fishById.clear();
      runId = state.run_id;
    }
    lightOn = state.controls.light_on !== false;
    feeding = state.controls.last_feed_ms !== null && state.timestamp_ms - state.controls.last_feed_ms < 14_000;
    const visible = new Set();
    for (const track of state.fish) {
      if (!track.position) continue;
      visible.add(track.id);
      let fish = fishById.get(track.id);
      if (!fish) {
        fish = createFish(parts);
        fish.group.position.set(track.position.x, track.position.y, track.position.z);
        fishById.set(track.id, fish);
        scene.add(fish.group);
      }
      fish.target.set(track.position.x, track.position.y, track.position.z);
      fish.lost = !state.tracking_fresh || track.status === "lost";
      fish.body.material = fish.lost ? parts.grayBody : parts.bodyMaterial;
      fish.tail.material = fish.dorsal.material = fish.lost ? parts.grayFin : parts.finMaterial;
      if (track.velocity && !fish.lost) {
        direction.set(...track.velocity);
        fish.speed = direction.length();
        if (fish.speed > 0.25) fish.orientation.setFromUnitVectors(forward, direction.normalize());
      } else {
        fish.speed = 0;
      }
    }
    for (const [id, fish] of fishById) {
      if (visible.has(id)) continue;
      scene.remove(fish.group);
      fishById.delete(id);
    }
  }

  function setOffline() {
    feeding = false;
    for (const fish of fishById.values()) {
      fish.lost = true;
      fish.body.material = parts.grayBody;
      fish.tail.material = fish.dorsal.material = parts.grayFin;
    }
  }

  setView("orbit");
  renderer.setAnimationLoop((time) => {
    const delta = Math.min((time - animationTime) / 1000, 0.1);
    animationTime = time;
    if (!inViewport) return;
    const smoothing = 1 - Math.exp(-12 * Math.max(0, delta));
    lamp.intensity += ((lightOn ? 2.35 : 0.15) - lamp.intensity) * smoothing;
    ambient.intensity += ((lightOn ? 1.55 : 0.65) - ambient.intensity) * smoothing;
    const pulse = motionPreference.matches ? 0 : Math.sin(time / 340) * 0.16;
    feedRing.visible = feeding;
    feedRing.material.opacity = 0.82 + pulse;
    let index = 0;
    for (const fish of fishById.values()) {
      fish.group.position.lerp(fish.target, smoothing);
      if (!fish.lost) fish.group.quaternion.slerp(fish.orientation, smoothing);
      fish.tail.rotation.y = fish.lost || motionPreference.matches
        ? 0 : Math.sin(time * 0.012 + index) * Math.min(0.65, 0.15 + fish.speed * 0.026);
      index += 1;
    }
    controls.update();
    const selected = fishById.get(selectedId);
    highlight.visible = Boolean(selected);
    if (selected) {
      highlight.position.copy(selected.group.position);
      highlight.quaternion.copy(camera.quaternion);
    }
    renderer.render(scene, camera);
  });
  return { update, setView, setOffline, selectFish(id) { selectedId = id; } };
}
