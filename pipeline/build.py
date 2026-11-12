#!/usr/bin/env python3
"""Slice the segment clips into scroll frames and write the site manifest.

Reads   manifest.src.json            the single source manifest
        assets-src/clips/*.mp4       one clip per segment
Writes  site/assets/frames/<segment>-<hash>/<variant>/NNNN.webp
        site/assets/frames/<segment>-<hash>/<variant>-p/NNNN.webp   (preview tier)
        site/assets/frames/<segment>-<hash>/poster*.webp            (milestone stills)
        site/assets/manifest.json    everything the site needs; nothing is hand-keyed

Segments are joined in one of two ways (output.transition, or per segment):
  dissolve  a film dissolve: the tail of one shot is mixed with the head of the
            next. For clips that were generated independently.
  join      for clips generated between shared first/last frames: the segment
            ends on the next clip's first settled frame, easing the last frames
            into it with a small zoom/shift plus a dissolve.

A segment may also use only parts of its clip ("ranges"), dissolving between them.

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
OUT = ROOT / "site" / "assets"

# Caption regions, as fractions of the variant frame (x0, y0, x1, y1). The build
# samples their brightness so the site can pick ink or cream text per frame.
CAPTION_REGION = {"desktop": (0.04, 0.36, 0.36, 0.58), "mobile": (0.14, 0.10, 0.46, 0.28)}
BUILD_REV = 7  # bump to invalidate every cached output
PIXEL_KEYS = ("ranges", "rangeDissolve", "tailHold", "focusX", "trim", "transition", "dissolve", "endBlend")


def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else json.dumps(p, sort_keys=True).encode())
    return h.hexdigest()


def smooth(w):
    return w * w * (3 - 2 * w)


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
        "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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


def frames_at(path, indices, width, height):
    """Return {index: frame} for the given source frame indices."""
    want, got = set(indices), {}
    if not want:
        return got
    for i, frame in enumerate(read_frames(path, width, height)):
        if i in want:
            got[i] = frame.copy()
            if len(got) == len(want):
                break
    return got


def lerp_keys(keys, t):
    """Piecewise-linear interpolation over evenly spaced keys, t in 0..1."""
    if len(keys) == 1:
        return keys[0]
    x = min(max(t, 0.0), 1.0) * (len(keys) - 1)
    i = min(int(x), len(keys) - 2)
    return keys[i] + (keys[i + 1] - keys[i]) * (x - i)


def strip_x(focus, src_w, strip_w):
    return int(round(min(max(focus * src_w - strip_w / 2, 0), src_w - strip_w)))


# ---------- eased joins (for clips locked to shared boundary frames) ----------

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
    return (arr[..., :3] * alpha + target.astype(np.float32) * (1 - alpha) + 0.5).astype(np.uint8)


def mix(a, b, w):
    return (a.astype(np.float32) * (1 - w) + b.astype(np.float32) * w + 0.5).astype(np.uint8)


# ---------- frame selection ----------

def resample(a, b, step):
    count = max(2, int(round((b - a) / step)) + 1)
    return sorted({int(round(a + j * (b - a) / (count - 1))) for j in range(count)})


def pick_shots(seg, path, info, cfg):
    """Choose which source frames a segment uses, as one or more shots.

    With explicit "ranges" each [start, end] pair of source frames is a shot.
    Otherwise the whole clip is one shot, minus `snap` frames at each end, with
    a static head or tail shortened to maxHold output frames so a part that has
    already settled does not cost dead scroll distance.
    """
    step = info["fps"] / cfg["fps"]
    n = info["frames"]
    if seg.get("ranges"):
        shots = [resample(max(0, a), min(n - 1, b), step) for a, b in seg["ranges"]]
        return shots, {"sourceFrames": n, "ranges": seg["ranges"]}

    snap = cfg.get("snap", 0)
    thumbs = np.stack([f.astype(np.float32) for f in read_frames(path, 160, 90)])
    n = len(thumbs)
    first, last = snap, n - 1 - snap
    a, b, head, tail = first, last, first, last
    if seg.get("trim", True):
        eps = 1.5
        d_last = np.abs(thumbs - thumbs[last]).mean(axis=(1, 2, 3))
        d_first = np.abs(thumbs - thumbs[first]).mean(axis=(1, 2, 3))
        while tail > first and d_last[tail - 1] < eps:
            tail -= 1
        while head < last and d_first[head + 1] < eps:
            head += 1
        hold = int(round(cfg.get("maxHold", 0) * step))
        a, b = max(first, head - hold), min(last, tail + hold)
        if b - a < step * 8:  # nearly static clip: keep it whole
            a, b = first, last
    return [resample(a, b, step)], {"sourceFrames": n, "staticHead": head - first, "staticTail": last - tail}


def plan_outputs(seg, shots, head_skip, transition, next_shot, blend):
    """Lay out a segment's output frames.

    Each entry is {"a": own source index, "b": None | ("self"|"next", index), "w": weight}.
    Returns (entries, poster_index): poster_index is the last frame that shows
    only this segment's finished state.
    """
    out = [{"a": i, "b": None, "w": 0.0} for i in shots[0][head_skip:]]
    for shot in shots[1:]:  # dissolve between ranges of the same clip
        k = max(0, min(int(seg.get("rangeDissolve", 12)), len(out) - 1, len(shot) - 1))
        for j in range(k):
            entry = out[len(out) - k + j]
            entry["b"], entry["w"] = ("self", shot[j]), smooth((j + 1) / (k + 1))
        out += [{"a": i, "b": None, "w": 0.0} for i in shot[k:]]

    # tailHold repeats the final frame, so the hand-over dissolve runs over a
    # held picture instead of using up a short shot.
    out += [{"a": out[-1]["a"], "b": None, "w": 0.0} for _ in range(int(seg.get("tailHold", 0)))]

    poster = len(out) - 1
    if next_shot is None:
        return out, poster, 0
    pure_tail = 0
    while pure_tail < len(out) and out[len(out) - 1 - pure_tail]["b"] is None:
        pure_tail += 1
    k = max(0, min(blend, pure_tail - 1, len(out) - 1))
    if transition == "dissolve":
        k = min(k, len(next_shot) - 2)
        for j in range(k):
            entry = out[len(out) - k + j]
            entry["b"], entry["w"] = ("next", next_shot[j]), smooth((j + 1) / (k + 1))
        poster = len(out) - 1 - k
    else:  # join: ease onto the next clip's first frame
        for j in range(1, k + 1):
            entry = out[len(out) - 1 - k + j]
            entry["b"], entry["w"], entry["ease"] = ("next", next_shot[0]), smooth(j / k), True
    return out, poster, k


def save_webp(arr, path, quality, method=4):
    Image.fromarray(arr).save(path, "WEBP", quality=quality, method=method)


def region_luma(arr, region):
    h, w, _ = arr.shape
    x0, y0, x1, y1 = region
    crop = arr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].astype(np.float32)
    return float((crop * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).sum(axis=2).mean())


def build_segment(seg, clip, info, shots, trim_info, head_skip, transition, nxt, cfg, is_first, force):
    """Render one segment. `nxt` is (clip, first_shot, pixel settings) of the next segment, or None."""
    variants, prev = cfg["variants"], cfg["preview"]
    settings = {"rev": BUILD_REV, "output": cfg, "seg": {k: seg.get(k) for k in PIXEL_KEYS},
                "headSkip": head_skip, "transition": transition, "first": is_first,
                "regions": CAPTION_REGION,
                "next": [sha(nxt[0].read_bytes())[:16], nxt[2]] if nxt else None}
    digest = sha(clip.read_bytes(), settings)[:10]
    name = f"{seg['id']}-{digest}"
    out_dir = OUT / "frames" / name
    meta_path = out_dir / "segment.json"
    if meta_path.exists() and not force:
        print(f"  {seg['id']}: cached ({name})")
        return json.loads(meta_path.read_text())

    if out_dir.exists():
        shutil.rmtree(out_dir)
    for v in variants:
        (out_dir / v).mkdir(parents=True)
        (out_dir / f"{v}-p").mkdir(parents=True)

    dw, dh = variants["desktop"]["width"], variants["desktop"]["height"]
    mw = variants["mobile"]["width"]
    pscale, pstep = prev["scale"], prev["step"]

    # How long the hand-over to the next segment takes.
    blend, align, next_frames = 0, None, {}
    next_shot = nxt[1] if nxt else None
    if nxt and transition == "dissolve":
        blend = int(seg.get("dissolve", cfg.get("dissolve", 16)))
    elif nxt:
        target = frames_at(nxt[0], [next_shot[0]], dw, dh)[next_shot[0]]
        own_last = shots[-1][-1]
        align, before, after = estimate_align(frames_at(clip, [own_last], dw, dh)[own_last], target)
        blend = seg.get("endBlend")
        if blend is None:
            blend = int(min(30, max(6, round(4 + 1.5 * before))))
        trim_info["seam"] = {"scale": round(align[0], 4), "shift": [round(align[1], 4), round(align[2], 4)],
                             "gap": round(before, 2), "gapAligned": round(after, 2)}

    plan, poster_at, blend = plan_outputs(seg, shots, head_skip, transition, next_shot, int(blend))
    count = len(plan)
    if nxt:
        next_frames = frames_at(nxt[0], [e["b"][1] for e in plan if e["b"] and e["b"][0] == "next"], dw, dh)
        trim_info["handover"] = {"type": transition, "frames": blend}

    uses = {}
    for e in plan:
        uses[e["a"]] = uses.get(e["a"], 0) + 1
        if e["b"] and e["b"][0] == "self":
            uses[e["b"][1]] = uses.get(e["b"][1], 0) + 1

    sizes = {v: 0 for v in list(variants) + [f"{v}-p" for v in variants]}
    luma = {v: [] for v in variants}
    posters = {}

    def emit(j, frame):
        t = j / max(1, count - 1)
        x = strip_x(lerp_keys(seg["focusX"], t), dw, mw)
        strip = np.ascontiguousarray(frame[:, x:x + mw])
        for v, arr in (("desktop", frame), ("mobile", strip)):
            p = out_dir / v / f"{j + 1:04d}.webp"
            save_webp(arr, p, variants[v]["quality"])
            sizes[v] += p.stat().st_size
            if j % pstep == 0:
                small = Image.fromarray(arr).resize(
                    (max(2, int(arr.shape[1] * pscale)), max(2, int(arr.shape[0] * pscale))), Image.LANCZOS)
                pp = out_dir / f"{v}-p" / f"{j // pstep + 1:04d}.webp"
                small.save(pp, "WEBP", quality=prev["quality"], method=4)
                sizes[f"{v}-p"] += pp.stat().st_size
                luma[v].append(round(region_luma(arr, CAPTION_REGION[v])))
        stills = []
        if j == poster_at:
            stills.append("poster")
        if is_first and j == 0:
            stills.append("opening")
        for key in stills:  # milestone stills: loading state, reduced-motion view, caption art
            save_webp(frame, out_dir / f"{key}.webp", 82, 6)
            save_webp(strip, out_dir / f"{key}-m.webp", 82, 6)
            thumb = np.asarray(Image.fromarray(frame).resize((480, 270), Image.LANCZOS))
            save_webp(thumb, out_dir / f"{key}-t.webp", 72, 6)
            posters[key] = {"desktop": f"frames/{name}/{key}.webp", "mobile": f"frames/{name}/{key}-m.webp",
                            "thumb": f"frames/{name}/{key}-t.webp"}

    store, ptr = {}, 0
    for s, frame in enumerate(read_frames(clip, dw, dh)):
        if s in uses:
            store[s] = frame.copy()
        while ptr < count:  # write every output frame whose sources have been read
            e = plan[ptr]
            if e["a"] not in store or (e["b"] and e["b"][0] == "self" and e["b"][1] not in store):
                break
            a = store[e["a"]]
            if e["b"] is None:
                result = a
            else:
                b = store[e["b"][1]] if e["b"][0] == "self" else next_frames[e["b"][1]]
                result = ease_into(a, b, align, e["w"]) if e.get("ease") else mix(a, b, e["w"])
            emit(ptr, result)
            for used in [e["a"]] + ([e["b"][1]] if e["b"] and e["b"][0] == "self" else []):
                uses[used] -= 1
                if uses[used] == 0:
                    del store[used]
            ptr += 1
        if ptr >= count:
            break
    if ptr < count:
        sys.exit(f"{clip.name}: could not render frame {ptr} of {count}; check ranges in the manifest")

    meta = {"id": seg["id"], "dir": f"frames/{name}", "count": count,
            "previewCount": (count + pstep - 1) // pstep, "bytes": sizes, "luma": luma,
            "posters": posters, "handoverFrames": blend if transition == "dissolve" else 1,
            "source": {"clip": clip.name, "fps": info["fps"], **trim_info}}
    meta_path.write_text(json.dumps(meta, indent=1))
    print(f"  {seg['id']}: {count} frames from {info['frames']}"
          + (f", {transition} over {blend}" if nxt else "")
          + f", desktop {sizes['desktop'] / 1e6:.1f} MB, mobile {sizes['mobile'] / 1e6:.1f} MB", flush=True)
    return meta


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

    print("Planning")
    infos = [probe(c) for c in clips]
    picked = [pick_shots(seg, clip, info, cfg) for seg, clip, info in zip(segments, clips, infos)]

    print("Frames")
    built, start, keep, head_skip = [], 0, set(), 0
    for i, seg in enumerate(segments):
        shots, trim_info = picked[i]
        transition = seg.get("transition", cfg.get("transition", "join"))
        nxt = None
        if i + 1 < len(segments):
            nseg = segments[i + 1]
            nxt = (clips[i + 1], picked[i + 1][0][0], {k: nseg.get(k) for k in PIXEL_KEYS})
        meta = build_segment(seg, clips[i], infos[i], shots, dict(trim_info), head_skip, transition,
                             nxt, cfg, is_first=i == 0, force=args.force)
        head_skip = meta["handoverFrames"] if nxt else 0
        keep.add(Path(meta["dir"]).name)
        built.append({
            "id": seg["id"], "order": seg["order"], "parts": seg["parts"],
            "start": start, "count": meta["count"], "previewCount": meta["previewCount"],
            "dir": meta["dir"], "height": seg["height"], "camera": seg["camera"],
            "sky": seg["sky"], "focusX": seg["focusX"], "luma": meta["luma"],
            "poster": meta["posters"]["poster"],
            "caption": dict(seg["caption"]) if seg["caption"] else None,
            "bytes": meta["bytes"],
        })
        if i == 0:
            opening = meta["posters"]["opening"]
        start += meta["count"]
    total = start

    for stale in (OUT / "frames").iterdir():
        if stale.is_dir() and stale.name not in keep:
            shutil.rmtree(stale)
            print(f"  removed stale {stale.name}")
    if (OUT / "posters").exists():  # earlier builds kept posters separately
        shutil.rmtree(OUT / "posters")

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
        "version": sha([s["dir"] for s in built], cfg)[:10],
        "title": src["title"], "subtitle": src["subtitle"], "hint": src["hint"],
        "fps": cfg["fps"], "scrollPxPerFrame": cfg["scrollPxPerFrame"], "holdEnd": cfg["holdEnd"],
        "totalFrames": total,
        "variants": {v: {"width": c["width"], "height": c["height"], "maxAspect": c.get("maxAspect")}
                     for v, c in cfg["variants"].items()},
        "preview": {"step": cfg["preview"]["step"], "scale": cfg["preview"]["scale"]},
        "captionRegion": CAPTION_REGION,
        "opening": opening, "finale": built[-1]["poster"],
        "segments": built,
        "bytes": totals,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))
    print(f"\n{total} frames, {len(built)} segments, manifest version {manifest['version']}")
    for key in sorted(totals):
        print(f"  {key:10s} {totals[key] / 1e6:6.1f} MB")


if __name__ == "__main__":
    main()
