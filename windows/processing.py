"""Picture processing on the PC: framing (digital zoom/pan, rotate, mirror),
colour adjustments, pause screen and placeholders.

Colour adjustments are done as ONE lookup table on the YCbCr picture that the JPEG
decoder produces anyway, so they cost almost nothing per frame.
"""

import io

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

DEFAULT_ADJUST = {"brightness": 0, "contrast": 0, "saturation": 0, "vibrance": 0, "warmth": 0, "tint": 0,
                  "hue": 0, "gamma": 0, "sharpness": 0, "backlight": 0}
DEFAULT_FRAMING = {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0, "rotate": 0, "mirror": False, "flip": False}

# Quick looks: each filter is just a preset of the adjustments above.
FILTERS = {
    "None": {},
    "Vivid": {"saturation": 20, "vibrance": 35, "contrast": 15, "sharpness": 25},
    "Warm": {"warmth": 35, "saturation": 10},
    "Cool": {"warmth": -35},
    "Bright": {"brightness": 10, "backlight": 40, "gamma": 15},
    "Soft": {"contrast": -20, "saturation": -10, "brightness": 5},
    "Mono": {"saturation": -100, "contrast": 10},
}

_BLACK = {"RGB": (0, 0, 0), "YCbCr": (0, 128, 128)}

_ROTATE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def _clamp(v):
    return 0 if v < 0 else 255 if v > 255 else int(round(v))


class Look:
    """Everything needed to colour-correct a picture, prepared once per settings change."""

    def __init__(self, lut=None, hue=0.0, sharpness=0):
        self.lut = lut              # 768-entry YCbCr table (or None)
        self.hue_matrix = _hue_matrix(hue) if abs(hue) >= 1 else None
        self.sharpness = sharpness  # 0..100

    def __bool__(self):
        return bool(self.lut) or self.hue_matrix is not None or self.sharpness > 0


def _hue_matrix(degrees):
    import math
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    lr, lg, lb = 0.213, 0.715, 0.072
    m = (
        lr + c * (1 - lr) + s * (-lr), lg + c * (-lg) + s * (-lg), lb + c * (-lb) + s * (1 - lb), 0,
        lr + c * (-lr) + s * 0.143, lg + c * (1 - lg) + s * 0.140, lb + c * (-lb) + s * (-0.283), 0,
        lr + c * (-lr) + s * (-(1 - lr)), lg + c * (-lg) + s * lg, lb + c * (1 - lb) + s * lb, 0,
    )
    return m


def build_lut(adjust):
    """Returns a Look for the adjustments, or None if nothing is changed."""
    a = {k: float(adjust.get(k, 0)) for k in DEFAULT_ADJUST}
    if all(abs(v) < 0.5 for v in a.values()):
        return None
    table = None
    if any(abs(a[k]) >= 0.5 for k in ("brightness", "contrast", "saturation", "vibrance", "warmth",
                                       "tint", "gamma", "backlight")):
        bright = a["brightness"] / 100.0 * 60.0
        contrast = 1.0 + a["contrast"] / 100.0 * 0.8
        sat = max(0.0, 1.0 + a["saturation"] / 100.0)
        vib = a["vibrance"] / 100.0
        warm = a["warmth"] / 100.0 * 22.0
        tint = a["tint"] / 100.0 * 16.0
        gamma = 2.0 ** (-a["gamma"] / 100.0)
        lift = max(0.0, a["backlight"]) / 100.0 * 70.0
        y = []
        for v in range(256):
            t = (v - 128) * contrast + 128 + bright
            t = 255.0 * (max(0.0, min(255.0, t)) / 255.0) ** gamma
            t += lift * (1.0 - t / 255.0) ** 2        # lift dark areas (backlit faces)
            y.append(_clamp(t))

        def chroma(v, shift):
            d = (v - 128) * sat
            d *= 1.0 + vib * (1.0 - min(1.0, abs(d) / 110.0))  # boosts dull colours more
            return _clamp(128 + d + shift)

        cb = [chroma(v, -warm + tint) for v in range(256)]
        cr = [chroma(v, warm + tint) for v in range(256)]
        table = y + cb + cr
    look = Look(table, a["hue"], max(0.0, a["sharpness"]))
    return look if look else None


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


def process(jpeg, width, height, framing=None, lut=None, sharpen=True):
    """JPEG bytes from the phone -> finished RGB picture of exactly width x height."""
    framing = framing or DEFAULT_FRAMING
    rotate = int(framing.get("rotate", 0)) % 360
    zoom = max(1.0, float(framing.get("zoom", 1.0)))
    img = Image.open(io.BytesIO(jpeg))
    need_w, need_h = (width, height) if rotate in (0, 180) else (height, width)
    want = "YCbCr" if (lut and lut.lut) else "RGB"
    img.draft(want, (int(need_w * zoom), int(need_h * zoom)))  # cheap shrink while decoding
    img.load()
    return process_image(img, width, height, framing, lut, sharpen)


def is_plain(framing, lut):
    """True when nothing needs changing (the fast path can be used)."""
    if lut:
        return False
    f = framing or DEFAULT_FRAMING
    return (float(f.get("zoom", 1.0)) <= 1.001 and int(f.get("rotate", 0)) % 360 == 0
            and not f.get("mirror") and not f.get("flip"))


def process_image(img, width, height, framing=None, lut=None, sharpen=True):
    """Any picture -> framing + colour adjustments -> RGB picture of exactly width x height."""
    framing = framing or DEFAULT_FRAMING
    rotate = int(framing.get("rotate", 0)) % 360
    zoom = max(1.0, float(framing.get("zoom", 1.0)))
    look = lut
    want = "YCbCr" if (look and look.lut) else "RGB"
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
    if look:
        if look.lut:
            img = img.point(look.lut).convert("RGB")
        if look.hue_matrix is not None:
            img = img.convert("RGB", look.hue_matrix)
        if look.sharpness > 0 and sharpen:
            k = look.sharpness / 100.0 * 0.9
            img = img.filter(ImageFilter.Kernel((3, 3), [0, -k, 0, -k, 1 + 4 * k, -k, 0, -k, 0], 1))
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
