#!/usr/bin/env python3
"""Slice the generated clips into scroll frames and write the site manifest.

Reads   manifest.src.json            the single source manifest
        assets-src/clips/*.mp4       one clip per segment (from the image/video MCP)
        assets-src/stills/*.jpg      boundary stills (from the image/video MCP)
Writes  site/assets/frames/<segment>-<hash>/<variant>/NNNN.webp
        site/assets/frames/<segment>-<hash>/<variant>-p/NNNN.webp   (preview tier)
        site/assets/posters/<still>-<hash>[-m|-t].webp
        site/assets/manifest.json    everything the site needs; nothing is hand-keyed

Output directories are named by a hash of the clip bytes and every setting that
affects the pixels, so a re-run skips unchanged segments and browsers can cache
frames forever.

Requires ffmpeg/ffprobe on PATH, plus Pillow and numpy.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "manifest.src.json"
CLIPS = ROOT / "assets-src" / "clips"
STILLS = ROOT / "assets-src" / "stills"
OUT = ROOT / "site" / "assets"

# Caption regions, as fractions of the variant frame (x0, y0, x1, y1). The build
# samples their brightness so the site can pick ink or cream text per frame.
CAPTION_REGION = {"desktop": (0.04, 0.36, 0.36, 0.58), "mobile": (0.14, 0.10, 0.46, 0.28)}

# Each clip is generated between two boundary stills. The video model renders a
# slightly brighter picture than the stills and steps into and out of that look
# over its first and last two frames, so those frames are never used: a segment
# runs from the clip's first settled frame to the next clip's first settled frame.
SNAP = 2
BUILD_REV = 5  # bump to invalidate every cached output


def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else json.dumps(p, sort_keys=True).encode())
    return h.hexdigest()


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames",
        "-of", "json", str(path)])
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"width": s["width"], "height": s["height"],
            "fps": float(num) / float(den), "frames": int(s["nb_read_frames"])}


def read_frames(path, width, height):
    """Yield every frame of the clip as an RGB numpy array, scaled to width x height."""
    proc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", str(path),
        "-vf", f"scale={width}:{height}:flags=lanczos", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    size = width * height * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
    finally:  # also reached when the caller stops early
        proc.kill()
        proc.stdout.close()
        proc.wait()


def frame_at(path, index, width, height):
    for i, frame in enumerate(read_frames(path, width, height)):
        if i == index:
            return frame.copy()
    raise IndexError(f"{path.name} has no frame {index}")


def lerp_keys(keys, t):
    """Piecewise-linear interpolation over evenly spaced keys, t in 0..1."""
    if len(keys) == 1:
        return keys[0]
    x = min(max(t, 0.0), 1.0) * (len(keys) - 1)
    i = min(int(x), len(keys) - 2)
    return keys[i] + (keys[i + 1] - keys[i]) * (x - i)


def strip_x(focus, src_w, strip_w):
    return int(round(min(max(focus * src_w - strip_w / 2, 0), src_w - strip_w)))


# ---------- seam easing ----------

def _zoom(img, scale, tx=0.0, ty=0.0, resample=Image.BICUBIC):
    """Zoom a PIL image about its centre by scale, then shift by (tx, ty) pixels."""
    w, h = img.size
    cx, cy = w / 2, h / 2
    return img.transform((w, h), Image.AFFINE, (1 / scale, 0, cx - (cx + tx) / scale,
                                                0, 1 / scale, cy - (cy + ty) / scale), resample)


def _phase_shift(a, b):
    fa, fb = np.fft.rfft2(a - a.mean()), np.fft.rfft2(b - b.mean())
    r = np.conj(fa) * fb
    r /= np.abs(r) + 1e-6
    c = np.fft.irfft2(r, s=a.shape)
    y, x = np.unravel_index(np.argmax(c), c.shape)
    return (x - a.shape[1] if x > a.shape[1] // 2 else x), (y - a.shape[0] if y > a.shape[0] // 2 else y)


def estimate_align(pre, target):
    """Find the zoom and shift that best maps frame `pre` onto frame `target`.

    Returns ((scale, tx, ty), residual_before, residual_after) with the shift as
    a fraction of width and height. Falls back to the identity when no transform
    clearly beats doing nothing (a lighting-only difference, for example).
    """
    tw, th = 320, 180
    a = Image.fromarray(pre).convert("L").resize((tw, th), Image.BILINEAR)
    b = np.asarray(Image.fromarray(target).convert("L").resize((tw, th), Image.BILINEAR), dtype=np.float32)
    my, mx = int(th * 0.12), int(tw * 0.12)

    def score(scale):
        z = np.asarray(_zoom(a, scale), dtype=np.float32)
        dx, dy = _phase_shift(z, b)
        if abs(dx) > tw * 0.2 or abs(dy) > th * 0.2:
            dx = dy = 0
        moved = np.asarray(_zoom(a, scale, dx, dy), dtype=np.float32)
        return float(np.abs(moved - b)[my:-my, mx:-mx].mean()), dx, dy

    base = float(np.abs(np.asarray(a, dtype=np.float32) - b)[my:-my, mx:-mx].mean())
    best = (base, 1.0, 0, 0)
    for lo, hi, n in ((0.8, 1.4, 31), (None, None, 21)):
        if lo is None:
            lo, hi = best[1] - 0.02, best[1] + 0.02
        for scale in np.linspace(lo, hi, n):
            sc, dx, dy = score(float(scale))
            if sc < best[0]:
                best = (sc, float(scale), dx, dy)
    if best[0] > base * 0.85:
        return (1.0, 0.0, 0.0), base, base
    return (best[1], best[2] / tw, best[3] / th), base, best[0]


def ease_into(frame, target, align, w):
    """Move `frame` a fraction w of the way onto `target`: partial zoom/shift plus dissolve."""
    scale, tx, ty = align
    h, wd, _ = frame.shape
    moved = _zoom(Image.fromarray(frame).convert("RGBA"), scale ** w, tx * wd * w, ty * h * w)
    arr = np.asarray(moved, dtype=np.float32)
    alpha = arr[..., 3:4] / 255.0 * (1 - w)
    return (arr[..., :3] * alpha + target * (1 - alpha) + 0.5).astype(np.uint8)


# ---------- frame selection ----------

def pick_indices(path, info, out_fps, max_hold, trim):
    """Choose which source frames to keep, between the two settled ends of the clip.

    Resamples to out_fps and shortens a static head or tail to max_hold output
    frames, so a part that has already settled does not cost dead scroll distance.
    """
    thumbs = np.stack([f.astype(np.float32) for f in read_frames(path, 160, 90)])
    n = len(thumbs)
    first, last = SNAP, n - 1 - SNAP
    eps = 1.5
    d_last = np.abs(thumbs - thumbs[last]).mean(axis=(1, 2, 3))
    d_first = np.abs(thumbs - thumbs[first]).mean(axis=(1, 2, 3))
    tail = last
    while tail > first and d_last[tail - 1] < eps:
        tail -= 1
    head = first
    while head < last and d_first[head + 1] < eps:
        head += 1
    step = info["fps"] / out_fps
    hold = int(round(max_hold * step))
    a = max(first, head - hold) if trim else first
    b = min(last, tail + hold) if trim else last
    if b - a < step * 8:  # nearly static clip: keep it whole
        a, b = first, last
    count = max(2, int(round((b - a) / step)) + 1)
    idx = sorted({int(round(a + j * (b - a) / (count - 1))) for j in range(count)})
    return idx, {"sourceFrames": n, "staticHead": head - first, "staticTail": last - tail}


def save_webp(arr, path, quality):
    Image.fromarray(arr).save(path, "WEBP", quality=quality, method=4)


def region_luma(arr, region):
    h, w, _ = arr.shape
    x0, y0, x1, y1 = region
    crop = arr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].astype(np.float32)
    return float((crop * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).sum(axis=2).mean())


def build_segment(seg, clip, next_clip, cfg, is_first, force):
    variants, prev = cfg["variants"], cfg["preview"]
    info = probe(clip)
    settings = {"rev": BUILD_REV, "snap": SNAP, "fps": cfg["fps"], "maxHold": cfg.get("maxHold", 0),
                "variants": variants, "preview": prev, "focusX": seg["focusX"],
                "trim": seg.get("trim", True), "first": is_first,
                "endBlend": seg.get("endBlend"), "regions": CAPTION_REGION,
                "next": sha(next_clip.read_bytes())[:16] if next_clip else None}
    digest = sha(clip.read_bytes(), settings)[:10]
    name = f"{seg['id']}-{digest}"
    out = OUT / "frames" / name
    meta_path = out / "segment.json"
    if meta_path.exists() and not force:
        print(f"  {seg['id']}: cached ({name})")
        return json.loads(meta_path.read_text())

    if out.exists():
        shutil.rmtree(out)
    for v in variants:
        (out / v).mkdir(parents=True)
        (out / f"{v}-p").mkdir(parents=True)

    dw, dh = variants["desktop"]["width"], variants["desktop"]["height"]
    mw = variants["mobile"]["width"]
    pscale, pstep = prev["scale"], prev["step"]

    idx, trim_info = pick_indices(clip, info, cfg["fps"], cfg.get("maxHold", 0), seg.get("trim", True))
    if not is_first:  # the previous segment already ends on this clip's first settled frame
        idx = idx[1:]
    wanted = {s: j for j, s in enumerate(idx)}
    count = len(idx)

    # Arrive on the next clip's first settled frame. Where the two differ, the
    # last frames ease into it: a small zoom/shift where the framing is off,
    # plus a dissolve, over a distance that grows with the size of the gap.
    blend, target, align = 0, None, None
    if next_clip:
        target = frame_at(next_clip, SNAP, dw, dh)
        align, before, after = estimate_align(frame_at(clip, idx[-1], dw, dh), target)
        blend = seg.get("endBlend")
        if blend is None:
            blend = int(min(30, max(6, round(4 + 1.5 * before))))
        blend = min(int(blend), count - 1)
        trim_info["seam"] = {"frames": blend, "scale": round(align[0], 4),
                             "shift": [round(align[1], 4), round(align[2], 4)],
                             "gap": round(before, 2), "gapAligned": round(after, 2)}
        target = target.astype(np.float32)

    sizes = {v: 0 for v in list(variants) + [f"{v}-p" for v in variants]}
    luma = {v: [] for v in variants}
    for s, frame in enumerate(read_frames(clip, dw, dh)):
        j = wanted.get(s)
        if j is None:
            continue
        if blend and j > count - 1 - blend:
            w = (j - (count - 1 - blend)) / blend
            frame = ease_into(frame, target, align, w * w * (3 - 2 * w))
        t = s / max(1, info["frames"] - 1)
        x = strip_x(lerp_keys(seg["focusX"], t), dw, mw)
        for v, arr in (("desktop", frame), ("mobile", np.ascontiguousarray(frame[:, x:x + mw]))):
            p = out / v / f"{j + 1:04d}.webp"
            save_webp(arr, p, variants[v]["quality"])
            sizes[v] += p.stat().st_size
            if j % pstep == 0:
                small = Image.fromarray(arr).resize(
                    (max(2, int(arr.shape[1] * pscale)), max(2, int(arr.shape[0] * pscale))), Image.LANCZOS)
                pp = out / f"{v}-p" / f"{j // pstep + 1:04d}.webp"
                small.save(pp, "WEBP", quality=prev["quality"], method=4)
                sizes[f"{v}-p"] += pp.stat().st_size
                luma[v].append(round(region_luma(arr, CAPTION_REGION[v])))

    meta = {"id": seg["id"], "dir": f"frames/{name}", "count": count,
            "previewCount": (count + pstep - 1) // pstep, "bytes": sizes, "luma": luma,
            "source": {"clip": clip.name, "fps": info["fps"], **trim_info}}
    meta_path.write_text(json.dumps(meta, indent=1))
    seam = trim_info.get("seam")
    print(f"  {seg['id']}: {count} frames from {info['frames']} "
          f"(static head {trim_info['staticHead']}, tail {trim_info['staticTail']}"
          + (f", seam gap {seam['gap']} eased over {seam['frames']}" if seam else "") + "), "
          f"desktop {sizes['desktop'] / 1e6:.1f} MB, mobile {sizes['mobile'] / 1e6:.1f} MB", flush=True)
    return meta


def build_posters(src, cfg):
    """Boundary stills become posters: loading state, reduced-motion hero and caption art."""
    out = OUT / "posters"
    out.mkdir(parents=True, exist_ok=True)
    variants = cfg["variants"]
    dw, dh = variants["desktop"]["width"], variants["desktop"]["height"]
    mw = variants["mobile"]["width"]
    focus = {}
    for seg in src["segments"]:
        focus.setdefault(seg["from"], seg["focusX"][0])
        focus[seg["to"]] = seg["focusX"][-1]
    posters, keep = {}, set()
    for still_id, fx in focus.items():
        path = STILLS / f"{still_id}.jpg"
        digest = sha(path.read_bytes(), {"rev": BUILD_REV, "w": dw, "h": dh, "mw": mw, "fx": fx})[:10]
        im = Image.open(path).convert("RGB").resize((dw, dh), Image.LANCZOS)
        x = strip_x(fx, dw, mw)
        files = {"desktop": (f"{still_id}-{digest}.webp", im, 82),
                 "mobile": (f"{still_id}-{digest}-m.webp", im.crop((x, 0, x + mw, dh)), 82),
                 "thumb": (f"{still_id}-{digest}-t.webp", im.resize((480, 270), Image.LANCZOS), 72)}
        posters[still_id] = {}
        for key, (fname, image, q) in files.items():
            if not (out / fname).exists():
                image.save(out / fname, "WEBP", quality=q, method=6)
            posters[still_id][key] = f"posters/{fname}"
            keep.add(fname)
    for stale in out.iterdir():
        if stale.name not in keep:
            stale.unlink()
    return posters


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="rebuild segments even if cached")
    args = ap.parse_args()

    src = json.loads(SRC.read_text())
    cfg = src["output"]
    segments = sorted(src["segments"], key=lambda s: s["order"])
    clips = [CLIPS / f"{seg['order']:02d}-{seg['id']}.mp4" for seg in segments]
    for clip in clips:
        if not clip.exists():
            sys.exit(f"missing clip {clip.relative_to(ROOT)} - generate it first (see pipeline/plan.py)")
    (OUT / "frames").mkdir(parents=True, exist_ok=True)

    print("Posters")
    posters = build_posters(src, cfg)

    print("Frames")
    built, start, keep = [], 0, set()
    for i, seg in enumerate(segments):
        nxt = clips[i + 1] if i + 1 < len(clips) else None
        meta = build_segment(seg, clips[i], nxt, cfg, is_first=i == 0, force=args.force)
        keep.add(Path(meta["dir"]).name)
        built.append({
            "id": seg["id"], "order": seg["order"], "parts": seg["parts"],
            "start": start, "count": meta["count"], "previewCount": meta["previewCount"],
            "dir": meta["dir"], "height": seg["height"], "camera": seg["camera"],
            "sky": seg["sky"], "focusX": seg["focusX"], "luma": meta["luma"],
            "poster": seg["to"], "caption": dict(seg["caption"]) if seg["caption"] else None,
            "bytes": meta["bytes"],
        })
        start += meta["count"]
    total = start

    for stale in (OUT / "frames").iterdir():
        if stale.is_dir() and stale.name not in keep:
            shutil.rmtree(stale)
            print(f"  removed stale {stale.name}")

    # A caption fades in once its part has landed and hands over before the next one.
    captioned = [s for s in built if s["caption"]]
    for seg in captioned:
        seg["caption"]["in"] = seg["start"] + int(seg["count"] * 0.6)
    for k, seg in enumerate(captioned):
        nxt = next((s for s in built if s["order"] == seg["order"] + 1), None)
        out_at = nxt["start"] + int(nxt["count"] * 0.3) if nxt else None
        if out_at is not None and k + 1 < len(captioned):
            out_at = min(out_at, captioned[k + 1]["caption"]["in"] - 14)
        seg["caption"]["out"] = out_at

    totals = {}
    for seg in built:
        for key, val in seg["bytes"].items():
            totals[key] = totals.get(key, 0) + val
    manifest = {
        "version": sha([s["dir"] for s in built], posters, cfg)[:10],
        "title": src["title"], "subtitle": src["subtitle"], "hint": src["hint"],
        "fps": cfg["fps"], "scrollPxPerFrame": cfg["scrollPxPerFrame"], "holdEnd": cfg["holdEnd"],
        "totalFrames": total,
        "variants": {v: {"width": c["width"], "height": c["height"], "maxAspect": c.get("maxAspect")}
                     for v, c in cfg["variants"].items()},
        "preview": {"step": cfg["preview"]["step"], "scale": cfg["preview"]["scale"]},
        "captionRegion": CAPTION_REGION,
        "posters": posters,
        "opening": segments[0]["from"], "finale": segments[-1]["to"],
        "segments": built,
        "bytes": totals,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))
    print(f"\n{total} frames, {len(built)} segments, manifest version {manifest['version']}")
    for key in sorted(totals):
        print(f"  {key:10s} {totals[key] / 1e6:6.1f} MB")


if __name__ == "__main__":
    main()
