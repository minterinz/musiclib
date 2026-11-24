#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fixall — 全库批量修整：规范命名 + 补封面

命名规则（无序号）：
  普通: 音乐名 - 歌手 - 专辑
  古典: 标题 - 作曲家
封面来源（按优先级）：同目录图片 → 上级目录图片 → 上上级目录图片
  prefer: cover/front/folder 命名，其次取像素面积最大的一张

用法:
  python fixall.py rename --root I:\ --apply
  python fixall.py cover  --root I:\ --apply [--limit N]
  python fixall.py report --root I:\
"""
import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

from mutagen import File as MFile
from mutagen.flac import FLAC, Picture

try:
    import PIL.Image as PILImage
except ImportError:
    PILImage = None

AUDIO = {".flac", ".mp3", ".dsf", ".dff", ".wav", ".m4a", ".aiff", ".aif", ".ogg", ".wv"}
IMG = {".jpg", ".jpeg", ".png", ".bmp"}
BAD = r'[\\/:*?"<>|]'
PREFER = ("cover", "front", "folder", "album", "00")


def log_line(fh, step, msg, **kw):
    rec = {"ts": time.strftime("%H:%M:%S"), "step": step, "msg": msg}
    rec.update(kw)
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()


def g(tags, *keys):
    if not tags:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            try:
                s = str(v[0]) if isinstance(v, list) else str(v)
            except Exception:
                s = ""
            s = s.strip()
            if s:
                return s
    return ""


def clean(s, limit=150):
    s = re.sub(BAD, "-", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip(" .")


def is_classical(tags):
    comp = g(tags, "composer", "TCOM")
    genre = g(tags, "genre", "TCON").lower()
    return bool(comp) or "classical" in genre or "古典" in genre


def strip_num(s):
    """去掉标题/歌手/专辑里残留的曲号前缀，如 01. / 03 - / 07、"""
    return re.sub(r"^\s*\d{1,3}\s*[-.、,)]\s*", "", s or "").strip()


def build_name(tags, stem):
    title = strip_num(g(tags, "title", "TIT2"))
    artist = strip_num(g(tags, "artist", "TPE1") or g(tags, "albumartist", "TPE2"))
    album = strip_num(g(tags, "album", "TALB"))
    comp = strip_num(g(tags, "composer", "TCOM"))
    if is_classical(tags):
        base = " - ".join(x for x in (title, comp) if x)
    else:
        base = " - ".join(x for x in (title, artist, album) if x)
    if not base:
        # 无标签：去掉数字前缀，保留原名
        base = re.sub(r"^\s*\d+\s*[-.、,]\s*", "", stem) or stem
    return base


def read_tags(p):
    try:
        f = MFile(p)
    except Exception:
        return None
    return getattr(f, "tags", None)


def collect(root):
    out = []
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in AUDIO:
                out.append(p)
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ rename
def do_rename(root, apply, log):
    files = collect(root)
    seen = {}
    changed = same = failed = 0
    plan = []
    for p in files:
        tags = read_tags(p)
        base = build_name(tags, p.stem)
        new = p.with_name(clean(base) + p.suffix.lower())
        if new == p:
            same += 1
            continue
        key = str(new).lower()
        if key in seen:
            seen[key] += 1
            new = new.with_name(new.stem + " ({})".format(seen[key]) + p.suffix.lower())
        else:
            seen[key] = 1
        plan.append((p, new))
        if len(plan) <= 15:
            log_line(log, "rename", "{} -> {}".format(p.name[:70], new.name[:70]))
        if apply:
            try:
                if new.exists() and new != p:
                    log_line(log, "rename", "跳过(目标存在) " + p.name[:60])
                    failed += 1
                    continue
                p.rename(new)
                changed += 1
            except Exception as exc:
                failed += 1
                log_line(log, "rename", "失败 {}".format(exc), file=str(p))
    log_line(log, "rename", "完成", 计划=len(plan), 已改名=changed,
             原样=same, 失败=failed, 总文件=len(files))
    return changed


# ------------------------------------------------------------------ cover
def pick_image(d):
    """在本目录挑一张最合适的图；没有则返回 None。"""
    imgs = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMG]
    if not imgs:
        return None

    def score(p):
        n = p.name.lower()
        pref = 0 if any(n.startswith(x) or n == x + p.suffix.lower() for x in PREFER) else 1
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        return (pref, -sz)

    imgs.sort(key=score)
    return imgs[0]


def find_image_for(p, root):
    d = p.parent
    for _ in range(3):
        got = pick_image(d)
        if got:
            return got
        if d == root or d.parent == d:
            break
        d = d.parent
    return None


def make_pic(img_path, max_px=700, quality=86):
    im = PILImage.open(img_path).convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), im.size


def embed_flac(p, data):
    f = FLAC(p)
    if f.pictures:
        return "has"
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = data
    f.add_picture(pic)
    f.save()
    return "ok"


def embed_mp3(p, data):
    from mutagen.id3 import APIC, ID3
    try:
        tags = ID3(p)
    except Exception:
        tags = ID3()
    if tags.getall("APIC"):
        return "has"
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
    tags.save(p)
    return "ok"


def do_cover(root, apply, log, limit=None):
    files = collect(root)
    todo = []
    for p in files:
        if p.suffix.lower() != ".flac":
            continue
        try:
            if not FLAC(p).pictures:
                todo.append(p)
        except Exception:
            pass
    if limit:
        todo = todo[:limit]
    log_line(log, "cover", "待补封面", 数量=len(todo))

    cache = {}
    ok = has = noimg = failed = 0
    for i, p in enumerate(todo, 1):
        try:
            img = find_image_for(p, root)
        except OSError as exc:
            failed += 1
            log_line(log, "cover", "目录不可读(设备掉线?) {}".format(exc), file=str(p))
            continue
        if img is None:
            noimg += 1
            log_line(log, "cover", "无可用图片 " + str(p.relative_to(root))[:90])
            continue
        key = str(img)
        if key not in cache:
            try:
                cache[key] = make_pic(img)[0]
            except Exception as exc:
                cache[key] = None
                log_line(log, "cover", "图片读取失败 {}".format(exc), img=key)
        data = cache[key]
        if data is None:
            failed += 1
            continue
        if not apply:
            ok += 1
            continue
        try:
            r = embed_flac(p, data)
            if r == "ok":
                ok += 1
            else:
                has += 1
        except Exception as exc:
            failed += 1
            log_line(log, "cover", "写入失败 {}".format(exc), file=str(p))
        if i % 50 == 0:
            log_line(log, "cover", "进度", 已完成=i, 总数=len(todo), 写入=ok, 无图=noimg, 失败=failed)
    log_line(log, "cover", "完成", 写入=ok, 已有=has, 无图=noimg, 失败=failed)
    return ok


# ------------------------------------------------------------------ report
def do_report(root, log):
    files = collect(root)
    by_ext = {}
    numbered = 0
    nocover = 0
    for p in files:
        by_ext[p.suffix.lower()] = by_ext.get(p.suffix.lower(), 0) + 1
        if re.match(r"^\s*\d+\s*[-.、,]\s*\S", p.name):
            numbered += 1
        if p.suffix.lower() == ".flac":
            try:
                if not FLAC(p).pictures:
                    nocover += 1
            except Exception:
                pass
    log_line(log, "report", "总览", 音频=len(files), 带序号=numbered,
             无封面=nocover, 按类型=by_ext)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["rename", "cover", "report"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--log", default=r"G:\X20-Rockbox\fixall.jsonl")
    a = ap.parse_args()

    if PILImage is None and a.cmd == "cover":
        print("需要 Pillow")
        return 2
    root = Path(a.root)
    if not root.is_dir():
        print("根目录不存在:", root)
        return 2

    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    with open(a.log, "a", encoding="utf-8") as fh:
        t0 = time.time()
        log_line(fh, "main", "开始 {} root={} apply={}".format(a.cmd, root, a.apply))
        if a.cmd == "rename":
            do_rename(root, a.apply, fh)
        elif a.cmd == "cover":
            do_cover(root, a.apply, fh, a.limit)
        else:
            do_report(root, fh)
        log_line(fh, "main", "结束 用时 {:.1f}s".format(time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
