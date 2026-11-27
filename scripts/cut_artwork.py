#!/usr/bin/env python3
"""Cut the committed artwork sheets into independent transparent PNG assets."""
from pathlib import Path
from PIL import Image
from collections import deque
import math

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/"assets"
CASE_ALIASES={(0,0):("starter",),(0,1):("neon",),(0,2):("royal",),(1,0):("crystal",),(1,1):("cosmic","legend")}

SHEETS=[
 ("1795742852204.jpg","slots/olympus",5,3),
 ("1795742875494.jpg","slots/sweet",5,3),
 ("1795742884706.jpg","slots/sugar",4,3),
 ("1795742892974.jpg","cases",3,2),
 ("1795742902056.jpg","rewards",3,2),
]

def remove_checkerboard(im):
    im=im.convert("RGBA")
    px=im.load(); w,h=im.size
    # The uploaded sheets use a neutral grey/near-black checkerboard.
    # Only remove neutral pixels that are connected to the cell boundary;
    # saturated artwork and enclosed neutral details remain intact.
    seen=bytearray(w*h); q=deque()
    def is_bg(x,y):
        r,g,b,a=px[x,y]
        mx=max(r,g,b); mn=min(r,g,b)
        lum=(r+g+b)/3
        return a>0 and (mx-mn)<=20 and 22<=lum<=178
    def add(x,y):
        k=y*w+x
        if not seen[k] and is_bg(x,y):
            seen[k]=1; q.append((x,y))
    for x in range(w):
        add(x,0); add(x,h-1)
    for y in range(h):
        add(0,y); add(w-1,y)
    while q:
        x,y=q.popleft()
        px[x,y]=(0,0,0,0)
        if x: add(x-1,y)
        if x+1<w: add(x+1,y)
        if y: add(x,y-1)
        if y+1<h: add(x,y+1)
    return im

def trim_and_pad(cell):
    alpha=cell.getchannel("A")
    bbox=alpha.getbbox()
    if not bbox: return Image.new("RGBA",(512,512),(0,0,0,0))
    obj=cell.crop(bbox)
    # Keep a generous safety margin so CSS/object-fit never clips an edge.
    max_side=440
    scale=min(max_side/obj.width,max_side/obj.height)
    obj=obj.resize((max(1,round(obj.width*scale)),max(1,round(obj.height*scale))),Image.Resampling.LANCZOS)
    out=Image.new("RGBA",(512,512),(0,0,0,0))
    out.alpha_composite(obj,((512-obj.width)//2,(512-obj.height)//2))
    return out

def main():
    for filename,folder,cols,rows in SHEETS:
        src=ROOT/filename
        if not src.exists(): raise SystemExit(f"Missing source sheet: {src}")
        sheet=Image.open(src).convert("RGB")
        w,h=sheet.size
        # Exact equal-cell slicing follows the documented sheet layouts.
        for row in range(rows):
            for col in range(cols):
                x0=round(col*w/cols); x1=round((col+1)*w/cols)
                y0=round(row*h/rows); y1=round((row+1)*h/rows)
                cell=sheet.crop((x0,y0,x1,y1))
                # Case labels sit below the artwork; exclude that caption strip.
                if folder=="cases":
                    cell=cell.crop((0,0,cell.width,round(cell.height*0.78)))
                cell=remove_checkerboard(cell)
                out=trim_and_pad(cell)
                dest=ASSETS/folder/f"r{row}c{col}.png"
                dest.parent.mkdir(parents=True,exist_ok=True)
                out.save(dest,optimize=True)
                if folder=="cases":
                    for alias in CASE_ALIASES.get((row,col),()):
                        named=ASSETS/"cases"/f"{alias}.png"
                        variant=out
                        if alias=="legend":
                            hsv=out.convert("HSV")
                            hp,sp,vp=hsv.split()
                            hp=hp.point(lambda value:(value+17)%256)
                            variant=Image.merge("HSV",(hp,sp,vp)).convert("RGBA")
                        variant.save(named,optimize=True)
                        print(f"{named.relative_to(ROOT)} {variant.size}")
                print(f"{dest.relative_to(ROOT)} {out.size}")
if __name__=="__main__": main()
