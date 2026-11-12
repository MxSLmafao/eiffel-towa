#!/usr/bin/env python3
"""Print the image/video requests that manifest.src.json implies.

The image/video MCP is only reachable from an agent session, not from a shell,
so generation is a two-step hand-off: this script lists exactly what to request
(tool, parameters, where to save the result), an agent with the MCP connected
runs the requests, and pipeline/build.py takes it from there.

    python3 pipeline/plan.py            # everything still missing
    python3 pipeline/plan.py --all      # every request, including ones already on disk
    python3 pipeline/plan.py --json     # machine-readable

Stills must be reachable by URL before a clip can use them as its first or last
frame: commit and push assets-src/stills so generation.hostBase resolves.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "manifest.src.json"


def requests(src):
    gen = src["generation"]
    host = gen["hostBase"]
    out = []
    for still in src["stills"]:
        args = {"model": gen["image"]["model"], "size": gen["image"]["size"],
                "prompt_extend": gen["image"]["prompt_extend"],
                "prompt": still["prompt"], "negative_prompt": src["style"]["negative"]}
        if still["reference"]:
            args["reference_image_url"] = f"{host}{still['reference']}.jpg"
        out.append({"kind": "still", "id": still["id"], "role": still["role"],
                    "needs": [still["reference"]] if still["reference"] else [],
                    "tool": f"{gen['server']}.{gen['image']['tool']}", "arguments": args,
                    "save_as": f"assets-src/stills/{still['id']}.jpg"})
    for seg in sorted(src["segments"], key=lambda s: s["order"]):
        video = gen["video"]
        args = {"model": video["model"], "resolution": video["resolution"], "ratio": video["ratio"],
                "audio": video["audio"], "prompt_extend": video["prompt_extend"],
                "duration": seg["duration"], "prompt": seg["prompt"],
                "first_frame_url": f"{host}{seg['from']}.jpg",
                "last_frame_url": f"{host}{seg['to']}.jpg"}
        out.append({"kind": "clip", "id": seg["id"], "role": f"segment {seg['order']}: {', '.join(seg['parts'])}",
                    "needs": [seg["from"], seg["to"]],
                    "tool": f"{gen['server']}.{video['tool']}", "arguments": args,
                    "save_as": f"assets-src/clips/{seg['order']:02d}-{seg['id']}.mp4"})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="include requests whose output already exists")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a summary")
    args = ap.parse_args()

    todo = [r for r in requests(json.loads(SRC.read_text()))
            if args.all or not (ROOT / r["save_as"]).exists()]
    if args.json:
        print(json.dumps(todo, indent=2, ensure_ascii=False))
        return
    if not todo:
        print("Nothing to generate: every still and clip in the manifest is on disk.")
        return
    for r in todo:
        a = r["arguments"]
        print(f"[{r['kind']}] {r['id']}  ->  {r['save_as']}")
        print(f"    {r['role']}")
        print(f"    {r['tool']}  " + ", ".join(f"{k}={v}" for k, v in a.items() if k not in ("prompt", "negative_prompt")))
        print(f"    prompt: {a['prompt'][:150]}...")
        print()
    print(f"{len(todo)} request(s). Use --json for full prompts.")


if __name__ == "__main__":
    main()
