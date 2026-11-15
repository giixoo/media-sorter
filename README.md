# media-sorter

A small web app for the first pass over a pile of trip footage: point it at a folder (with any
sub-folders) of videos and photos, watch everything in one timeline sorted by capture time, and mark
what is worth keeping:

- **Videos** – mark good pieces with IN / OUT, give them 1–5 stars, flag "keep original sound", add a caption.
- **Photos** – click to select, rate 1–5 stars, open a viewer with non-destructive light fixes
  (exposure, highlights, shadows, contrast, saturation, warmth, auto fix) and crop (free / original / 16:9 / 9:16 / 4:3 / 1:1).

Nothing in your folder is changed. Choices are saved continuously to one JSON file that an editing
script, an NLE import or a person can work from.

It handles footage that browsers can't play directly: HEVC / 10-bit / 4K / HDR phone clips, MKV, AVI…
get a small H.264 preview made in the background (HDR is tone-mapped to SDR). DJI `.LRF` proxies next to the
originals are used as they are. HEIC photos work too.

## Run

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -v "/path/to/your/folder:/media" \
  -e TZ=Europe/Warsaw \
  ghcr.io/giixoo/media-sorter
```

Windows (PowerShell):

```powershell
docker run --rm -p 127.0.0.1:8765:8765 -v "D:\Videos\Trip:/media" -e TZ=Europe/Warsaw ghcr.io/giixoo/media-sorter
```

Then open <http://localhost:8765>. The first start indexes the folder (the header shows progress) and
starts making previews for clips that need them. Clips you open jump to the front of the queue.

With compose: `MEDIA=/path/to/folder docker compose up` (see `docker-compose.yml`).

On Linux, add `--user "$(id -u):$(id -g)"` so the generated files belong to you.

## What it writes

Everything goes into **`.media-sorter/`** inside the mounted folder (or `STATE_DIR`):

| path | |
|---|---|
| `selections.json` | your picks – the useful output |
| `index.json` | metadata cache, so restarts and rescans are quick |
| `thumbs/` | thumbnails |
| `proxies/` | browser previews (roughly 3–5 MB per minute at 720p) |

To keep the media folder read-only, mount it with `:ro` and give the state its own volume:
`-v /path/to/folder:/media:ro -v media-sorter-state:/state -e STATE_DIR=/state`.

Folders starting with `.` are skipped. Press **⟳ Rescan** after adding files.

### selections.json

Keys are paths relative to the media folder.

```jsonc
{
  "version": 2,
  "clips": {                       // video pieces, times in seconds of the original file
    "cam/DJI_0026_D.MP4": [ { "a": 12.4, "b": 19.8, "stars": 4, "nat": true, "cap": "Lublin · castle" } ]
  },
  "photos": { "phone/20260605_164227.jpg": 5 },        // selected photos -> stars
  "edits": {                                            // photo fixes (selected or not)
    "phone/20260605_164227.jpg": {
      "adj":  { "exp": -0.3, "hl": 0.4, "sh": 0.2, "con": 0.1, "sat": 0.1, "tmp": 0 },
      "crop": { "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.6 }       // 0..1 of the EXIF-rotated image
    }
  },
  "saved": "2026-11-15T22:44:41"
}
```

To apply the same photo fixes in your own pipeline, use `app/photofx.py`
(`photofx.render(path, edit)` returns the adjusted, cropped Pillow image, matching the preview).

## Settings

| env | default | |
|---|---|---|
| `MEDIA_DIR` | `/media` | folder to scan |
| `STATE_DIR` | `$MEDIA_DIR/.media-sorter` | where state and previews go |
| `PORT` | `8765` | |
| `TITLE` | folder name | name in the header |
| `TZ` | `UTC` | used to turn UTC video timestamps into local time |
| `PROXY_WORKERS` | `1` | parallel preview transcodes |
| `PROXY_HEIGHT` | `720` | preview size (short side) |
| `PROXY_ENCODER` | `libx264` | e.g. `h264_nvenc` with an NVIDIA GPU (`--gpus all`); falls back to libx264 if it fails |
| `PREGENERATE_PROXIES` | `1` | `0` = make previews only when a clip is opened |
| `DIRECT_PLAY_MAX` | `1920` | longest side for which plain H.264 files are streamed as they are |

**Capture time** is taken from (photos) EXIF → file name → file date, and (videos) file name →
QuickTime creation date → container `creation_time` (UTC, converted with `TZ`) → file date. File names
like `20260604_101447`, `DJI_20260604085022_0015`, `PXL_20260604_101447123` or `2026-06-04 10.14.47` are understood.

## Develop

```bash
pip install -r requirements.txt      # plus ffmpeg on PATH
MEDIA_DIR=/path/to/folder python app/server.py
```

No build step: `app/server.py` (stdlib HTTP server + API), `app/library.py` (scan, metadata,
thumbnails, preview queue), `app/photofx.py` (photo adjustments), `app/static/index.html` (the whole UI).

## Publish

`.github/workflows/docker.yml` builds `linux/amd64` + `linux/arm64` images and pushes them to
`ghcr.io/<owner>/media-sorter` on every push to `main` (`:latest`, `:main`) and on version tags
(`git tag v1.0.0 && git push --tags` → `:1.0.0`, `:1.0`). Add `DOCKERHUB_USERNAME` /
`DOCKERHUB_TOKEN` repository secrets to also push to Docker Hub. After the first push, make the GHCR
package public in its settings if you want to pull it without logging in.

## License

Apache 2.0 – see [LICENSE](LICENSE).
