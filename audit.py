# -*- coding: utf-8 -*-
"""全库审计：命名合规率、封面缺口、目录图片缺口。只读。"""
import json
import re
import sys
from pathlib import Path

from mutagen import File as MFile
from mutagen.flac import FLAC

R = Path(sys.argv[1] if len(sys.argv) > 1 else "I:/")
EXTS = {".flac", ".dsf", ".dff", ".wav", ".ape", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".wv"}
IMG = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}

total = 0
byext = {}
numbered = []
nocover = []
dirs = set()
dir_imgs = {}

for p in R.rglob("*"):
    try:
        if not p.is_file():
            continue
    except OSError:
        continue
    e = p.suffix.lower()
    if e in IMG:
        dir_imgs.setdefault(p.parent, []).append(p)
        continue
    if e not in EXTS:
        continue
    total += 1
    byext[e] = byext.get(e, 0) + 1
    dirs.add(p.parent)
    if re.match(r"^\s*\d+\s*[-.、,]\s*\S", p.name) or re.match(r"^\s*\d+\s+\S", p.name):
        numbered.append(p)
    if e == ".flac":
        try:
            if not FLAC(p).pictures:
                nocover.append(p)
        except Exception:
            pass

no_img_dirs = [d for d in dirs if not dir_imgs.get(d)]

print("=== 全库审计（{}）===".format(R))
print("音频总数: {}".format(total))
for k, v in sorted(byext.items(), key=lambda x: -x[1]):
    print("  {:<6} {}".format(k, v))
print("文件名仍带序号前缀: {}".format(len(numbered)))
for p in numbered[:12]:
    print("   ", p.relative_to(R))
print("FLAC 无内嵌封面: {}".format(len(nocover)))
for p in nocover[:8]:
    print("   ", p.relative_to(R))
print("专辑目录: {}  其中目录内没有任何图片: {}".format(len(dirs), len(no_img_dirs)))
for d in no_img_dirs[:15]:
    print("   ", d.relative_to(R))

out = Path(r"G:\X20-Rockbox\audit.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "total": total,
    "byext": byext,
    "numbered": [str(p) for p in numbered],
    "nocover": [str(p) for p in nocover],
    "no_img_dirs": [str(d) for d in no_img_dirs],
}, ensure_ascii=False, indent=1), encoding="utf-8")
print("明细已写: {}".format(out))
