"""Non-destructive photo adjustments (used by the server for previews; reusable by builders that apply the edits).
adj keys (all optional, 0 = neutral):
  exp  exposure in EV (-2..+2)       hl   highlight recovery (0..1)
  sh   shadow lift (0..1)            con  contrast (-1..1)
  sat  saturation (-1..1)            tmp  warmth (-1..1, + = warmer)
crop: {x, y, w, h} normalised 0..1 on the (EXIF-rotated) image.
"""
import numpy as np
from PIL import Image, ImageOps

KEYS = ('exp', 'hl', 'sh', 'con', 'sat', 'tmp')


def _smooth(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def apply_adj(im, adj):
    """im: PIL RGB -> PIL RGB with adjustments."""
    if not adj or not any(float(adj.get(k, 0) or 0) for k in KEYS):
        return im
    a = {k: float(adj.get(k, 0) or 0) for k in KEYS}
    v = np.asarray(im, dtype=np.float32) / 255.0
    L0 = v.max(axis=2)  # original brightness: fully clipped areas (blown sky) must stay bright, not turn grey
    if a['exp']:
        lin = np.power(v, 2.2) * (2.0 ** a['exp'])
        v = np.power(np.clip(lin, 0, None), 1 / 2.2)
    if a['tmp']:
        v[..., 0] *= 1 + 0.10 * a['tmp']
        v[..., 2] *= 1 - 0.10 * a['tmp']
    L = v[..., 0] * 0.2126 + v[..., 1] * 0.7152 + v[..., 2] * 0.0722
    L2 = L.copy()
    if a['hl']:  # pull highlights down smoothly (keeps colour ratios)
        L2 = L2 - a['hl'] * 0.55 * _smooth(0.45, 1.05, L2) * (L2 - 0.45)
    if a['sh']:
        L2 = L2 + a['sh'] * 0.45 * (1 - _smooth(0.0, 0.55, L2)) * (0.55 - L2)
    if a['con']:
        c = a['con']
        L2 = L2 + c * (L2 - 0.5) * (1 - np.abs(2 * L2 - 1)) * 1.2
    prot = _smooth(0.90, 0.995, L0)
    L2 = L2 + prot * (np.maximum(L2, 0.91 + 0.06 * L0) - L2)
    ratio = np.where(L > 1e-4, L2 / np.maximum(L, 1e-4), 1.0)
    v = v * ratio[..., None]
    if a['sat']:
        Lc = (v[..., 0] * 0.2126 + v[..., 1] * 0.7152 + v[..., 2] * 0.0722)[..., None]
        v = Lc + (v - Lc) * (1 + a['sat'])
    v = np.clip(v, 0, 1)
    return Image.fromarray((v * 255 + 0.5).astype(np.uint8))


def auto_adj(im):
    """Suggest settings for an image (aimed at over-exposed phone shots)."""
    small = im.copy()
    small.thumbnail((400, 400))
    v = np.asarray(small, dtype=np.float32) / 255.0
    L = v[..., 0] * 0.2126 + v[..., 1] * 0.7152 + v[..., 2] * 0.0722
    p5, p50, p99 = np.percentile(L, [5, 50, 99])
    clipped = float((v.max(axis=2) > 0.985).mean())
    adj = dict(exp=0.0, hl=0.0, sh=0.0, con=0.0, sat=0.0, tmp=0.0)
    if p50 > 0.56:
        adj['exp'] = round(float(np.clip(np.log2(0.50 / p50) * 1.2, -1.0, 0)), 2)
    elif p50 < 0.28:
        adj['exp'] = round(float(np.clip(np.log2(0.40 / max(p50, 0.05)), 0, 1.2)), 2)
    if p99 > 0.93 or clipped > 0.01:
        adj['hl'] = round(float(np.clip(0.30 + clipped * 3, 0.30, 0.7)), 2)
    if p5 < 0.04:
        adj['sh'] = 0.25
    if adj['exp'] < 0 or adj['hl'] > 0.5:
        adj['con'] = 0.15
        adj['sat'] = 0.12
    return adj


def load(path, max_side=None):
    im = Image.open(path)
    if max_side:
        im.draft('RGB', (max_side, max_side))  # fast JPEG downscale on decode
    im = ImageOps.exif_transpose(im).convert('RGB')
    if max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def crop_img(im, crop):
    if not crop:
        return im
    W, H = im.size
    x, y, w, h = (float(crop[k]) for k in ('x', 'y', 'w', 'h'))
    box = (int(round(x * W)), int(round(y * H)), int(round((x + w) * W)), int(round((y + h) * H)))
    return im.crop(box)


def render(path, edit=None, max_side=None, do_crop=True):
    """Full pipeline for builders: load, adjust, crop."""
    im = load(path, max_side)
    edit = edit or {}
    im = apply_adj(im, edit.get('adj'))
    if do_crop:
        im = crop_img(im, edit.get('crop'))
    return im
