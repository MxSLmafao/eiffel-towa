# La Tour Eiffel — scroll to build

A single pinned, full-viewport scene. Scrolling scrubs a pre-rendered frame
sequence in which the Eiffel Tower assembles from its foundations to its
illumination at night; scrolling back up runs the build in reverse. The camera
move, the assembly and the change from daylight to night are all baked into the
frames.

```bash
npm run dev
```

Then open http://localhost:5173. There is no bundler and no dependency to
install for the site: `site/` is plain HTML, CSS and ES modules.

## How it fits together

```
manifest.src.json        the one source manifest: segments, order, heights,
                         camera notes, captions, prompts, output settings
assets-src/stills/       boundary stills from the image MCP (one per milestone)
assets-src/clips/        one clip per segment from the video MCP
pipeline/plan.py         lists the image/video requests the manifest implies
pipeline/build.py        clips -> frames, phone crops, posters, site manifest
pipeline/qa.py           checks the built frames, writes contact sheets to qa/
site/                    the page; reads site/assets/manifest.json
```

The site hard-codes nothing about the build. Segment order, frame ranges,
caption text and timing, heights for the progress readout, the phone crop focus
and the text tone per frame all come from `site/assets/manifest.json`, which
`pipeline/build.py` writes.

## The six segments

| # | Segment | Built | Camera | Sky |
|---|---|---|---|---|
| 1 | foundations | four masonry piers | low, locked off | day |
| 2 | first-level | four legs, first platform | long pull-back | day |
| 3 | second-level | mid-lattice, second platform | slow push-in | day |
| 4 | upper-shaft | upper shaft | crane up with an orbit | golden hour |
| 5 | spire | summit cabin, spire, antenna | aerial, locked off | dusk |
| 6 | illumination | lights, base to beacon | aerial, locked off | night |

## Generating assets

Assets come from the Higgsfield MCP (`generate_image`, `generate_video`), all at
720p and 16:9. That server is reachable from an agent session, not from a shell,
so generation is a hand-off:

1. `npm run assets:plan` prints every still and clip the manifest asks for that
   is not on disk yet (`-- --json` for full prompts and parameters).
2. An agent with the MCP connected runs those requests and saves each result to
   the path the plan names.
3. `npm run assets:build`, then `npm run assets:qa`.

Two things make the segments join:

- **Stills first.** One style-anchor still sets the look. Every other still is
  an edit of an earlier one, so the tower, palette and setting stay identical
  while the build state, camera or light changes.
- **Clips are locked at both ends.** Each clip is generated with the previous
  milestone still as its first frame and the next as its last, so neighbouring
  clips meet on the same picture. The video tool needs those stills by URL,
  which is why `assets-src/stills` is committed and pushed before clips are
  requested (`generation.hostBase` in the manifest).

## What the build does

- Drops the first and last two frames of every clip. The video model renders
  slightly brighter than the stills and steps into and out of that look over
  those frames.
- Ends each segment on the next clip's first settled frame, easing the last few
  frames into it (a small zoom or shift where framing differs, plus a dissolve).
- Trims a static head or tail to a short hold, so a part that has settled does
  not cost dead scroll distance.
- Writes two variants: `desktop` (1280×720) and `mobile` (448×720, a strip
  cover-cropped around each segment's `focusX`), plus a quarter-size preview
  tier of every 8th frame.
- Names each segment directory by a hash of the clip and every setting that
  affects pixels. Unchanged segments are skipped on re-runs and the files can
  be cached forever (`vercel.json` sets the headers).

Current output: 804 frames at 20 fps. Desktop 32.9 MB, mobile 16.9 MB, previews
0.4 MB and 0.2 MB.

## What the page does

- Loads the manifest, shows the opening still, then loads the preview tier.
  The rule under the date draws in as previews arrive; once they are in, the
  whole build can be scrubbed at low resolution.
- Streams full frames nearest-first around the scroll position and decodes only
  a small window around the playhead, so memory stays flat on phones.
- Picks the phone strip when the viewport is narrower than about 16:25,
  otherwise the 16:9 frames cover-cropped around the tower.
- With `prefers-reduced-motion`, skips the scrub entirely and presents the
  finished, illuminated tower with the five milestones beneath it. Append
  `?still` to preview that view, or `?variant=mobile` / `?variant=desktop` to
  force a frame set.

## Requirements for the pipeline

Python 3 with Pillow and numpy, and ffmpeg on the PATH.
