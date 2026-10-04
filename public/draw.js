// "Wet sketch-pen" page painting.
// 1. Ink: find the outlines in the finished illustration and draw them stroke by stroke.
// 2. Wash: let the colors bleed in from a few spots like wet watercolor, wobbling until they settle.
// No AI here: the illustration is generated once, and the browser animates it.

const PAPER = "#fbf5e8";
const INK = "rgba(44, 34, 30, 0.92)";
const GRID = 210; // resolution used to find outlines

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

function seeded(seed) {
  let s = seed >>> 0 || 1;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

// Sobel edges + thinning, then link neighbouring edge pixels into pen strokes.
function traceStrokes(img) {
  const c = document.createElement("canvas");
  c.width = c.height = GRID;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.filter = "blur(0.7px)";
  ctx.drawImage(img, 0, 0, GRID, GRID);
  const px = ctx.getImageData(0, 0, GRID, GRID).data;
  const L = new Float32Array(GRID * GRID);
  for (let i = 0; i < L.length; i++) L[i] = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];

  const mag = new Float32Array(L.length), dir = new Uint8Array(L.length);
  for (let y = 1; y < GRID - 1; y++) {
    for (let x = 1; x < GRID - 1; x++) {
      const i = y * GRID + x;
      const gx = -L[i - GRID - 1] - 2 * L[i - 1] - L[i + GRID - 1] + L[i - GRID + 1] + 2 * L[i + 1] + L[i + GRID + 1];
      const gy = -L[i - GRID - 1] - 2 * L[i - GRID] - L[i - GRID + 1] + L[i + GRID - 1] + 2 * L[i + GRID] + L[i + GRID + 1];
      mag[i] = Math.hypot(gx, gy);
      const a = ((Math.atan2(gy, gx) * 180) / Math.PI + 180) % 180;
      dir[i] = a < 22.5 || a >= 157.5 ? 0 : a < 67.5 ? 1 : a < 112.5 ? 2 : 3;
    }
  }
  const sorted = Array.from(mag).filter((v) => v > 0).sort((a, b) => a - b);
  const thr = sorted[Math.floor(sorted.length * 0.83)] || 1;
  const offs = [[1, 0], [1, 1], [0, 1], [-1, 1]];
  const edge = new Uint8Array(L.length);
  for (let y = 1; y < GRID - 1; y++) {
    for (let x = 1; x < GRID - 1; x++) {
      const i = y * GRID + x;
      if (mag[i] < thr) continue;
      const [dx, dy] = offs[dir[i]];
      if (mag[i] >= mag[i + dy * GRID + dx] && mag[i] >= mag[i - dy * GRID - dx]) edge[i] = 1;
    }
  }

  const seen = new Uint8Array(L.length);
  const nb = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
  const strokes = [];
  for (let y = 1; y < GRID - 1; y++) {
    for (let x = 1; x < GRID - 1; x++) {
      if (!edge[y * GRID + x] || seen[y * GRID + x]) continue;
      const pts = [[x, y]];
      seen[y * GRID + x] = 1;
      let cx = x, cy = y, last = -1;
      for (;;) {
        let best = -1, bestCost = 9;
        for (let k = 0; k < 8; k++) {
          const nx = cx + nb[k][0], ny = cy + nb[k][1];
          if (nx < 1 || ny < 1 || nx >= GRID - 1 || ny >= GRID - 1) continue;
          const j = ny * GRID + nx;
          if (!edge[j] || seen[j]) continue;
          const cost = last < 0 ? 0 : Math.min((k - last + 8) % 8, (last - k + 8) % 8);
          if (cost < bestCost) { bestCost = cost; best = k; }
        }
        if (best < 0) break;
        cx += nb[best][0]; cy += nb[best][1]; last = best;
        seen[cy * GRID + cx] = 1;
        pts.push([cx, cy]);
      }
      if (pts.length >= 4) strokes.push(smooth(pts));
    }
  }
  // Big contours first, the way an illustrator blocks in shapes before details.
  strokes.sort((a, b) => b.length - a.length);
  return strokes;
}

function smooth(pts) {
  return pts.map((p, i) => {
    const a = pts[Math.max(0, i - 1)], b = pts[Math.min(pts.length - 1, i + 1)];
    return [(a[0] + 2 * p[0] + b[0]) / 4, (a[1] + 2 * p[1] + b[1]) / 4];
  });
}

function makeBlobs(strokes, W, rand) {
  const blobs = [];
  const n = 7;
  for (let i = 0; i < n; i++) {
    const s = strokes[Math.floor(rand() * Math.min(strokes.length, 40))] || [[GRID / 2, GRID / 2]];
    const p = s[Math.floor(rand() * s.length)];
    const parts = Array.from({ length: 10 }, () => ({
      dx: (rand() - 0.5) * 0.5, dy: (rand() - 0.5) * 0.5, r: 0.55 + rand() * 0.6,
    }));
    blobs.push({ x: (p[0] / GRID) * W, y: (p[1] / GRID) * W, delay: rand() * 0.35, speed: 0.8 + rand() * 0.5, parts });
  }
  return blobs;
}

const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

// Paints `src` onto `canvas`. Returns {done: Promise, cancel()}.
export function paintPage(canvas, src, opts = {}) {
  const { inkMs = 3400, washMs = 3600, onWash = () => {}, seed = 7 } = opts;
  let cancelled = false;
  const done = (async () => {
    const img = await loadImage(src);
    // Draw at the size it is shown, not at the image's full resolution: far less work per frame.
    const shown = Math.round((canvas.clientWidth || 512) * Math.min(2, window.devicePixelRatio || 1));
    canvas.width = canvas.height = Math.max(256, Math.min(1024, shown));
    const W = canvas.width;
    const ctx = canvas.getContext("2d");
    if (reduceMotion()) {
      ctx.drawImage(img, 0, 0, W, W);
      onWash();
      return;
    }
    const strokes = traceStrokes(img);
    const rand = seeded(seed);
    const scale = W / GRID;

    const colour = document.createElement("canvas");
    colour.width = colour.height = W;
    colour.getContext("2d").drawImage(img, 0, 0, W, W);
    const ink = document.createElement("canvas");
    ink.width = ink.height = W;
    const ictx = ink.getContext("2d");
    ictx.strokeStyle = INK;
    ictx.lineCap = ictx.lineJoin = "round";
    ictx.lineWidth = Math.max(1.2, scale * 0.42);

    const total = strokes.reduce((n, s) => n + s.length, 0) || 1;
    let si = 0, pi = 0, drawn = 0;
    const frame = () => new Promise((r) => requestAnimationFrame(r));

    // ---- ink phase
    const t0 = performance.now();
    for (;;) {
      if (cancelled) return;
      const t = Math.min(1, (performance.now() - t0) / inkMs);
      const target = Math.floor(easeInOut(t) * total);
      while (drawn < target && si < strokes.length) {
        const s = strokes[si];
        if (pi === 0) { pi = 1; }
        const a = s[pi - 1], b = s[pi];
        const j = () => (rand() - 0.5) * scale * 0.25;
        ictx.beginPath();
        ictx.moveTo(a[0] * scale + j(), a[1] * scale + j());
        ictx.lineTo(b[0] * scale + j(), b[1] * scale + j());
        ictx.stroke();
        drawn++; pi++;
        if (pi >= s.length) { si++; pi = 0; drawn++; }
      }
      ctx.fillStyle = PAPER;
      ctx.fillRect(0, 0, W, W);
      ctx.drawImage(ink, 0, 0);
      if (t >= 1) break;
      await frame();
    }

    // ---- wash phase
    onWash();
    // The wash mask is drawn at quarter size and stretched: cheaper, and the stretch softens the edges.
    const M = Math.round(W / 4);
    const mask = document.createElement("canvas");
    mask.width = mask.height = M;
    const mctx = mask.getContext("2d");
    mctx.setTransform(M / W, 0, 0, M / W, 0, 0);
    const tmp = document.createElement("canvas");
    tmp.width = tmp.height = W;
    const tctx = tmp.getContext("2d");
    const blobs = makeBlobs(strokes, W, rand);
    const disp = document.getElementById("wet-disp");
    canvas.style.filter = disp ? "url(#wet)" : "";
    const t1 = performance.now();
    for (;;) {
      if (cancelled) { canvas.style.filter = ""; return; }
      const t = Math.min(1, (performance.now() - t1) / washMs);
      mctx.clearRect(0, 0, W, W);
      for (const b of blobs) {
        const p = Math.max(0, Math.min(1, (t - b.delay) / (1 - b.delay))) * b.speed;
        if (p <= 0) continue;
        const R = easeInOut(Math.min(1, p)) * W * 1.05;
        for (const part of b.parts) {
          const x = b.x + part.dx * R, y = b.y + part.dy * R, r = R * part.r;
          const g = mctx.createRadialGradient(x, y, r * 0.35, x, y, r);
          g.addColorStop(0, "rgba(0,0,0,1)");
          g.addColorStop(1, "rgba(0,0,0,0)");
          mctx.fillStyle = g;
          mctx.beginPath();
          mctx.arc(x, y, r, 0, Math.PI * 2);
          mctx.fill();
        }
      }
      if (t > 0.85) { // make sure the corners fill in
        mctx.globalAlpha = (t - 0.85) / 0.15;
        mctx.fillRect(0, 0, W, W);
        mctx.globalAlpha = 1;
      }
      tctx.globalCompositeOperation = "source-over";
      tctx.clearRect(0, 0, W, W);
      tctx.drawImage(colour, 0, 0);
      tctx.globalCompositeOperation = "destination-in";
      tctx.drawImage(mask, 0, 0, W, W);

      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = PAPER;
      ctx.fillRect(0, 0, W, W);
      ctx.drawImage(tmp, 0, 0);
      ctx.globalAlpha = 1 - easeInOut(t);
      ctx.globalCompositeOperation = "multiply";
      ctx.drawImage(ink, 0, 0);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      if (disp) disp.setAttribute("scale", String(9 * (1 - t)));
      if (t >= 1) break;
      await frame();
    }
    canvas.style.filter = "";
    ctx.drawImage(colour, 0, 0);
  })();
  return { done, cancel: () => { cancelled = true; } };
}
