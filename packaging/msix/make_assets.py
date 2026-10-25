"""Draw the Store logos and the .exe icon (a speedometer on a rounded tile).

Generated rather than committed as binaries so the artwork is reviewable and
every size is rendered crisp from vectors instead of scaled from one bitmap.
Usage: python make_assets.py <out_dir>   (needs Pillow)
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

BG = (37, 99, 235)          # SysPerf blue
FG = (255, 255, 255)
OK = (34, 197, 94)


def tile(w, h, pad_frac=0.0, transparent=False):
    """A w x h image with the gauge centred; drawn at 4x and downsampled."""
    s = 4
    W, H = w * s, h * s
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    side = min(W, H) * (1 - 2 * pad_frac)
    cx, cy = W / 2, H / 2
    x0, y0 = cx - side / 2, cy - side / 2
    if not transparent:
        d.rounded_rectangle([x0, y0, x0 + side, y0 + side], radius=side * 0.22, fill=BG)
    r = side * 0.32
    gy = cy + side * 0.08
    lw = max(2, int(side * 0.075))
    box = [cx - r, gy - r, cx + r, gy + r]
    d.arc(box, 150, 390, fill=FG, width=lw)
    d.arc(box, 330, 390, fill=OK, width=lw)
    ang = math.radians(-35)
    d.line([cx, gy, cx + r * 0.85 * math.cos(ang), gy + r * 0.85 * math.sin(ang)],
           fill=FG, width=lw)
    k = lw * 0.9
    d.ellipse([cx - k, gy - k, cx + k, gy + k], fill=FG)
    return im.resize((w, h), Image.LANCZOS)


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    sizes = {"Square44x44Logo": (44, 44, 0.0), "Square150x150Logo": (150, 150, 0.18),
             "Wide310x150Logo": (310, 150, 0.12), "StoreLogo": (50, 50, 0.0),
             "SmallTile": (71, 71, 0.12), "LargeTile": (310, 310, 0.2)}
    for name, (w, h, pad) in sizes.items():
        tile(w, h, pad).save(out / f"{name}.png")
    # Taskbar / Start list icon without the plate, as Windows expects.
    tile(44, 44, 0.0).save(out / "Square44x44Logo.targetsize-44_altform-unplated.png")
    tile(256, 256).save(out / "SysPerf.ico",
                        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Assets")
