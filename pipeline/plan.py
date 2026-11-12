#!/usr/bin/env python3
"""List the clips manifest.src.json asks for, with the prompt for each.

Clips are generated outside this repository (a video model, by hand or through
an agent) and saved to the path shown. pipeline/build.py takes it from there.

    python3 pipeline/plan.py            # clips still missing
    python3 pipeline/plan.py --all      # every clip, including ones on disk
    python3 pipeline/plan.py --json     # machine-readable
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "manifest.src.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="include clips already on disk")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a summary")
    args = ap.parse_args()

    src = json.loads(SRC.read_text())
    clips = []
    for seg in sorted(src["segments"], key=lambda s: s["order"]):
        path = f"assets-src/clips/{seg['order']:02d}-{seg['id']}.mp4"
        if args.all or not (ROOT / path).exists():
            clips.append({"id": seg["id"], "order": seg["order"], "parts": seg["parts"],
                          "duration": seg["duration"], "format": src["source"]["format"],
                          "prompt": seg["prompt"], "save_as": path})
    if args.json:
        print(json.dumps(clips, indent=2, ensure_ascii=False))
        return
    if not clips:
        print("Nothing to generate: every clip in the manifest is on disk.")
        return
    for c in clips:
        print(f"[{c['order']}] {c['id']}  ->  {c['save_as']}")
        print(f"    {c['duration']} s, {c['format']}; builds: {', '.join(c['parts'])}")
        print(f"    prompt: {c['prompt'][:150]}...")
        print()
    print(f"{len(clips)} clip(s). Use --json for full prompts.")


if __name__ == "__main__":
    main()
