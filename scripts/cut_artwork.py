#!/usr/bin/env python3
"""Extract isolated transparent artwork from the committed source sheets."""
from pathlib import Path
from collections import deque
from PIL import Image
import colorsys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "clean"

SHEETS = [
    ("1795742852204.jpg", "slots/olympus", 5, 3),
    ("1795742875494.jpg", "slots/sweet", 5, 3),
    ("1795742884706.jpg", "slots/sugar", 4, 3),
    ("1795742892974.jpg", "rewards", 3, 2),
    ("1795742902056.jpg", "cases", 3, 2),
]
CASE_NAMES = {
    (0, 0): "starter", (0, 1): "neon", (0, 2): "royal",
    (1, 0): "crystal", (1, 1): "legend", (1, 2): "cosmic",
}

def erase_edge_checker(cell):
    """Erase only neutral checker pixels connected to the crop edge."""
    im = cell.convert("RGBA")
    px = im.load()
    w, h = im.size
    seen = bytearray(w * h)
    q = deque()

    def background(x, y):
        r, g, b, a = px[x, y]
        return a > 0 and max(r, g, b) - min(r, g, b) <= 26 and (r + g + b) / 3 <= 218

    def push(x, y):
        k = y * w + x
        if not seen[k] and background(x, y):
            seen[k] = 1
            q.append((x, y))

    for x in range(w):
        push(x, 0); push(x, h - 1)
    for y in range(h):
        push(0, y); push(w - 1, y)

    while q:
        x, y = q.popleft()
        px[x, y] = (0, 0, 0, 0)
        if x: push(x - 1, y)
        if x + 1 < w: push(x + 1, y)
        if y: push(x, y - 1)
        if y + 1 < h: push(x, y + 1)
    return im

def keep_art_components(im):
    """Drop isolated caption/noise components while preserving multi-part art."""
    a = im.getchannel("A")
    w, h = im.size
    mask = a.load()
    seen = bytearray(w * h)
    comps = []
    for y in range(h):
        for x in range(w):
            k = y * w + x
            if seen[k] or mask[x, y] == 0:
                continue
            q = deque([(x, y)])
            seen[k] = 1
            area = 0
            x0 = x1 = x
            y0 = y1 = y
            while q:
                xx, yy = q.popleft()
                area += 1
                x0 = min(x0, xx); x1 = max(x1, xx)
                y0 = min(y0, yy); y1 = max(y1, yy)
                for nx, ny in ((xx-1,yy),(xx+1,yy),(xx,yy-1),(xx,yy+1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nk = ny*w+nx
                        if not seen[nk] and mask[nx,ny] > 0:
                            seen[nk] = 1
                            q.append((nx,ny))
            comps.append((area, (x0,y0,x1+1,y1+1)))

    if not comps:
        return im
    largest = max(area for area, _ in comps)
    # Keep substantial detached pieces (sparkles, leaves, candy pieces), not tiny text.
    keep = [bbox for area, bbox in comps if area >= max(18, largest * 0.035)]
    cleaned = Image.new("RGBA", im.size, (0,0,0,0))
    for bbox in keep:
        part = im.crop(bbox)
        cleaned.alpha_composite(part, (bbox[0], bbox[1]))
    return cleaned

def fit_canvas(im):
    bbox = im.getchannel("A").getbbox()
    canvas = Image.new("RGBA", (512,512), (0,0,0,0))
    if not bbox:
        return canvas
    obj = im.crop(bbox)
    scale = min(440 / obj.width, 440 / obj.height)
    obj = obj.resize((max(1,round(obj.width*scale)), max(1,round(obj.height*scale))), Image.Resampling.LANCZOS)
    canvas.alpha_composite(obj, ((512-obj.width)//2, (512-obj.height)//2))
    return canvas

def main():
    for filename, folder, cols, rows in SHEETS:
        source = ROOT / filename
        if not source.exists():
            raise SystemExit(f"Missing source sheet: {source}")
        sheet = Image.open(source).convert("RGB")
        w, h = sheet.size
        for row in range(rows):
            for col in range(cols):
                cell = sheet.crop((round(col*w/cols), round(row*h/rows),
                                   round((col+1)*w/cols), round((row+1)*h/rows)))
                clean = fit_canvas(keep_art_components(erase_edge_checker(cell)))
                target = OUT / folder / f"r{row}c{col}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                clean.save(target, optimize=True)
                if folder == "cases":
                    name = CASE_NAMES[(row,col)]
                    named = OUT / "cases" / f"{name}.png"
                    clean.save(named, optimize=True)
                    print(f"case {name}: {named.relative_to(ROOT)}")
                print(f"{target.relative_to(ROOT)} {clean.size} RGBA")

if __name__ == "__main__":
    main()
