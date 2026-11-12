#!/usr/bin/env python3
"""Check the built frame sequence and write contact sheets for review.

Reads site/assets/manifest.json and the frames it points to. Writes
  qa/<n>-<segment>.jpg   desktop frames with the phone crop marked, sampled evenly
  qa/phone.jpg           the phone strips across the whole build
  qa/report.json         per-segment numbers and any flags

Flags raised:
  cut        a frame-to-frame change far above the segment's norm (a jump or pop)
  seam       the step from one segment's last frame to the next one's first
  duplicate  consecutive frames that are identical
  dark/blank a frame with almost no detail

Exit code is 1 when anything is flagged, so this can gate a build.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "site" / "assets"
OUT = ROOT / "qa"


def load(path, size):
    return np.asarray(Image.open(path).convert("RGB").resize(size, Image.BILINEAR), dtype=np.float32)


def main():
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    OUT.mkdir(exist_ok=True)
    dv, mv = manifest["variants"]["desktop"], manifest["variants"]["mobile"]
    strip_frac = mv["width"] / dv["width"]
    report, flagged, prev_last = [], 0, None
    phone_tiles = []

    for seg in manifest["segments"]:
        ddir, mdir = ASSETS / seg["dir"] / "desktop", ASSETS / seg["dir"] / "mobile"
        count = seg["count"]
        thumbs = np.stack([load(ddir / f"{i + 1:04d}.webp", (160, 90)) for i in range(count)])
        diffs = np.abs(thumbs[1:] - thumbs[:-1]).mean(axis=(1, 2, 3))
        median = float(np.median(diffs))
        limit = max(6 * median, 5.0)
        flags = []
        for i, d in enumerate(diffs):
            if d > limit:
                flags.append({"type": "cut", "frame": i + 1, "diff": round(float(d), 2)})
            elif d < 1e-4:
                flags.append({"type": "duplicate", "frame": i + 1})
        for i, t in enumerate(thumbs):
            if t.std() < 3:
                flags.append({"type": "blank", "frame": i})
        seam = None
        if prev_last is not None:
            seam = float(np.abs(thumbs[0] - prev_last).mean())
            if seam > max(limit, 5.0):
                flags.append({"type": "seam", "diff": round(seam, 2)})
        prev_last = thumbs[-1]
        flagged += len(flags)

        # Contact sheet: 12 evenly spaced frames, phone crop outlined.
        cols, tw, th = 4, 400, 225
        picks = [round(k * (count - 1) / 11) for k in range(12)]
        sheet = Image.new("RGB", (cols * tw, 3 * th), "#111")
        for k, i in enumerate(picks):
            tile = Image.open(ddir / f"{i + 1:04d}.webp").convert("RGB").resize((tw, th), Image.LANCZOS)
            t = i / max(1, count - 1)
            keys = seg["focusX"]
            x = t * (len(keys) - 1)
            a = min(int(x), len(keys) - 2)
            fx = keys[a] + (keys[a + 1] - keys[a]) * (x - a)
            left = min(max(fx - strip_frac / 2, 0), 1 - strip_frac) * tw
            draw = ImageDraw.Draw(tile)
            draw.rectangle([left, 0, left + strip_frac * tw, th - 1], outline="#ffd27a")
            draw.text((6, 4), f"{seg['start'] + i}", fill="#fff")
            sheet.paste(tile, ((k % cols) * tw, (k // cols) * th))
        sheet.save(OUT / f"{seg['order']}-{seg['id']}.jpg", quality=82)

        for i in (0, count // 2, count - 1):
            phone_tiles.append(Image.open(mdir / f"{i + 1:04d}.webp").convert("RGB").resize((168, 270), Image.LANCZOS))

        report.append({
            "id": seg["id"], "frames": count, "start": seg["start"],
            "medianDiff": round(median, 2), "maxDiff": round(float(diffs.max()), 2),
            "seamIn": None if seam is None else round(seam, 2),
            "luma": [round(float(thumbs[0].mean())), round(float(thumbs[-1].mean()))],
            "bytes": seg["bytes"], "flags": flags,
        })
        seam_txt = "-" if seam is None else f"{seam:.2f}"
        print(f"{seg['id']:14s} {count:4d} frames  median diff {median:5.2f}  max {diffs.max():5.2f}  "
              f"seam in {seam_txt:>5s}  flags {len(flags)}")

    phone = Image.new("RGB", (len(phone_tiles) * 168, 270))
    for k, tile in enumerate(phone_tiles):
        phone.paste(tile, (k * 168, 0))
    phone.save(OUT / "phone.jpg", quality=82)

    (OUT / "report.json").write_text(json.dumps(
        {"version": manifest["version"], "totalFrames": manifest["totalFrames"],
         "bytes": manifest["bytes"], "flagged": flagged, "segments": report}, indent=2))
    print(f"\n{manifest['totalFrames']} frames, {flagged} flag(s). Sheets and report in qa/")
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()
