import { Sequence, pickVariant, drawCover, clamp } from './scrub.js';

const ASSETS = 'assets/';
const params = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);

const smoothstep = (a, b, x) => {
  const t = clamp((x - a) / (b - a), 0, 1);
  return t * t * (3 - 2 * t);
};

const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const wantsStill = () => params.has('still') || (reducedMotion.matches && !params.has('motion'));
const canScrub = () => 'createImageBitmap' in window && 'fetch' in window;

boot();

async function boot() {
  let manifest;
  try {
    const res = await fetch(`${ASSETS}manifest.json`);
    if (!res.ok) throw new Error(res.status);
    manifest = await res.json();
  } catch (err) {
    document.body.classList.remove('is-loading');
    console.error('Could not load the build manifest', err);
    return;
  }

  if (wantsStill() || !canScrub()) {
    showStill(manifest);
  } else {
    startBuild(manifest);
  }
  // Switching the OS setting swaps presentation without a stale half-state.
  reducedMotion.addEventListener('change', () => location.reload());
}

/* ---------- Still presentation: the finished tower, no scrubbing ---------- */

function captionMarkup(caption) {
  return `<p class="caption__numeral">${caption.numeral}</p>
    <h2 class="caption__title">${caption.title}</h2>
    <p class="caption__line">${caption.line}</p>`;
}

function showStill(manifest) {
  document.body.classList.remove('is-loading');
  document.body.classList.add('is-still');
  $('still-hero-mobile').srcset = ASSETS + manifest.finale.mobile;
  $('still-hero').src = ASSETS + manifest.finale.desktop;
  $('moments').innerHTML = manifest.segments
    .filter((seg) => seg.caption)
    .map((seg) => `<li class="moment">
        <img src="${ASSETS}${seg.poster.thumb}" alt="" loading="lazy" width="480" height="270">
        ${captionMarkup(seg.caption)}
      </li>`)
    .join('');
  $('still').hidden = false;
}

/* ---------- The scroll build ---------- */

function startBuild(manifest) {
  const build = $('build');
  const stage = $('stage');
  const canvas = $('canvas');
  const poster = $('poster');
  const overlay = $('overlay');
  const title = $('title');
  const hint = $('hint');
  const heightEl = $('height');
  const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });

  const total = manifest.totalFrames;
  const segments = manifest.segments;
  const captioned = segments.filter((seg) => seg.caption);
  const touch = matchMedia('(pointer: coarse)').matches;
  const saveData = navigator.connection && navigator.connection.saveData;

  $('subtitle').textContent = manifest.subtitle;
  $('hint-text').textContent = manifest.hint;

  // Captions, progress ticks and a text outline for assistive tech, all from the manifest.
  const captionEls = captioned.map((seg) => {
    const el = document.createElement('article');
    el.className = 'caption';
    el.innerHTML = captionMarkup(seg.caption);
    $('captions').appendChild(el);
    return el;
  });
  const tickEls = captioned.map((seg) => {
    const el = document.createElement('span');
    el.className = 'progress__tick';
    el.style.bottom = `${(seg.caption.in / (total - 1)) * 100}%`;
    $('rail').appendChild(el);
    return el;
  });
  $('outline').innerHTML = captioned
    .map((seg) => `<li>${seg.caption.title}: ${seg.caption.line}</li>`)
    .join('');

  let variant = params.get('variant') || pickVariant(manifest, innerWidth, innerHeight);
  let seq = null;
  let width = 0;
  let height = 0;
  let current = 0;
  let frame = -1;
  let dirty = true;
  let drawnExact = false;
  let ready = false;
  let tone = 'ink';
  let lastTime = performance.now();

  function layout() {
    const rect = stage.getBoundingClientRect();
    // The source is 720p: a backing store beyond ~1.5x its size only costs fill rate.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size = manifest.variants[variant];
    const scale = Math.min(dpr, (size.height * 1.5) / rect.height, (size.width * 2.2) / rect.width);
    width = Math.max(1, Math.round(rect.width * Math.max(scale, 0.5)));
    height = Math.max(1, Math.round(rect.height * Math.max(scale, 0.5)));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    ctx.imageSmoothingQuality = 'high';
    const perFrame = manifest.scrollPxPerFrame * clamp(innerHeight / 900, 0.72, 1.1);
    build.style.setProperty('--build-height', `${Math.round((total * perFrame) / (1 - manifest.holdEnd) + innerHeight)}px`);
    dirty = true;
  }

  function mount() {
    if (seq) seq.dispose();
    seq = new Sequence(manifest, variant, ASSETS, {
      radius: saveData ? 120 : Infinity,
      maxFetch: touch ? 5 : 8,
    });
    seq.onUpdate = (i) => {
      if (i < 0 ? !drawnExact : Math.abs(i - frame) <= 2) dirty = true;
    };
    seq.focus = Math.max(0, frame);

    // Loading is part of the scene: the empty Champ de Mars sharpens into view
    // while the rule beneath the date draws itself in.
    poster.src = ASSETS + (variant === 'mobile' ? manifest.opening.mobile : manifest.opening.desktop);
    poster.decode().catch(() => {}).then(() => poster.classList.add('is-in'));

    const loading = seq;
    seq.loadPreviews((p) => overlay.style.setProperty('--load', p.toFixed(3))).then(() => {
      if (loading !== seq) return;
      ready = true;
      overlay.style.setProperty('--load', 1);
      document.body.classList.remove('is-loading');
      document.body.classList.add('is-ready');
      canvas.classList.add('is-in');
      setTimeout(() => poster.classList.add('is-out'), 700);
      seq.startStreaming();
      seq.decodeAround();
      dirty = true;
      updateOverlay(Math.max(0, frame));
    });
  }

  function targetFrame() {
    const rect = build.getBoundingClientRect();
    const span = rect.height - innerHeight;
    const p = span > 0 ? clamp(-rect.top / span, 0, 1) : 0;
    return Math.min(1, p / (1 - manifest.holdEnd)) * (total - 1);
  }

  function updateOverlay(f) {
    const { s, seg, local } = seq.locate(f);
    const t = seg.count > 1 ? local / (seg.count - 1) : 1;

    overlay.style.setProperty('--p', (f / (total - 1)).toFixed(4));
    overlay.style.setProperty('--title-opacity', (1 - smoothstep(4, 46, f)).toFixed(3));
    hint.classList.toggle('is-on', ready && f < 8);

    const metres = Math.round(seg.height[0] + (seg.height[1] - seg.height[0]) * smoothstep(0.1, 0.85, t));
    if (heightEl.textContent !== String(metres)) heightEl.textContent = metres;

    captioned.forEach((cs, k) => {
      const on = ready && f >= cs.caption.in && (cs.caption.out === null || f < cs.caption.out);
      captionEls[k].classList.toggle('is-on', on);
      tickEls[k].classList.toggle('is-passed', f >= cs.caption.in);
    });

    // Ink text over bright frames, cream over dark ones, with hysteresis so it never flickers.
    const samples = seg.luma[variant] || seg.luma.desktop;
    const luma = samples[Math.min(samples.length - 1, Math.round(local / manifest.preview.step))];
    const next = tone === 'ink' ? (luma < 112 ? 'cream' : 'ink') : (luma > 138 ? 'ink' : 'cream');
    if (next !== tone) {
      tone = next;
      overlay.dataset.tone = tone;
    }
    stage.dataset.segment = segments[s].id;
  }

  function draw(f) {
    const { image, exact } = seq.best(f);
    if (!image) return;
    const { seg, local } = seq.locate(f);
    let focus = 0.5;
    if (variant === 'desktop') {
      const keys = seg.focusX;
      const x = (seg.count > 1 ? local / (seg.count - 1) : 0) * (keys.length - 1);
      const i = Math.min(Math.floor(x), keys.length - 2);
      focus = keys[i] + (keys[i + 1] - keys[i]) * (x - i);
    }
    drawCover(ctx, image, width, height, focus);
    drawnExact = exact;
  }

  function tick(now) {
    const dt = Math.min(0.1, (now - lastTime) / 1000);
    lastTime = now;
    const target = targetFrame();
    // Ease toward the scroll position so the wheel's steps become one continuous move.
    current += (target - current) * (1 - Math.exp(-dt / (touch ? 0.07 : 0.13)));
    if (Math.abs(target - current) < 0.03) current = target;
    const f = clamp(Math.round(current), 0, total - 1);
    if (f !== frame) {
      frame = f;
      seq.setFocus(f);
      updateOverlay(f);
      dirty = true;
    }
    if (dirty) {
      dirty = false;
      draw(f);
    }
    requestAnimationFrame(tick);
  }

  let resizeTimer = 0;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const wanted = params.get('variant') || pickVariant(manifest, innerWidth, innerHeight);
      if (wanted !== variant) {
        variant = wanted;
        layout();
        mount();
      } else {
        layout();
      }
    }, 120);
  });

  layout();
  mount();
  current = targetFrame();
  requestAnimationFrame(tick);

  // Small handle for QA and debugging.
  window.__build = {
    manifest,
    get frame() { return frame; },
    get variant() { return variant; },
    get loaded() { return seq.loaded; },
    get ready() { return ready; },
    get exact() { return drawnExact; },
    scrollToFrame(f) {
      const span = build.getBoundingClientRect().height - innerHeight;
      scrollTo(0, build.offsetTop + (f / (total - 1)) * (1 - manifest.holdEnd) * span);
    },
  };
}
