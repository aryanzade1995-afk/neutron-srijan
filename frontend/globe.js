/* Procedural "connected globe" backdrop.
   Renders a rotating night-side Earth: clustered city lights, glowing great-circle
   arcs with travelling pulses, and an atmospheric rim. Drawn on canvas so it stays
   sharp at any resolution and carries no stock-image licensing.

   To use a licensed photo instead: set --hero-image on .hero-bg in styles.css and
   add data-globe="off" to the canvas. */

(function () {
  const canvas = document.getElementById('globe');
  if (!canvas || canvas.dataset.globe === 'off') return;

  const ctx = canvas.getContext('2d');
  const TAU = Math.PI * 2;

  let W = 0, H = 0, DPR = 1;
  let cx = 0, cy = 0, R = 0;

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- geometry ----------

  function sphere(lat, lon) {
    const cl = Math.cos(lat);
    return { x: cl * Math.cos(lon), y: Math.sin(lat), z: cl * Math.sin(lon) };
  }

  function rotateY(p, a) {
    const s = Math.sin(a), c = Math.cos(a);
    return { x: p.x * c + p.z * s, y: p.y, z: -p.x * s + p.z * c };
  }

  // Tilt the pole slightly so the globe reads as a planet, not a circle.
  const TILT = -0.38;
  function rotateX(p, a) {
    const s = Math.sin(a), c = Math.cos(a);
    return { x: p.x, y: p.y * c - p.z * s, z: p.y * s + p.z * c };
  }

  function project(p) {
    return { x: cx + p.x * R, y: cy - p.y * R, z: p.z };
  }

  // ---------- world generation ----------

  const rand = (() => {
    let seed = 26;                       // fixed seed - the backdrop is identical every load
    return () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
  })();

  // Landmass-ish clusters: metro centres with lights scattered around them.
  const HUBS = [];
  for (let i = 0; i < 112; i++) {
    // bias toward northern mid-latitudes, where the reference image is brightest
    const lat = (rand() * 1.5 - 0.42) * 0.95;
    const lon = rand() * TAU;
    HUBS.push({ lat, lon, size: 0.35 + rand() * 0.9 });
  }

  const LIGHTS = [];
  for (const hub of HUBS) {
    const n = 20 + Math.floor(rand() * 74 * hub.size);
    for (let i = 0; i < n; i++) {
      const spread = 0.05 + rand() * 0.22 * hub.size;
      const ang = rand() * TAU;
      const lat = hub.lat + Math.cos(ang) * spread * (0.5 + rand());
      const lon = hub.lon + Math.sin(ang) * spread * (0.9 + rand()) / Math.max(0.25, Math.cos(hub.lat));
      LIGHTS.push({
        lat, lon,
        r: 0.6 + rand() * 1.9,
        warm: rand(),
        twinkle: rand() * TAU,
      });
    }
  }

  // A faint lat/lon lattice under the lights, like the reference grid.
  const GRID = [];
  for (let la = -80; la <= 80; la += 10) {
    for (let lo = 0; lo < 360; lo += 5) {
      GRID.push({ lat: la * Math.PI / 180, lon: lo * Math.PI / 180 });
    }
  }

  // Great-circle arcs between hubs.
  const ARCS = [];
  for (let i = 0; i < 30; i++) {
    const a = HUBS[Math.floor(rand() * HUBS.length)];
    const b = HUBS[Math.floor(rand() * HUBS.length)];
    if (a === b) continue;
    ARCS.push({
      a, b,
      lift: 0.10 + rand() * 0.30,
      speed: 0.10 + rand() * 0.26,
      phase: rand(),
      width: 0.7 + rand() * 1.5,
    });
  }

  function slerp(p, q, t) {
    let dot = p.x * q.x + p.y * q.y + p.z * q.z;
    dot = Math.max(-1, Math.min(1, dot));
    const omega = Math.acos(dot);
    if (omega < 1e-4) return p;
    const so = Math.sin(omega);
    const s1 = Math.sin((1 - t) * omega) / so;
    const s2 = Math.sin(t * omega) / so;
    return { x: p.x * s1 + q.x * s2, y: p.y * s1 + q.y * s2, z: p.z * s1 + q.z * s2 };
  }

  // ---------- sizing ----------

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth;
    H = canvas.clientHeight;
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);

    // Anchor the horizon at a fixed fraction of the hero height rather than
    // deriving it from the centre, so the rim stays visible under the nav at
    // every viewport while the sphere itself stays huge.
    R = Math.max(W, H) * 1.06;
    cx = W * 0.42;
    cy = H * 0.23 + R;
  }

  // ---------- drawing ----------

  function drawAtmosphere() {
    const glow = ctx.createRadialGradient(cx, cy, R * 0.70, cx, cy, R * 1.20);
    glow.addColorStop(0, 'rgba(28, 96, 200, 0.00)');
    glow.addColorStop(0.58, 'rgba(52, 134, 245, 0.38)');
    glow.addColorStop(0.82, 'rgba(120, 190, 255, 0.62)');
    glow.addColorStop(1, 'rgba(14, 44, 96, 0)');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.20, 0, TAU);
    ctx.fill();

    const body = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.42, R * 0.05, cx, cy, R);
    body.addColorStop(0, 'rgba(24, 60, 118, 0.97)');
    body.addColorStop(0.55, 'rgba(12, 34, 74, 0.97)');
    body.addColorStop(1, 'rgba(5, 16, 40, 0.99)');
    ctx.fillStyle = body;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, TAU);
    ctx.fill();

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, TAU);
    ctx.clip();
    const rim = ctx.createRadialGradient(cx, cy, R * 0.80, cx, cy, R);
    rim.addColorStop(0, 'rgba(80, 160, 255, 0)');
    rim.addColorStop(0.72, 'rgba(96, 172, 255, 0.28)');
    rim.addColorStop(1, 'rgba(168, 216, 255, 0.85)');
    ctx.fillStyle = rim;
    ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
    ctx.restore();
  }

  function drawGrid(spin) {
    ctx.fillStyle = 'rgba(120, 180, 250, 0.34)';
    for (const g of GRID) {
      const p = rotateX(rotateY(sphere(g.lat, g.lon), spin), TILT);
      if (p.z <= 0.02) continue;
      const s = project(p);
      if (s.x < -20 || s.x > W + 20 || s.y < -20 || s.y > H + 20) continue;
      ctx.globalAlpha = 0.18 + p.z * 0.52;
      ctx.fillRect(s.x, s.y, 1, 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawLights(spin, t) {
    for (const l of LIGHTS) {
      const p = rotateX(rotateY(sphere(l.lat, l.lon), spin), TILT);
      if (p.z <= 0.03) continue;
      const s = project(p);
      if (s.x < -30 || s.x > W + 30 || s.y < -30 || s.y > H + 30) continue;

      const flicker = 0.78 + 0.22 * Math.sin(t * 1.6 + l.twinkle);
      const depth = Math.pow(p.z, 0.7);
      const alpha = Math.min(1, depth * flicker * 1.25);
      const radius = l.r * (0.58 + depth * 0.82);

      // warm amber city cores, cooler blue-white on the fringes
      const warm = l.warm;
      const col = warm > 0.40
        ? `rgba(255, ${162 + warm * 56 | 0}, ${78 + warm * 66 | 0}, ${alpha})`
        : `rgba(${170 + warm * 120 | 0}, ${215 + warm * 40 | 0}, 255, ${alpha * 0.85})`;

      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.arc(s.x, s.y, radius, 0, TAU);
      ctx.fill();

      if (radius > 1.5) {
        ctx.globalAlpha = alpha * 0.15;
        ctx.beginPath();
        ctx.arc(s.x, s.y, radius * 2.6, 0, TAU);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
  }

  function drawArcs(spin, t) {
    const STEPS = 44;
    for (const arc of ARCS) {
      const a = rotateX(rotateY(sphere(arc.a.lat, arc.a.lon), spin), TILT);
      const b = rotateX(rotateY(sphere(arc.b.lat, arc.b.lon), spin), TILT);
      if (a.z < -0.35 && b.z < -0.35) continue;

      const pts = [];
      for (let i = 0; i <= STEPS; i++) {
        const u = i / STEPS;
        const m = slerp(a, b, u);
        const lift = 1 + Math.sin(u * Math.PI) * arc.lift;
        pts.push(project({ x: m.x * lift, y: m.y * lift, z: m.z * lift }));
      }

      const visible = pts.filter(p => p.z > -0.25).length / pts.length;
      if (visible < 0.15) continue;

      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';

      // soft outer glow
      ctx.strokeStyle = `rgba(80, 165, 255, ${0.20 * visible})`;
      ctx.lineWidth = arc.width * 5;
      strokePath(pts);

      ctx.strokeStyle = `rgba(150, 212, 255, ${0.85 * visible})`;
      ctx.lineWidth = arc.width;
      strokePath(pts);

      // travelling pulse along the arc
      const head = (t * arc.speed + arc.phase) % 1;
      const idx = Math.floor(head * STEPS);
      const tail = pts.slice(Math.max(0, idx - 7), idx + 1);
      if (tail.length > 1) {
        ctx.strokeStyle = `rgba(235, 248, 255, ${1.0 * visible})`;
        ctx.lineWidth = arc.width * 1.5;
        strokePath(tail);

        const tip = pts[idx];
        if (tip && tip.z > -0.2) {
          ctx.fillStyle = `rgba(235, 248, 255, ${0.9 * visible})`;
          ctx.beginPath();
          ctx.arc(tip.x, tip.y, arc.width * 1.5, 0, TAU);
          ctx.fill();
          ctx.fillStyle = `rgba(140, 205, 255, ${0.34 * visible})`;
          ctx.beginPath();
          ctx.arc(tip.x, tip.y, arc.width * 7, 0, TAU);
          ctx.fill();
        }
      }
    }
  }

  function strokePath(pts) {
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    ctx.stroke();
  }

  function drawStars(t) {
    ctx.fillStyle = 'rgba(205, 230, 255, 0.75)';
    for (let i = 0; i < 90; i++) {
      const x = ((i * 97.31) % 100) / 100 * W;
      const y = ((i * 41.77) % 100) / 100 * H * 0.72;
      const d = Math.hypot(x - cx, y - cy);
      if (d < R * 1.02) continue;
      ctx.globalAlpha = 0.18 + 0.3 * Math.abs(Math.sin(t * 0.7 + i));
      ctx.fillRect(x, y, 1.2, 1.2);
    }
    ctx.globalAlpha = 1;
  }

  // ---------- loop ----------

  let start = performance.now();
  function frame(now) {
    const t = (now - start) / 1000;
    const spin = reduceMotion ? 0.6 : t * 0.026;

    ctx.clearRect(0, 0, W, H);
    drawStars(t);
    drawAtmosphere();
    drawGrid(spin);
    drawArcs(spin, t);
    drawLights(spin, t);

    requestAnimationFrame(frame);
  }

  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();
  requestAnimationFrame(frame);
})();
