"""media-sorter – browse a folder of videos and photos, mark the good pieces, rate, light-fix and crop photos.
Selections are written to STATE_DIR/selections.json for whatever builds the final edit.

  MEDIA_DIR   folder to scan (default /media)
  STATE_DIR   where thumbnails, previews and selections go (default MEDIA_DIR/.media-sorter)
  PORT        HTTP port (default 8765)
  TITLE       name shown in the header
"""
import io
import json
import mimetypes
import os
import re
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import photofx  # noqa: E402
from library import MEDIA, STATE, Library  # noqa: E402

PORT = int(os.environ.get('PORT', '8765'))
HOST = os.environ.get('HOST', '0.0.0.0')
TITLE = os.environ.get('TITLE') or os.path.basename(MEDIA.rstrip('/\\')) or 'media'
VERSION = '1.0.0'

LIB = Library()
_cache, _clock = OrderedDict(), threading.Lock()
_save_lock = threading.Lock()


def preview_base(it, side=1800):
    key = (it['id'], side)
    with _clock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    im = photofx.load(LIB.abspath(it), side)
    with _clock:
        _cache[key] = im
        while len(_cache) > 60:
            _cache.popitem(last=False)
    return im


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def send_bytes(self, b, ctype, cache=True, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8',
                        cache=False, code=code)

    def send_file(self, path, ctype=None, cache=True):
        if not path or not os.path.exists(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        start, end = 0, size - 1
        m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(path, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def status(self):
        s = dict(LIB.status)
        s.update(generation=LIB.generation, previews=LIB.proxies.counts())
        return s

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p in ('/', '/index.html'):
                return self.send_file(os.path.join(HERE, 'static', 'index.html'), 'text/html; charset=utf-8', cache=False)
            if p == '/api/data':
                videos, photos = LIB.listing()
                return self.send_json(dict(title=TITLE, version=VERSION, media=MEDIA, state=STATE, videos=videos,
                                           photos=photos, sel=LIB.load_selections(), status=self.status()))
            if p == '/api/status':
                return self.send_json(self.status())
            if p == '/api/selections':
                return self.send_json(LIB.load_selections())
            m = re.match(r'^/(thumb|video|img|full|api/video|api/auto)/([0-9a-f]{12})$', p)
            if not m:
                return self.send_error(404)
            kind, it = m.group(1), LIB.get(m.group(2))
            if not it:
                return self.send_error(404)
            if kind == 'thumb':
                return self.send_file(LIB.thumb(it), 'image/jpeg')
            if kind == 'api/video':
                _, st = LIB.playable(it)
                return self.send_json(st)
            if kind == 'video':
                path, st = LIB.playable(it)
                if not path:
                    return self.send_json(st, code=409)
                ct = 'video/webm' if path.lower().endswith('.webm') else 'video/mp4'
                return self.send_file(path, ct)
            if it['kind'] != 'photo':
                return self.send_error(404)
            if kind == 'full':
                path, data = LIB.full_photo(it)
                return self.send_file(path) if path else self.send_bytes(data, 'image/jpeg')
            if kind == 'api/auto':
                return self.send_json(photofx.auto_adj(preview_base(it)))
            if kind == 'img':  # adjusted preview: /img/<id>?w=1600&exp=..&crop=x,y,w,h
                side = max(64, min(4000, int(float(q.get('w', 1600)))))
                im = preview_base(it, 1800 if side > 400 else 400).copy()
                if max(im.size) > side:
                    im.thumbnail((side, side))
                adj = {k: float(q[k]) for k in photofx.KEYS if k in q}
                im = photofx.apply_adj(im, adj)
                if q.get('crop'):
                    x, y, w, h = (float(v) for v in q['crop'].split(','))
                    im = photofx.crop_img(im, dict(x=x, y=y, w=w, h=h))
                buf = io.BytesIO()
                im.save(buf, 'JPEG', quality=88)
                return self.send_bytes(buf.getvalue(), 'image/jpeg')
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        except Exception as e:  # keep the server alive, tell the browser
            print('error', self.path, repr(e), flush=True)
            try:
                return self.send_json({'error': str(e)}, code=500)
            except Exception:
                return
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n) if n else b''
        if p == '/api/save':
            try:
                sel = json.loads(body.decode('utf-8'))
                assert isinstance(sel, dict)
            except Exception:
                return self.send_json({'ok': False, 'error': 'bad json'}, code=400)
            with _save_lock:
                LIB.save_selections(sel)
            return self.send_json({'ok': True})
        if p == '/api/rescan':
            return self.send_json({'ok': LIB.scan()})
        self.send_error(404)


def main():
    if not os.path.isdir(MEDIA):
        sys.exit(f'MEDIA_DIR {MEDIA} does not exist – mount your folder there (docker run -v /path/to/folder:/media …)')
    print(f'media-sorter {VERSION}: {MEDIA} (state in {STATE})', flush=True)
    LIB.scan()
    print(f'open http://localhost:{PORT}', flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == '__main__':
    main()
