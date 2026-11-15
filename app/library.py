"""Media library: scans MEDIA_DIR for videos and photos, reads capture time / GPS / codec info,
makes thumbnails and browser-playable previews. Everything generated goes to STATE_DIR."""
import hashlib
import heapq
import io
import itertools
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PIL import Image, ImageOps

try:
    import pillow_heif  # iPhone / Samsung HEIC photos

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(os.environ.get('TZ') or 'UTC')
except Exception:  # unknown TZ name
    LOCAL_TZ = timezone.utc

MEDIA = os.path.abspath(os.environ.get('MEDIA_DIR', '/media'))
STATE = os.path.abspath(os.environ.get('STATE_DIR') or os.path.join(MEDIA, '.media-sorter'))
THUMBS = os.path.join(STATE, 'thumbs')
PROXIES = os.path.join(STATE, 'proxies')
INDEX = os.path.join(STATE, 'index.json')
SELECTIONS = os.path.join(STATE, 'selections.json')

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.mts', '.m2ts', '.webm', '.3gp', '.wmv', '.mpg', '.mpeg'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.tif', '.tiff', '.bmp'}
WEB_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
SKIP_DIRS = {'@eaDir', '$RECYCLE.BIN', 'System Volume Information', 'node_modules', '__pycache__'}

PROXY_ENCODER = os.environ.get('PROXY_ENCODER', 'libx264')
PROXY_HEIGHT = int(os.environ.get('PROXY_HEIGHT', '720'))
PROXY_WORKERS = max(1, int(os.environ.get('PROXY_WORKERS', '1')))
PREGENERATE = os.environ.get('PREGENERATE_PROXIES', '1') not in ('0', 'false', 'no')
DIRECT_MAX = int(os.environ.get('DIRECT_PLAY_MAX', '1920'))  # longest side a browser gets the original for

# HDR (PQ / HLG, BT.2020 – modern phones) -> SDR BT.709
TONEMAP = ('zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
           'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')

FN_TIME = re.compile(r'(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])[-_ .T]?'
                     r'([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d)')


def item_id(rel):
    return hashlib.sha1(rel.encode('utf-8')).hexdigest()[:12]


def _ensure_dirs():
    for d in (STATE, THUMBS, PROXIES):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- metadata

def time_from_name(name):
    m = FN_TIME.search(name)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def _exif_time_gps(im):
    t, gps = None, None
    try:
        ex = im.getexif()
    except Exception:
        return None, None
    try:
        sub = ex.get_ifd(0x8769)
        s = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
        if s:
            t = datetime.strptime(str(s).strip()[:19], '%Y:%m:%d %H:%M:%S')
    except Exception:
        t = None
    try:
        g = ex.get_ifd(0x8825)
        if g and 2 in g and 4 in g:
            f = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            lat, lon = f(g[2]), f(g[4])
            if g.get(1) == 'S':
                lat = -lat
            if g.get(3) == 'W':
                lon = -lon
            if lat or lon:
                gps = [round(lat, 6), round(lon, 6)]
    except Exception:
        gps = None
    return t, gps


def probe_photo(path, name):
    info = {}
    t, gps = None, None
    try:
        with Image.open(path) as im:
            t, gps = _exif_time_gps(im)
            w, h = im.size
            try:
                if im.getexif().get(0x0112) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
            info.update(w=w, h=h)
    except Exception as e:
        info['error'] = str(e)[:200]
    info['taken'] = t or time_from_name(name)
    info['gps'] = gps
    return info


def _ffprobe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
                         capture_output=True, timeout=60)
    return json.loads(out.stdout or b'{}')


def _parse_tag_time(tags):
    s = tags.get('com.apple.quicktime.creationdate')
    if s:  # local wall-clock time with offset
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00')[:25]).replace(tzinfo=None)
        except ValueError:
            pass
    s = tags.get('creation_time')
    if s:  # UTC
        try:
            d = datetime.fromisoformat(s.replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d.year > 1980:
                return d.astimezone(LOCAL_TZ).replace(tzinfo=None)
        except ValueError:
            pass
    return None


def probe_video(path, name):
    info = {}
    try:
        j = _ffprobe(path)
    except Exception as e:
        return {'error': str(e)[:200], 'taken': time_from_name(name)}
    fmt = j.get('format', {})
    vs = next((s for s in j.get('streams', []) if s.get('codec_type') == 'video' and
               not s.get('disposition', {}).get('attached_pic')), None)
    aus = next((s for s in j.get('streams', []) if s.get('codec_type') == 'audio'), None)
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    try:
        info['dur'] = round(float(fmt.get('duration') or (vs or {}).get('duration') or 0), 3)
    except ValueError:
        info['dur'] = 0
    if vs:
        w, h = int(vs.get('width') or 0), int(vs.get('height') or 0)
        rot = 0
        for sd in vs.get('side_data_list') or []:
            if 'rotation' in sd:
                rot = int(float(sd['rotation']))
        rot = rot or int((vs.get('tags') or {}).get('rotate', 0) or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        try:
            num, den = (vs.get('avg_frame_rate') or '0/1').split('/')
            fps = float(num) / float(den) if float(den) else 0
        except ValueError:
            fps = 0
        info.update(w=w, h=h, fps=round(fps, 3), vcodec=vs.get('codec_name'), pix=vs.get('pix_fmt'),
                    hdr=vs.get('color_transfer') in ('smpte2084', 'arib-std-b67'))
    else:
        info['error'] = 'no video stream'
    info['acodec'] = aus.get('codec_name') if aus else None
    info['taken'] = time_from_name(name) or _parse_tag_time(tags)
    return info


def play_mode(rel, info, lrf):
    ext = os.path.splitext(rel)[1].lower()
    if info.get('error'):
        return 'none'
    v, a = info.get('vcodec'), info.get('acodec')
    small = max(info.get('w', 0), info.get('h', 0)) <= DIRECT_MAX
    if not info.get('hdr') and small and a in (None, 'aac', 'mp3', 'opus', 'vorbis'):
        if ext in ('.mp4', '.m4v', '.mov') and v == 'h264' and info.get('pix') in ('yuv420p', 'yuvj420p'):
            return 'direct'
        if ext == '.webm' and v in ('vp8', 'vp9', 'av1'):
            return 'direct'
    if lrf:
        return 'lrf'
    return 'proxy'


# ---------------------------------------------------------------- library

class Library:
    def __init__(self):
        _ensure_dirs()
        self.lock = threading.RLock()
        self.items = {}          # id -> item
        self.generation = 0      # bumped after each finished scan
        self.status = dict(scanning=False, done=0, total=0, message='')
        self.proxies = ProxyQueue(self)
        self._thumb_locks = {}
        self._thumb_failed = set()
        self._thumb_sem = threading.Semaphore(4)
        try:
            cached = json.load(open(INDEX, encoding='utf-8'))
            self._cache = {it['path']: it for it in cached.get('items', [])}
        except Exception:
            self._cache = {}
        with self.lock:
            self.items = {it['id']: it for it in self._cache.values() if os.path.exists(self.abspath(it))}

    @staticmethod
    def abspath(item_or_rel):
        rel = item_or_rel['path'] if isinstance(item_or_rel, dict) else item_or_rel
        return os.path.join(MEDIA, *rel.split('/'))

    # -- scanning
    def _walk(self):
        vids, imgs, lrfs = [], [], {}
        for root, dirs, files in os.walk(MEDIA):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS
                             and os.path.abspath(os.path.join(root, d)) != STATE)
            for f in sorted(files):
                if f.startswith('.'):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, MEDIA).replace(os.sep, '/')
                stem, ext = os.path.splitext(rel)
                ext = ext.lower()
                if ext == '.lrf':  # DJI low-res preview next to the original
                    lrfs[stem.lower()] = rel
                elif ext in VIDEO_EXT:
                    vids.append(rel)
                elif ext in IMAGE_EXT:
                    imgs.append(rel)
        return vids, imgs, lrfs

    def _describe(self, rel, kind, lrf=None):
        full = self.abspath(rel)
        st = os.stat(full)
        old = self._cache.get(rel)
        if old and old.get('size') == st.st_size and old.get('mtime') == int(st.st_mtime) and old.get('lrf') == lrf \
                and old.get('v') == 2:
            return old
        name = os.path.basename(rel)
        info = probe_video(full, name) if kind == 'video' else probe_photo(full, name)
        taken = info.pop('taken', None) or datetime.fromtimestamp(st.st_mtime)
        parts = rel.split('/')
        it = dict(v=2, id=item_id(rel), path=rel, kind=kind, name=name,
                  src=parts[0] if len(parts) > 1 else '.', folder='/'.join(parts[:-1]) or '.',
                  size=st.st_size, mtime=int(st.st_mtime),
                  ts=taken.strftime('%Y-%m-%dT%H:%M:%S'), day=taken.strftime('%Y-%m-%d'), time=taken.strftime('%H:%M'),
                  **info)
        if kind == 'video':
            it['lrf'] = lrf
            it['play'] = play_mode(rel, info, lrf)
        return it

    def scan(self):
        with self.lock:
            if self.status['scanning']:
                return False
            self.status.update(scanning=True, done=0, total=0, message='listing files…')
        threading.Thread(target=self._scan, daemon=True).start()
        return True

    def _scan(self):
        try:
            vids, imgs, lrfs = self._walk()
            jobs = [(r, 'video', lrfs.get(os.path.splitext(r)[0].lower())) for r in vids] + [(r, 'photo', None) for r in imgs]
            self.status.update(total=len(jobs), message='reading metadata…')
            new = {}

            def one(job):
                try:
                    return self._describe(*job)
                except Exception as e:
                    print('scan error', job[0], e, flush=True)
                    return None
                finally:
                    self.status['done'] += 1

            with ThreadPoolExecutor(8) as ex:
                for it in ex.map(one, jobs):
                    if it:
                        new[it['id']] = it
            with self.lock:
                self._thumb_failed.clear()
                self.items = new
                self._cache = {it['path']: it for it in new.values()}
                self.generation += 1
            tmp = INDEX + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'items': list(new.values())}, f, ensure_ascii=False)
            os.replace(tmp, INDEX)
            nv = sum(1 for i in new.values() if i['kind'] == 'video')
            print(f'scan done: {nv} videos, {len(new) - nv} photos', flush=True)
            if PREGENERATE:
                for it in sorted((i for i in new.values() if i['kind'] == 'video' and i.get('play') == 'proxy'),
                                 key=lambda i: i['ts']):
                    self.proxies.add(it, prio=1)
        finally:
            self.status.update(scanning=False, message='')

    # -- views
    def listing(self):
        with self.lock:
            items = sorted(self.items.values(), key=lambda i: (i['ts'], i['path']))
        keep_v = ('id', 'path', 'name', 'src', 'folder', 'day', 'time', 'ts', 'dur', 'w', 'h', 'play', 'hdr', 'error')
        keep_p = ('id', 'path', 'name', 'src', 'folder', 'day', 'time', 'ts', 'w', 'h', 'gps', 'error')
        videos = [{k: i.get(k) for k in keep_v} for i in items if i['kind'] == 'video']
        photos = [{k: i.get(k) for k in keep_p} for i in items if i['kind'] == 'photo']
        return videos, photos

    def get(self, iid):
        with self.lock:
            return self.items.get(iid)

    # -- thumbnails
    def thumb(self, it):
        out = os.path.join(THUMBS, it['id'] + '.jpg')
        if os.path.exists(out):
            return out
        if it['id'] in self._thumb_failed or it.get('play') == 'none':
            return None
        with self.lock:
            lk = self._thumb_locks.setdefault(it['id'], threading.Lock())
        with lk, self._thumb_sem:
            if os.path.exists(out):
                return out
            tmp = out + '.tmp.jpg'
            try:
                if it['kind'] == 'photo':
                    with Image.open(self.abspath(it)) as im:
                        im.draft('RGB', (720, 720))
                        im = ImageOps.exif_transpose(im).convert('RGB')
                        im.thumbnail((360, 360))
                        im.save(tmp, quality=80)
                else:
                    src, hdr = self.abspath(it), it.get('hdr')
                    if it.get('lrf'):
                        src, hdr = self.abspath(it['lrf']), False
                    elif os.path.exists(self.proxies.path(it)):
                        src, hdr = self.proxies.path(it), False
                    t = min(2.0, (it.get('dur') or 0) / 2)
                    vf = (TONEMAP + ',' if hdr else '') + 'scale=320:180:force_original_aspect_ratio=increase,crop=320:180'
                    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', src, '-frames:v', '1', '-vf', vf, tmp],
                                   check=True, timeout=120)
                os.replace(tmp, out)
                return out
            except Exception as e:
                print('thumb failed', it['path'], e, flush=True)
                self._thumb_failed.add(it['id'])  # cleared by the next rescan
                return None

    # -- playable video file
    def playable(self, it):
        """-> (path or None, status dict)"""
        mode = it.get('play')
        if mode == 'direct':
            return self.abspath(it), {'ready': True, 'mode': mode}
        if mode == 'lrf':
            return self.abspath(it['lrf']), {'ready': True, 'mode': mode}
        if mode == 'proxy':
            p = self.proxies.path(it)
            if os.path.exists(p):
                return p, {'ready': True, 'mode': mode}
            st = self.proxies.add(it, prio=0)
            return None, dict(ready=False, mode=mode, **st)
        return None, {'ready': False, 'mode': mode, 'state': 'error', 'message': it.get('error') or 'not playable'}

    # -- full-size photo for the browser
    def full_photo(self, it):
        p = self.abspath(it)
        if os.path.splitext(p)[1].lower() in WEB_IMAGE_EXT:
            return p, None
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im).convert('RGB')
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=92)
            return None, buf.getvalue()

    # -- selections
    @staticmethod
    def load_selections():
        try:
            return json.load(open(SELECTIONS, encoding='utf-8'))
        except Exception:
            return {'version': 2, 'clips': {}, 'photos': {}, 'edits': {}}

    @staticmethod
    def save_selections(sel):
        sel['version'] = 2
        sel['saved'] = datetime.now().isoformat(timespec='seconds')
        tmp = SELECTIONS + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sel, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SELECTIONS)


class ProxyQueue:
    """Background ffmpeg transcodes to small H.264 MP4s the browser can play (HEVC, 10-bit, HDR, 4K, MKV, AVI…)."""

    def __init__(self, lib):
        self.lib = lib
        self.cv = threading.Condition()
        self.heap, self.seq = [], itertools.count()
        self.state = {}       # id -> queued | running | done | error
        self.errors = {}
        self.encoder = PROXY_ENCODER
        for _ in range(PROXY_WORKERS):
            threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def path(it):
        return os.path.join(PROXIES, it['id'] + '.mp4')

    def add(self, it, prio=1):
        with self.cv:
            s = self.state.get(it['id'])
            if os.path.exists(self.path(it)):
                self.state[it['id']] = 'done'
                return {'state': 'done'}
            if s == 'running':
                return {'state': 'running', 'message': 'making a preview…'}
            if s == 'error':
                return {'state': 'error', 'message': self.errors.get(it['id'], 'ffmpeg failed')}
            heapq.heappush(self.heap, (prio, next(self.seq), it['id']))
            self.state[it['id']] = 'queued'
            self.cv.notify()
            ahead = sum(1 for p, _, i in self.heap if p <= prio and self.state.get(i) == 'queued') - 1
            return {'state': 'queued', 'message': f'waiting for preview ({max(ahead, 0)} ahead)'}

    def counts(self):
        with self.cv:
            vals = list(self.state.values())
        return {k: vals.count(k) for k in ('queued', 'running', 'done', 'error')}

    def _worker(self):
        while True:
            with self.cv:
                while not self.heap:
                    self.cv.wait()
                _, _, iid = heapq.heappop(self.heap)
                if self.state.get(iid) != 'queued':
                    continue
                self.state[iid] = 'running'
            it = self.lib.get(iid)
            ok = it is not None and self._make(it)
            with self.cv:
                self.state[iid] = 'done' if ok else 'error'

    def _cmd(self, it, src, out, enc):
        vf = [TONEMAP] if it.get('hdr') else []
        if (it.get('fps') or 0) > 31:
            vf.append('fps=30')
        H = PROXY_HEIGHT
        vf.append(f'scale=w={H * 16 // 9}:h={H * 16 // 9}:force_original_aspect_ratio=decrease:force_divisible_by=2'
                  if (it.get('h') or 0) > (it.get('w') or 0) else
                  f'scale=w=-2:h=min({H}\\,ih):force_divisible_by=2')
        vf.append('format=yuv420p')
        if enc == 'libx264':
            venc = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26']
        elif enc == 'h264_nvenc':
            venc = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '26']
        else:
            venc = ['-c:v', enc, '-b:v', '3M']
        return ['ffmpeg', '-v', 'error', '-y', '-i', src, '-map', '0:v:0', '-map', '0:a:0?', '-vf', ','.join(vf),
                *venc, '-g', '30', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-movflags', '+faststart', out]

    def _make(self, it):
        out = self.path(it)
        tmp = out + '.part.mp4'
        src = self.lib.abspath(it)
        t0 = time.time()
        for enc in dict.fromkeys([self.encoder, 'libx264']):
            r = subprocess.run(self._cmd(it, src, tmp, enc), capture_output=True)
            if r.returncode == 0 and os.path.exists(tmp):
                os.replace(tmp, out)
                print(f'preview ok ({enc}, {time.time() - t0:.0f}s): {it["path"]}', flush=True)
                return True
            msg = r.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or ['ffmpeg failed']
            print(f'preview failed ({enc}): {it["path"]}: {msg[0]}', flush=True)
            self.errors[it['id']] = msg[0][:300]
            if enc != 'libx264':
                self.encoder = 'libx264'  # don't retry a broken hardware encoder for every clip
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
