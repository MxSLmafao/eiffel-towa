# La Tour Eiffel — scroll to build

A single pinned, full-viewport scene. Scrolling scrubs a pre-rendered frame
sequence in which the Eiffel Tower is built from its foundations to its
illumination at night; scrolling back up runs the build in reverse. The camera
moves, the assembly and the change from daylight to night are all baked into
the frames.

Live at https://eiffel-towa.vercel.app.

```bash
npm run dev
```

Then open http://localhost:5173. There is no bundler and no dependency to
install for the site: `site/` is plain HTML, CSS and ES modules.

## How it fits together

```
manifest.src.json        the one source manifest: segments, order, heights,
                         camera notes, captions, prompts, output settings
assets-src/clips/        one clip per segment
pipeline/plan.py         lists the clips the manifest asks for, with prompts
pipeline/build.py        clips -> frames, phone crops, milestone stills, site manifest
pipeline/qa.py           checks the built frames, writes contact sheets to qa/
site/                    the page; reads site/assets/manifest.json
```

The site hard-codes nothing about the build. Segment order, frame ranges,
caption text and timing, heights for the progress readout, the phone crop focus
and the text tone per frame all come from `site/assets/manifest.json`, which
`pipeline/build.py` writes.

## The six segments

This is the photorealistic version. Its six clips are independent shots, each
generated from the prompt recorded on its segment in `manifest.src.json`, so
the setting, light and iron colour change from shot to shot and segments are
joined with film dissolves.

| # | Segment | Built | Source frames used |
|---|---|---|---|
| 1 | foundations | masonry piers | all |
| 2 | first-level | four legs, first platform | 0–50 and 138–167 |
| 3 | second-level | mid-lattice, second platform | all |
| 4 | upper-shaft | upper shaft | 0–150 |
| 5 | spire | summit cabin, spire, antenna | all |
| 6 | illumination | lights, base to beacon | all |

Two clips are trimmed because they work against the story: clip 2 builds well
past the first platform and then shrinks back to it, and clip 4 finishes the
spire that segment 5 is there to build.

An earlier low-poly version, whose clips were locked to shared first and last
frames and joined without dissolves, is in the git history up to commit
`7e2ce5e`.

## Replacing clips

1. `npm run assets:plan -- --all` prints each segment's prompt, duration and
   the path its clip is saved to.
2. Put the new clip at that path.
3. `npm run assets:build`, then `npm run assets:qa`.

Per-segment options in `manifest.src.json`:

- `ranges`: use only these `[start, end]` source-frame spans, dissolving
  between them over `rangeDissolve` frames.
- `tailHold`: repeat the last frame this many times, so the dissolve into the
  next segment does not use up a short shot.
- `focusX`: evenly spaced keys for where the tower sits across the segment, as
  a fraction of frame width. Drives the phone crop.
- `transition`: `dissolve` (default here) or `join`. `join` is for clips that
  were generated between shared first and last frames; it ends the segment on
  the next clip's first frame and eases into it.

## What the build does

- Resamples each clip to `output.fps` and lays out the dissolves.
- Writes two variants: `desktop` (1280×720) and `mobile` (448×720, a strip
  cover-cropped around `focusX`), plus a quarter-size preview tier of every
  8th frame.
- Saves a milestone still per segment, used for the loading state, the
  reduced-motion view and as caption art.
- Names each segment directory by a hash of the clip and every setting that
  affects pixels. Unchanged segments are skipped on re-runs and the files can
  be cached forever (`vercel.json` sets the headers).

Current output: 772 frames at 24 fps. Desktop 31.4 MB, mobile 13.8 MB, previews
0.5 MB and 0.2 MB.

`pipeline/qa.py` currently reports one flag: at frame 36 the first stone blocks
drop into the pits within a single frame of the source clip.

## What the page does

- Loads the manifest, shows the opening still, then loads the preview tier.
  The rule under the date draws in as previews arrive; once they are in, the
  whole build can be scrubbed at low resolution.
- Streams full frames nearest-first around the scroll position and decodes only
  a small window around the playhead, so memory stays flat on phones.
- Picks the phone strip when the viewport is narrower than about 16:25,
  otherwise the 16:9 frames cover-cropped around the tower.
- Chooses ink or cream caption text per frame from brightness sampled at build
  time, with a soft backing so captions stay readable over stone and ironwork.
- With `prefers-reduced-motion`, skips the scrub entirely and presents the
  finished, illuminated tower with the five milestones beneath it. Append
  `?still` to preview that view, or `?variant=mobile` / `?variant=desktop` to
  force a frame set.

## Requirements for the pipeline

Python 3 with Pillow and numpy, and ffmpeg on the PATH.
