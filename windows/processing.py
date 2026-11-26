"""Picture processing on the PC: framing (digital zoom/pan, rotate, mirror),
colour adjustments, pause screen and placeholders.

Colour adjustments are done as ONE lookup table on the YCbCr picture that the JPEG
decoder produces anyway, so they cost almost nothing per frame.
"""

import io

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

DEFAULT_ADJUST = {"brightness": 0, "contrast": 0, "saturation": 0, "warmth": 0, "tint": 0}
DEFAULT_FRAMING = {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0, "rotate": 0, "mirror": False, "flip": False}

# Quick looks: each filter is just a preset of the adjustments above.
FILTERS = {
    "None": {},
    "Vivid": {"saturation": 35, "contrast": 15},
    "Warm": {"warmth": 35, "saturation": 10},
    "Cool": {"warmth": -35},
    "Bright": {"brightness": 20, "contrast": -5},
    "Soft": {"contrast": -20, "saturation": -10, "brightness": 5},
    "Mono": {"saturation": -100, "contrast": 10},
}

_BLACK = {"RGB": (0, 0, 0), "YCbCr": (0, 128, 128)}

_ROTATE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def _clamp(v):
    return 0 if v < 0 else 255 if v > 255 else int(round(v))


def build_lut(adjust):
    """Returns a 768-entry YCbCr lookup table, or None if nothing is changed."""
    a = {k: float(adjust.get(k, 0)) for k in DEFAULT_ADJUST}
    if all(abs(v) < 0.5 for v in a.values()):
        return None
    bright = a["brightness"] / 100.0 * 60.0
    contrast = 1.0 + a["contrast"] / 100.0 * 0.8
    sat = max(0.0, 1.0 + a["saturation"] / 100.0)
    warm = a["warmth"] / 100.0 * 22.0
    tint = a["tint"] / 100.0 * 16.0
    y = [_clamp((v - 128) * contrast + 128 + bright) for v in range(256)]
    cb = [_clamp((v - 128) * sat + 128 - warm + tint) for v in range(256)]
    cr = [_clamp((v - 128) * sat + 128 + warm + tint) for v in range(256)]
    return y + cb + cr


def fit_to(img, width, height):
    """Scales the picture to fit, adding black bars if needed."""
    iw, ih = img.size
    if (iw, ih) == (width, height):
        return img
    scale = min(width / iw, height / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), Image.BILINEAR, reducing_gap=2.0 if scale < 0.5 else None)
    if (nw, nh) == (width, height):
        return img
    canvas = Image.new(img.mode, (width, height), _BLACK.get(img.mode, 0))
    canvas.paste(img, ((width - nw) // 2, (height - nh) // 2))
    return canvas


def process(jpeg, width, height, framing=None, lut=None):
    """JPEG bytes from the iPhone -> finished RGB picture of exactly width x height."""
    framing = framing or DEFAULT_FRAMING
    rotate = int(framing.get("rotate", 0)) % 360
    zoom = max(1.0, float(framing.get("zoom", 1.0)))

    img = Image.open(io.BytesIO(jpeg))
    need_w, need_h = (width, height) if rotate in (0, 180) else (height, width)
    want = "YCbCr" if lut else "RGB"
    img.draft(want, (int(need_w * zoom), int(need_h * zoom)))  # cheap shrink while decoding
    img.load()
    if img.mode != want:
        img = img.convert(want)

    if rotate in _ROTATE:
        img = img.transpose(_ROTATE[rotate])

    if zoom > 1.001:
        w, h = img.size
        cw, ch = w / zoom, h / zoom
        px = max(-1.0, min(1.0, float(framing.get("pan_x", 0.0))))
        py = max(-1.0, min(1.0, float(framing.get("pan_y", 0.0))))
        cx = w / 2 + px * (w - cw) / 2
        cy = h / 2 + py * (h - ch) / 2
        img = img.crop((int(cx - cw / 2), int(cy - ch / 2), int(cx + cw / 2), int(cy + ch / 2)))

    if framing.get("mirror"):
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if framing.get("flip"):
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    img = fit_to(img, width, height)
    if lut:
        img = img.point(lut).convert("RGB")
    return img


# ---------------------------------------------------------------------------
# Screens shown instead of video
# ---------------------------------------------------------------------------

def load_font(size, bold=False):
    names = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else \
            ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _centered_text(draw, width, y, text, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def make_placeholder(width, height, line1, line2=""):
    img = Image.new("RGB", (width, height), (18, 24, 38))
    draw = ImageDraw.Draw(img)
    _centered_text(draw, width, height * 0.42, line1, load_font(max(20, height // 14), True), (235, 240, 250))
    if line2:
        _centered_text(draw, width, height * 0.56, line2, load_font(max(14, height // 30)), (150, 165, 190))
    return img


def make_pause_screen(last, width, height):
    """Blurred, darkened copy of the last picture with 'Camera paused' on top."""
    if last is None:
        base = Image.new("RGB", (width, height), (18, 24, 38))
    else:
        small = last.convert("RGB").resize((max(1, width // 10), max(1, height // 10)), Image.BILINEAR)
        base = small.filter(ImageFilter.GaussianBlur(3)).resize((width, height), Image.BILINEAR)
        base = ImageEnhance.Brightness(base).enhance(0.55)
    draw = ImageDraw.Draw(base)
    r = height // 14
    cx, cy = width // 2, int(height * 0.40)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255))
    for x0, x1 in ((-0.38, -0.12), (0.12, 0.38)):
        draw.rectangle([cx + r * x0, cy - r * 0.45, cx + r * x1, cy + r * 0.45], fill=(30, 40, 60))
    _centered_text(draw, width, cy + r * 1.5, "Camera paused", load_font(max(20, height // 16), True), (245, 248, 255))
    return base
