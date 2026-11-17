"""Template video renderer: turns a script into a vertical MP4 with Pillow and ffmpeg.

No video model, no GPU, no cost. Each script line becomes a slide of large
text over a gradient, with a spinning 3D wireframe model as the motion
element. Swap this file for a real video generator later; the rest of the
factory only needs `render_short(cfg, script, path) -> seconds`.
"""
import math
import shutil
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PALETTES = [((14, 26, 48), (10, 143, 220)), ((32, 16, 44), (232, 92, 140)), ((10, 36, 34), (40, 190, 160))]


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg not found: run pip install imageio-ffmpeg") from None
        return exe


def _font(size):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _gradient(w, h, top, bottom):
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t * 0.35) for i in range(3)))
    return col.resize((w, h))


def _slide(w, h, text, palette, big):
    img = _gradient(w, h, palette[0], palette[1])
    d = ImageDraw.Draw(img)
    size = int(w * (0.095 if big else 0.07))
    font = _font(size)
    wrapped = textwrap.wrap(text, width=max(8, int(w / (size * 0.58))))[:6]
    line_h = int(size * 1.25)
    y = int(h * 0.62) - line_h * len(wrapped) // 2
    for line in wrapped:
        tw = d.textlength(line, font=font)
        x = (w - tw) / 2
        d.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        d.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h
    return img


CUBE = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
EDGES = [(a, b) for a in range(8) for b in range(a + 1, 8) if sum(CUBE[a][i] != CUBE[b][i] for i in range(3)) == 1]


def _draw_model(d, cx, cy, r, angle, color):
    """A wireframe cube spinning in 3D, projected to 2D."""
    pts = []
    for x, y, z in CUBE:
        x, z = x * math.cos(angle) - z * math.sin(angle), x * math.sin(angle) + z * math.cos(angle)
        y, z = y * math.cos(angle * 0.6) - z * math.sin(angle * 0.6), y * math.sin(angle * 0.6) + z * math.cos(angle * 0.6)
        f = 3 / (z + 4.2)
        pts.append((cx + x * r * f, cy + y * r * f))
    for a, b in EDGES:
        d.line([pts[a], pts[b]], fill=color, width=max(2, int(r / 25)))
    for p in pts:
        d.ellipse([p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5], fill=(255, 255, 255))


def render_short(cfg, script: dict, out: Path) -> float:
    w = int(cfg.get("video.width", 720))
    h = int(cfg.get("video.height", 1280))
    fps = int(cfg.get("video.fps", 24))
    per_line = float(cfg.get("video.seconds_per_line", 3.5))
    texts = [script["hook"] or script["lines"][0]] + script["lines"]
    palette = PALETTES[sum(map(ord, script["caption"])) % len(PALETTES)]
    slides = [_slide(w, h, t, palette, i == 0) for i, t in enumerate(texts)]
    frames_per = int(per_line * fps)
    total = frames_per * len(slides)

    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    thumb = None
    try:
        for i in range(total):
            frame = slides[i // frames_per].copy()
            d = ImageDraw.Draw(frame)
            _draw_model(d, w / 2, h * 0.27, w * 0.22, i / fps * 1.4, palette[1])
            d.rectangle([0, h - 14, w * (i + 1) / total, h], fill=palette[1])
            if i == frames_per // 2:
                thumb = frame
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err.strip()[:300]}")
    (thumb or slides[0]).convert("RGB").save(out.with_suffix(".jpg"), quality=80)
    return total / fps
