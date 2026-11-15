# Reading media-sorter selections (for agents and scripts)

This folder (`.media-sorter/`) was written by **media-sorter**, a web app where a person watched the
videos and photos in the parent folder and picked the ones worth using. Use it to find out **which
footage the person chose, which part of each video, how much they like it, and how each photo should
be fixed and cropped.**

Do not re-judge or re-pick the footage yourself. The person's choices are the source of truth.

## Files

| file | what it is |
|---|---|
| `selections.json` | **the person's picks**: read this. It doesn't exist until the first pick is made, which means nothing is selected yet. |
| `index.json` | metadata for every media file: capture time, duration, size, codec, GPS… |
| `thumbs/`, `proxies/` | small previews made for the browser. Don't use them for final output. |
| `AGENTS.md` | this file. It is rewritten each time the app starts. |

All paths in both JSON files are **relative to the media root**, which is normally the folder that
contains `.media-sorter/`. They always use `/`, even on Windows. Join them with the media root to
get the original file.

If the app was started with a separate `STATE_DIR`, this folder isn't inside the media folder. In that
case, ask the person where the media root is, or match the paths against the folder they point you to.

## selections.json

```jsonc
{
  "version": 2,
  "saved": "2026-11-15T22:44:41",              // local time of the last change
  "clips": {                                    // VIDEO: chosen pieces per file
    "cam/DJI_20260605183630_0026_D.MP4": [
      { "a": 12.4, "b": 19.8,                   // start / end in SECONDS from the start of the original file
        "stars": 4,                             // 1..5, see below
        "nat": true,                            // keep the camera's own sound loud (talk, crowd, engines)
        "cap": "Lublin · castle bridge" }       // optional on-screen caption, may be "" or missing
    ]
  },
  "photos": {                                   // PHOTOS: selected files -> stars (1..5)
    "phone/20260605_164227.jpg": 5
  },
  "edits": {                                    // PHOTO fixes, keyed by path
    "phone/20260605_164227.jpg": {
      "adj":  { "exp": -0.3, "hl": 0.4, "sh": 0.2, "con": 0.1, "sat": 0.1, "tmp": 0 },
      "crop": { "x": 0.10, "y": 0.05, "w": 0.80, "h": 0.60 }
    }
  }
}
```

Rules:

- **A video is selected only if it has pieces in `clips`.** Use only the pieces, never the whole file.
  `a` < `b`, and pieces of one file may come in any order, so sort them by `a`.
- **A photo is selected only if it appears in `photos`.** `edits` can also hold entries for photos that
  were fixed but not selected. Ignore those unless the person says otherwise.
- **Stars:** 5 = must have; 3 = default (the person picked it but didn't rate it); 1 = use only if there is room.
  When you need to shorten, drop low stars first. Within the same star level, prefer even coverage over time.
- `nat: false` or missing means the camera audio isn't needed, so music can cover it.
- Any key may be missing. Treat a missing key as the default: `stars` 3, `nat` false, `cap` "", no edits.
- Keys you don't recognise come from newer versions. Ignore them.

## Order and timing (index.json)

`index.json` is `{"items": [ ... ]}`, with one item per media file:

```jsonc
{ "id": "1f3a9c0b2d4e", "path": "cam/DJI_..._0026_D.MP4", "kind": "video",   // or "photo"
  "ts": "2026-06-05T18:36:30", "day": "2026-06-05", "time": "18:36",          // LOCAL capture time, no zone
  "dur": 72.906, "w": 3840, "h": 2160, "fps": 29.97,                           // video: seconds; w/h already rotated
  "vcodec": "hevc", "hdr": false, "acodec": "aac",
  "lrf": "cam/DJI_..._0026_D.LRF",                                             // DJI low-res copy, if present
  "gps": [50.8709, 22.3177],                                                   // photos with GPS, else null
  "folder": "cam", "src": "cam", "size": 755123456, "mtime": 1780000000 }
```

- **Chronological order:** sort everything by `ts`. A video piece happens at `ts + a` seconds, and a
  photo at `ts`. The top-level sub-folder (`src`) is usually one camera or one person's phone.
- Capture time comes from EXIF, then the file name, then container tags, then the file date. Clocks
  of different devices can be a few minutes apart.
- `hdr: true` means a phone HDR (PQ/HLG) video. Tone-map it to SDR BT.709 if the output is SDR, or it
  will look washed out.
- The index may be stale if files changed after the last scan. If a path is missing from it, fall back to
  probing the file (`ffprobe`, EXIF) yourself.

## Applying photo edits

Order: open the original → apply EXIF rotation → apply `adj` → apply `crop`.

- `crop` values are fractions (0..1) of the **EXIF-rotated** image: left `x`, top `y`, width `w`, height `h`.
  In pixels that is `x*W, y*H, w*W, h*H` of the rotated image.
- `adj`, all values 0 = neutral:
  - `exp`: exposure in EV (−2..2)
  - `hl`: highlight recovery (0..1)
  - `sh`: shadow lift (0..1)
  - `con`: contrast (−1..1)
  - `sat`: saturation (−1..1)
  - `tmp`: warmth (−1..1, positive = warmer)
- To get exactly what the person saw, use the app's own code (Pillow + numpy). It's in the
  media-sorter image at `/app/photofx.py`, or in the repo at `app/photofx.py`:

```python
import photofx                       # sys.path.insert(0, "<repo>/app") first
img = photofx.render("/media/phone/20260605_164227.jpg", sel["edits"].get("phone/20260605_164227.jpg"))
img.save("out.jpg", quality=92)      # a PIL.Image, already rotated, adjusted and cropped
```

If you can't use `photofx`, apply at least the crop and the rotation. Then say that the light fixes were
approximated or skipped.

## Minimal reader

```python
import json, os

root  = "/path/to/media"                                  # the folder that contains .media-sorter/
state = os.path.join(root, ".media-sorter")
sel   = json.load(open(os.path.join(state, "selections.json"), encoding="utf-8"))
idx   = {i["path"]: i for i in json.load(open(os.path.join(state, "index.json"), encoding="utf-8"))["items"]}

items = []
for path, pieces in sel.get("clips", {}).items():
    for p in sorted(pieces, key=lambda p: p["a"]):
        items.append(dict(kind="video", path=path, file=os.path.join(root, *path.split("/")),
                          start=p["a"], end=p["b"], stars=p.get("stars", 3), nat=p.get("nat", False),
                          caption=p.get("cap", ""), ts=idx.get(path, {}).get("ts", ""), hdr=idx.get(path, {}).get("hdr", False)))
for path, stars in sel.get("photos", {}).items():
    items.append(dict(kind="photo", path=path, file=os.path.join(root, *path.split("/")), stars=stars,
                      edit=sel.get("edits", {}).get(path), ts=idx.get(path, {}).get("ts", "")))
items.sort(key=lambda i: (i["ts"], i.get("start", 0)))
```

Cutting a piece with ffmpeg (re-encode for frame accuracy; `-ss` before `-i` is fast and accurate when re-encoding):

```bash
ffmpeg -ss 12.4 -i "cam/DJI_..._0026_D.MP4" -t 7.4 -c:v libx264 -crf 18 -c:a aac piece.mp4
```

## While the app is running

- `selections.json` is rewritten about half a second after every click, always in full and atomically
  (temp file, then rename). Reading it at any time is safe. Read it again before a long job if the person
  may still be sorting.
- **Don't edit `selections.json` while the app is open in a browser.** The page keeps its own copy and will
  overwrite your changes on the next click. If you must change picks, ask the person to close the page,
  edit the file, then reload the page.
- The same data is available over HTTP from a running app (default `http://localhost:8765`):
  - `GET /api/selections`: the same as `selections.json`
  - `GET /api/data`: `{videos, photos, sel, status}`. The lists are like `index.json` items, sorted by time.
  - `GET /full/<id>`: the original photo (HEIC is converted to JPEG)
  - `GET /img/<id>?w=1600&exp=..&crop=x,y,w,h`: an adjusted preview
  - `GET /video/<id>`: a browser-playable version (the original, the DJI LRF copy or a 720p proxy). Use it for
    looking only, never for output.

  `id` is the item's `id` from the index.
