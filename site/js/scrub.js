// Frame-sequence store for the scroll scrubber.
//
// Two tiers come from the build manifest:
//   preview  every Nth frame at low resolution - small enough to load up front,
//            so the whole build can be scrubbed straight away
//   full     every frame - streamed nearest-first around the current position
//
// Full frames are kept as compressed blobs and only decoded inside a small
// window around the playhead, which keeps memory flat on phones.

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const pad = (n) => String(n).padStart(4, '0');

export function pickVariant(manifest, width, height) {
  const mobile = manifest.variants.mobile;
  return mobile && mobile.maxAspect && width / height <= mobile.maxAspect ? 'mobile' : 'desktop';
}

export class Sequence {
  constructor(manifest, variant, base, options = {}) {
    this.manifest = manifest;
    this.variant = variant;
    this.base = base;
    this.total = manifest.totalFrames;
    this.size = manifest.variants[variant];
    this.step = manifest.preview.step;
    this.segments = manifest.segments;

    this.segmentOf = new Uint8Array(this.total);
    this.previewStart = [];
    let previews = 0;
    this.segments.forEach((seg, s) => {
      this.segmentOf.fill(s, seg.start, seg.start + seg.count);
      this.previewStart.push(previews);
      previews += seg.previewCount;
    });
    this.previews = new Array(previews).fill(null);

    this.blobs = new Array(this.total).fill(null);
    this.state = new Uint8Array(this.total); // 0 idle, 1 loading, 2 ready
    this.tries = new Uint8Array(this.total);
    this.bitmaps = new Map();
    this.decoding = new Set();
    this.loaded = 0;

    this.focus = 0;
    this.direction = 1;
    this.active = 0;
    this.maxFetch = options.maxFetch ?? 6;
    this.radius = options.radius ?? Infinity; // how far from the playhead to prefetch
    this.ahead = options.ahead ?? (variant === 'mobile' ? 14 : 12);
    this.behind = options.behind ?? 6;
    this.onUpdate = () => {};
    this.disposed = false;
    this.streaming = false;
  }

  locate(i) {
    const s = this.segmentOf[i];
    return { s, seg: this.segments[s], local: i - this.segments[s].start };
  }

  frameUrl(i) {
    const { seg, local } = this.locate(i);
    return `${this.base}${seg.dir}/${this.variant}/${pad(local + 1)}.webp`;
  }

  previewIndex(i) {
    const { s, seg, local } = this.locate(i);
    return this.previewStart[s] + Math.min(seg.previewCount - 1, Math.round(local / this.step));
  }

  // Load the whole preview tier. Resolves once every preview is decoded.
  async loadPreviews(onProgress = () => {}) {
    const jobs = [];
    this.segments.forEach((seg, s) => {
      for (let k = 0; k < seg.previewCount; k++) {
        jobs.push({ slot: this.previewStart[s] + k, url: `${this.base}${seg.dir}/${this.variant}-p/${pad(k + 1)}.webp` });
      }
    });
    let done = 0;
    let next = 0;
    const worker = async () => {
      while (next < jobs.length && !this.disposed) {
        const job = jobs[next++];
        try {
          const res = await fetch(job.url);
          if (!res.ok) throw new Error(res.status);
          this.previews[job.slot] = await createImageBitmap(await res.blob());
        } catch (err) {
          // A missing preview only costs sharpness while scrubbing fast.
        }
        done++;
        onProgress(done / jobs.length);
        this.onUpdate(-1);
      }
    };
    await Promise.all(Array.from({ length: 8 }, worker));
  }

  startStreaming() {
    this.streaming = true;
    this.pump();
  }

  setFocus(i) {
    if (i === this.focus) return;
    this.direction = i > this.focus ? 1 : -1;
    this.focus = i;
    if (this.streaming) this.pump();
    this.decodeAround();
  }

  // Nearest frame that still needs fetching, looking ahead of the scroll first.
  nextWanted() {
    const limit = Math.min(this.total, this.radius);
    for (let d = 0; d < limit; d++) {
      const a = this.focus + d * this.direction;
      if (a >= 0 && a < this.total && this.state[a] === 0) return a;
      const b = this.focus - d * this.direction;
      if (d && b >= 0 && b < this.total && this.state[b] === 0) return b;
    }
    return -1;
  }

  pump() {
    while (!this.disposed && this.active < this.maxFetch && this.loaded < this.total) {
      const i = this.nextWanted();
      if (i < 0) return;
      this.state[i] = 1;
      this.active++;
      fetch(this.frameUrl(i))
        .then((res) => {
          if (!res.ok) throw new Error(res.status);
          return res.blob();
        })
        .then((blob) => {
          this.blobs[i] = blob;
          this.state[i] = 2;
          this.loaded++;
          if (Math.abs(i - this.focus) <= this.ahead) this.decodeAround();
        })
        .catch(() => {
          // Retry a few times, then leave the frame to the preview tier.
          this.state[i] = ++this.tries[i] < 3 ? 0 : 3;
          if (this.state[i] === 3) this.loaded++;
        })
        .finally(() => {
          this.active--;
          setTimeout(() => this.pump(), 0);
        });
    }
  }

  decodeAround() {
    if (this.disposed) return;
    const lo = this.direction > 0 ? this.focus - this.behind : this.focus - this.ahead;
    const hi = this.direction > 0 ? this.focus + this.ahead : this.focus + this.behind;
    for (const [i, bitmap] of this.bitmaps) {
      if (i < lo - 6 || i > hi + 6) {
        bitmap.close();
        this.bitmaps.delete(i);
      }
    }
    const order = [];
    for (let i = Math.max(0, lo); i <= Math.min(this.total - 1, hi); i++) {
      if (this.state[i] === 2 && !this.bitmaps.has(i) && !this.decoding.has(i)) order.push(i);
    }
    order.sort((a, b) => Math.abs(a - this.focus) - Math.abs(b - this.focus));
    for (const i of order) {
      if (this.decoding.size >= 4) break;
      this.decoding.add(i);
      createImageBitmap(this.blobs[i])
        .then((bitmap) => {
          if (this.disposed) return bitmap.close();
          this.bitmaps.set(i, bitmap);
          this.onUpdate(i);
        })
        .catch(() => {})
        .finally(() => {
          this.decoding.delete(i);
          this.decodeAround();
        });
    }
  }

  // Best image available for frame i: the exact frame, a close neighbour, or a preview.
  best(i) {
    const exact = this.bitmaps.get(i);
    if (exact) return { image: exact, exact: true };
    for (let d = 1; d <= 2; d++) {
      const near = this.bitmaps.get(i + d) || this.bitmaps.get(i - d);
      if (near) return { image: near, exact: false };
    }
    const p = this.previewIndex(i);
    for (let d = 0; d < 6; d++) {
      const image = this.previews[p - d] || this.previews[p + d];
      if (image) return { image, exact: false };
    }
    return { image: null, exact: false };
  }

  dispose() {
    this.disposed = true;
    for (const bitmap of this.bitmaps.values()) bitmap.close();
    for (const bitmap of this.previews) if (bitmap) bitmap.close();
    this.bitmaps.clear();
    this.blobs = [];
  }
}

// Draw an image to fill the canvas ("cover"), keeping focusX of the image in view.
export function drawCover(ctx, image, width, height, focusX = 0.5) {
  const iw = image.width;
  const ih = image.height;
  const scale = Math.max(width / iw, height / ih);
  const sw = width / scale;
  const sh = height / scale;
  const sx = clamp(focusX * iw - sw / 2, 0, iw - sw);
  const sy = (ih - sh) / 2;
  ctx.drawImage(image, sx, sy, sw, sh, 0, 0, width, height);
}

export { clamp };
