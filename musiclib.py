#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musiclib — 音乐库整理 / 修改工具（单文件 CLI）

子命令
  scan      扫描统计：格式、采样率、位深、声道、封面、大小，输出 Markdown/CSV
  rename    规范命名：普通=音乐名-歌手-专辑，古典=标题-作曲家（不加序号）
  flatten   目录扁平化：把散在子目录里的音频提到专辑目录第一层
  cover     封面内嵌：把 cover.jpg 写进 FLAC
  downmix   5.1 → 立体声降混（ffmpeg，ITU-R BS.775 权重 + -3dB）
  cleanup   旧文件清理：新集覆盖校验通过才删旧的，逐条写日志
  verify    库体检：0Hz/0bit、无封面、重名、空目录、ISO/DSD 残留

安全默认
  除 scan / verify 外，所有会改动文件的子命令**默认 dry-run**，
  必须显式加 --apply 才真正执行；cleanup 另有 --force 才允许删除。
"""

import argparse
import csv
import json
import os


def run_hidden(args, **kw):
    """跑外部程序但不弹控制台窗口。

    --windowed 打包后 subprocess 默认会拉起一个黑色 cmd 窗口，
    每次转码/分割都在屏幕上闪一下（2026-11-30 用户报「前台弹 cmd 窗口」）。
    Windows 上必须显式给 CREATE_NO_WINDOW。
    """
    import subprocess as _sp
    if os.name == "nt":
        kw["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)
    return _sp.run(args, **kw)

import re
import shutil
import subprocess
import time as _time
import sys
import time
from pathlib import Path

try:
    from mutagen.flac import FLAC, Picture
except ImportError:
    sys.stderr.write("缺少依赖 mutagen：pip install mutagen\n")
    raise SystemExit(2)

AUDIO_EXT = {".flac", ".dsf", ".dff", ".wav", ".ape", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".wv"}
DOWNMIX_DIRNAME = "5.1降混"
BAD_CHARS = r'[\\/:*?"<>|]'


# ---------------------------------------------------------------- 基础设施
# 只读保护盘：用户明确要求 Z:\download\music 是保种源，任何写操作都不许碰。
# 用环境变量可扩展：MUSICLIB_READONLY_ROOTS="z:\;d:\xxx"（分号分隔）
READONLY_ROOTS = [x.strip().lower().rstrip("\\")
                  for x in os.environ.get("MUSICLIB_READONLY_ROOTS", "z:").split(";")
                  if x.strip()]


def assert_writable(root, apply: bool, log=None):
    """执行写操作前校验目标盘不是保种源。apply=False（dry-run）不拦。"""
    if not apply:
        return
    try:
        rp = str(Path(root).resolve()).lower().rstrip("\\")
    except Exception:
        return
    for g in READONLY_ROOTS:
        if rp == g or rp.startswith(g + "\\"):
            msg = ("拒绝写入：{} 在只读保护列表里（{}）。"
                   "保种源不得修改 —— 请先复制到 I: 盘，只改副本。").format(root, g)
            if log:
                log("guard", msg)
            raise SystemExit(2)


class Log:
    def __init__(self, path: Path = None, quiet=False):
        self.quiet = quiet
        self.fh = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, step, msg, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step, "msg": msg}
        rec.update(fields)
        if self.fh:
            self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.fh.flush()
        if not self.quiet:
            extra = " ".join("{}={}".format(k, v) for k, v in fields.items())
            print("[{:<8}] {}{}".format(step, msg, ("  " + extra) if extra else ""), flush=True)

    def close(self):
        if self.fh:
            self.fh.close()


def human(n):
    f = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return "{:.1f}{}".format(f, u)
        f /= 1024.0


def safe_name(s, limit=150):
    s = re.sub(BAD_CHARS, "-", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip(" .")


def tag(f, key):
    v = f.get(key)
    if not v:
        return ""
    return str(v[0]).strip()


def iter_flac(root: Path, skip_downmix=True):
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() != ".flac":
            continue
        if skip_downmix and DOWNMIX_DIRNAME in p.parts:
            continue
        yield p


def open_flac(p):
    try:
        return FLAC(p)
    except Exception:
        return None


# ---------------------------------------------------------------- 封面查找
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
# 明显不是封面的图（封底、碟面、内页、书脊…）：仅在"按名字找不到"时用来排除
NON_COVER_WORDS = ("back", "rear", "disc", "cd", "dvd", "booklet", "inlay", "tray",
                   "inside", "inner", "obi", "spine", "matrix", "label", "封底",
                   "碟面", "内页", "书脊", "背面")

# 常见封面文件名（越靠前越优先）
COVER_NAMES = ("cover", "folder", "front", "albumart", "albumartlarge", "albumartsmall",
               "album", "jacket", "artwork", "case", "thumb", "封面", "封底")
# 常见存放图片的子目录名
COVER_SUBDIRS = ("artwork", "artworks", "scans", "scan", "covers", "cover", "art",
                 "booklet", "images", "pictures", "jpg", "图片", "封面", "扫图", "bk")


def _img_rank(p: Path):
    stem = p.stem.strip().lower()
    for i, key in enumerate(COVER_NAMES):
        if stem == key:
            return (0, i, p.name)
    for i, key in enumerate(COVER_NAMES):
        if stem.startswith(key):
            return (1, i, p.name)
    return (2, 99, p.name)


def _list_images(d: Path):
    """列目录下的图片；目录读不动时抛 OSError（便于区分「没有图」和「读不到」）。"""
    out = []
    for e in d.iterdir():
        try:
            if e.is_file() and e.suffix.lower() in IMG_EXT:
                out.append(e)
        except OSError:
            continue
    return out


def _best_image(d: Path):
    imgs = _list_images(d)
    if not imgs:
        return None
    imgs.sort(key=_img_rank)
    best = imgs[0]
    if _img_rank(best)[0] == 2 and len(imgs) > 1:
        try:  # 全是不认识的名字，挑体积最大的（主封面通常比缩略图大）
            imgs.sort(key=lambda x: x.stat().st_size, reverse=True)
            best = imgs[0]
        except OSError:
            pass
    return best


def find_cover_image(p: Path, root: Path = None, max_up: int = 3):
    """按优先级找可用封面图。

    顺序：本目录常见封面名 → 本目录唯一一张图 → 向下 Artwork/Scans 等子目录
          → 向上最多 max_up 层父目录（不越过 root）

    返回 (图片路径 或 None, 来源说明 或 失败原因, 是否属于「目录读不动」)
    """
    root_p = Path(root) if root else None

    def within(d: Path):
        if root_p is None:
            return True
        try:
            d.resolve().relative_to(root_p.resolve())
            return True
        except Exception:
            return False

    # 1) 本目录
    try:
        same = _list_images(p.parent)
    except OSError as exc:
        return (None, "目录不可读（{}）".format(exc.__class__.__name__), True)
    named = [e for e in same if _img_rank(e)[0] <= 1]
    if named:
        named.sort(key=_img_rank)
        return (named[0], "本目录", False)
    if len(same) == 1:
        return (same[0], "本目录(唯一图)", False)
    # 兜底：名字不规范（IMG_0001.bmp / 扫描1.JPG / img001.jpg 这类）
    # 排除明显不是封面的（封底/碟面/内页），剩下的按体积取最大
    if same:
        cand = [
            e for e in same
            if not any(w in e.stem.lower() for w in NON_COVER_WORDS)
        ]
        if not cand:
            cand = same
        try:
            cand.sort(key=lambda e: e.stat().st_size, reverse=True)
        except OSError:
            pass
        return (cand[0], "本目录(按大小取最大的图)", False)

    # 2) 向下找 Artwork / Scans / Covers 等子目录
    try:
        subs = [e for e in p.parent.iterdir() if e.is_dir()]
    except OSError as exc:
        return (None, "目录不可读（{}）".format(exc.__class__.__name__), True)
    for sub in subs:
        if sub.name.lower() not in COVER_SUBDIRS:
            continue
        try:
            hit = _best_image(sub)
        except OSError:
            continue
        if hit:
            return (hit, "子目录 {}".format(sub.name), False)

    # 3) 向上找父目录（库根不算专辑，不能拿根上的图充当封面）
    d = p.parent
    for _ in range(max_up):
        parent = d.parent
        if not within(parent):
            break
        if root_p is not None:
            try:
                if parent.resolve() == root_p.resolve():
                    break
            except Exception:
                pass
        d = parent
        try:
            hit = _best_image(d)
        except OSError:
            continue
        if hit:
            return (hit, "上级 {}".format(d.name or str(d)), False)
    return (None, "目录链上没有可用图片", False)


def wav_to_flac(p: Path, ffmpeg="ffmpeg", verify=True) -> Path:
    """WAV → FLAC，逐字节无损。返回新文件路径；失败抛异常。

    为什么值得转：FLAC 是完全无损的压缩格式，解码后与 WAV 的 PCM 逐字节相同
    （用 -f s32le 原始样本算 MD5 可验证），体积通常小 35-45%；
    而且 Windows 资源管理器能给 FLAC 画封面缩略图，WAV 不行。

    保留：采样率 / 位深 / 声道数一律不动，标签和封面跟着过去。
    安全：先写 .w2f.tmp.flac，校验通过才改名；原 WAV 不动（删不删由调用方决定）。
    """
    import hashlib
    import subprocess

    p = Path(p)
    if p.suffix.lower() != ".wav":
        raise RuntimeError("不是 WAV：{}".format(p.name))
    dst = p.with_suffix(".flac")
    if dst.exists():
        raise RuntimeError("目标已存在，跳过：{}".format(dst.name))

    tmp = p.with_name(p.stem + ".w2f.tmp.flac")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    r = run_hidden(
        [ffmpeg, "-y", "-v", "error", "-i", str(p),
         "-map", "0:a:0", "-c:a", "flac", "-compression_level", "8",
         "-map_metadata", "0", str(tmp)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError("ffmpeg 转换失败：{}".format((r.stderr or "")[:300]))

    if verify:
        def raw_md5(path):
            q = run_hidden(
                [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0",
                 "-f", "s32le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            return hashlib.md5(q.stdout).hexdigest()

        try:
            m1 = raw_md5(p)
            m2 = raw_md5(tmp)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("无损校验执行失败：{}".format(exc))
        if m1 != m2:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                "无损校验不一致（WAV {} vs FLAC {}），已放弃".format(m1[:12], m2[:12]))

    # 把封面重写成 JPEG 并补 width/height/depth（Windows 靠这几个字段画缩略图）
    try:
        raw_cover = get_cover_bytes(p)
        if raw_cover:
            img = p.with_name(p.stem + ".w2f.cover.jpg")
            img.write_bytes(raw_cover)
            embed_cover(tmp, img, max_px=700)
    except Exception:
        pass  # allow-swallow: 封面补写失败不影响音频，转出来的 FLAC 仍然可用

    os.replace(tmp, dst)
    return dst


def _cue_read_text(cue: Path) -> str:
    """cue 编码很杂（GBK/Big5/UTF-8），按序试，返回第一个能解的。"""
    raw = Path(cue).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _cue_time_to_sec(t: str) -> float:
    """cue 的 mm:ss:ff 转秒（ff 是 1/75 秒的帧）。"""
    parts = t.strip().split(":")
    if len(parts) == 3:
        m, s, f = parts
        return int(m) * 60 + int(s) + int(f) / 75.0
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + int(s)
    return float(t)


def parse_cue(cue: Path):
    """解析 cue，返回 (音频文件名, [ {index, title, performer, start, end} ])。

    end 由下一轨的 start 推出，最后一轨为 None（用总时长）。
    """
    text = _cue_read_text(cue)
    files = re.findall(r'^\s*FILE\s+"([^"]+)"', text, re.M) or \
            re.findall(r'^\s*FILE\s+(\S+)', text, re.M)
    if not files:
        raise RuntimeError("cue 里没有 FILE 行")

    disc_title = ""
    m = re.search(r'^\s*TITLE\s+"([^"]*)"', text, re.M)
    if m:
        disc_title = m.group(1).strip()
    disc_perf = ""
    m = re.search(r'^\s*PERFORMER\s+"([^"]*)"', text, re.M)
    if m:
        disc_perf = m.group(1).strip()

    tracks = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        mt = re.match(r'TRACK\s+(\d+)\s+AUDIO', s, re.I)
        if mt:
            if cur:
                tracks.append(cur)
            cur = {"index": int(mt.group(1)), "title": "", "performer": "",
                   "start": None, "end": None}
            continue
        if cur is None:
            continue
        mti = re.match(r'TITLE\s+"([^"]*)"', s, re.I)
        if mti:
            cur["title"] = mti.group(1).strip(); continue
        mip = re.match(r'PERFORMER\s+"([^"]*)"', s, re.I)
        if mip:
            cur["performer"] = mip.group(1).strip(); continue
        mi = re.match(r'INDEX\s+01\s+(\d+:\d+:\d+)', s, re.I)
        if mi:
            cur["start"] = _cue_time_to_sec(mi.group(1))
    if cur:
        tracks.append(cur)

    tracks = [t for t in tracks if t["start"] is not None]
    for i, t in enumerate(tracks):
        t["end"] = tracks[i + 1]["start"] if i + 1 < len(tracks) else None
    return files[0], tracks, disc_title, disc_perf


def find_cue_audio(cue: Path, cue_file_name: str):
    """cue 里写的音频可能已经转成别的格式，按扩展名轮着找。"""
    want = Path(cue_file_name)
    cand = cue.parent / want.name
    if cand.exists():
        return cand
    for ext in (".flac", ".wav", ".ape", ".wv", ".tta", ".aiff", ".aif", ".dsf", ".dff", ".mp3"):
        c2 = cand.with_suffix(ext)
        if c2.exists():
            return c2
    return None


def split_cue_album(cue: Path, ffmpeg="ffmpeg", apply=True, out_dir: Path = None,
                    on_track=None):
    """把一个「单文件 cue + 整轨音频」按 cue 分轨，一个字节都不重新编码。

    返回 (音频路径, [要生成的分轨路径...])；apply=False 时只算计划。

    on_track(i, total, out_path, track_dict)：每切完一轨回调一次，
    让调用方能把"第几轨/共几轨/曲名"写进日志——否则切 16 轨静默 15 秒，
    用户根本不知道在干什么（2026-11-30 用户原话：「进度你得写进日志里啊」）。
    """
    import subprocess

    cue = Path(cue)
    files, tracks, disc_title, disc_perf = parse_cue(cue)
    if not tracks:
        raise RuntimeError("cue 里没有可用 TRACK")
    audio = find_cue_audio(cue, files)
    if audio is None:
        raise RuntimeError("cue 指向的音频不存在：{}".format(files))
    if len(set([files])) != 1:
        raise RuntimeError("多文件 cue（分轨索引），不需要分割")

    dest = Path(out_dir) if out_dir else cue.parent
    dest.mkdir(parents=True, exist_ok=True)

    width = 2 if len(tracks) >= 10 else 1
    planned = []
    for t in tracks:
        name = t["title"] or "track{:02d}".format(t["index"])
        safe = safe_name(name)
        fn = "{:0{w}d} {}.flac".format(t["index"], safe, w=width)
        planned.append(dest / fn)

    if not apply:
        return audio, planned

    total_len = None
    for idx, (t, out) in enumerate(zip(tracks, planned), 1):
        if out.exists():
            if on_track:
                try:
                    on_track(idx, len(tracks), out, t, "已存在")
                except Exception:
                    pass  # allow-swallow: 回调里的日志出错不能中断切分
            continue
        t_track = _time.time()

        # -ss 必须放在 -i **之后**（输出侧精确 seek）：
        # 放前面是快速 seek，-c copy 只能落在 FLAC 帧边界上——
        # 实测第一轨开头就偏了 0.17 秒（会把上一首的尾音带进来）。
        # 重编码成 FLAC 是无损的：解码出的 PCM 与原区间逐字节相同，
        # 只是不再复用具原文件的压缩块。这里要的是切得准，不是省时间。
        args = [ffmpeg, "-y", "-v", "error", "-i", str(audio),
                "-ss", "{:.6f}".format(t["start"]),
                "-map", "0:a:0"]
        if t["end"] is not None:
            args += ["-t", "{:.6f}".format(t["end"] - t["start"])]
        args += ["-c:a", "flac", "-compression_level", "8", str(out)]
        r = run_hidden(args, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("分割第 {} 轨失败：{}".format(t["index"], (r.stderr or "")[:200]))
        # 写标签（标题/艺术家/专辑/曲目号）
        try:
            write_tag(out, "title", t["title"] or out.stem)
            write_tag(out, "artist", t["performer"] or disc_perf or "")
            write_tag(out, "album", disc_title or cue.stem)
            write_tag(out, "tracknumber", "{:02d}".format(t["index"]))
            if len(tracks) > 1:
                write_tag(out, "totaltracks", str(len(tracks)))
        except Exception:
            pass  # allow-swallow: 标签写失败不影响音频本身

        if on_track:
            try:
                on_track(idx, len(tracks), out, t,
                         "{:.1f}s {:.0f}MB".format(_time.time() - t_track,
                                                  out.stat().st_size / 1024 / 1024))
            except Exception:
                pass  # allow-swallow: 回调里的日志出错不能中断切分
    return audio, planned


def has_cover(p: Path, ext=None):
    """判断音频是否已有封面；读不动抛异常。不支持的格式返回 None。

    注意 ext：对「副本」做回读校验时必须传**原文件**扩展名，
    否则 .tagtmp/.mbtmp 这类后缀会被判成不支持的格式（反复踩过）。
    """
    ext = (ext or p.suffix).lower()
    if ext == ".flac":
        return bool(FLAC(p).pictures)
    # wav / dsf / dff / mp3 / aif / aiff 都在 ID3_FORMATS 里，统一走 APIC 判断。
    # 这里判错会和 get_cover_bytes 打架 —— 列表里出现「缩略图有图但封面列打叉」
    # 就是这个原因（2026-11-30 排查过），两者必须用同一套格式判断。
    if ext in ID3_FORMATS:
        opener, _need = _id3_opener(ext)
        try:
            obj = opener(str(p))
        except Exception:
            return False
        tags = getattr(obj, "tags", None)
        if tags is None:
            return False
        return any(k.startswith("APIC") for k in tags.keys())
    return None


def get_cover_bytes(p: Path, ext=None):
    """取内嵌封面的原始字节（跨格式）；没有或读不到返回 None。

    ext 同上：校验临时副本时传原文件扩展名。
    """
    ext = (ext or p.suffix).lower()
    try:
        if ext == ".flac":
            f = FLAC(p)
            return f.pictures[0].data if f.pictures else None
        if ext in ID3_FORMATS:
            opener, _need = _id3_opener(ext)
            obj = opener(str(p))
            t = getattr(obj, "tags", None)
            if t is None:
                return None
            for k in t.keys():
                if k.startswith("APIC"):
                    return t[k].data
            return None
        if ext == ".wav":
            from mutagen.wave import WAVE
            w = WAVE(str(p))
            if w.tags:
                for k in w.tags.keys():
                    if k.startswith("APIC"):
                        return w.tags[k].data
            return None
        if ext in (".dsf", ".dff"):
            from mutagen.dsf import DSF
            d = DSF(str(p))
            if d.tags:
                for k in d.tags.keys():
                    if k.startswith("APIC"):
                        return d.tags[k].data
            return None
    except Exception:
        return None
    return None


def embed_cover(p: Path, img_path: Path, max_px=700, quality=86):
    """给 FLAC / MP3 写封面（覆盖原有封面）。

    安全写法：先在**临时副本**上写，校验通过后才原子替换原文件。
    原文件在这之前一个字节都不动，写坏了直接丢弃副本。
    原因：mutagen 的 FLAC.save() 是在原文件里原地搬移音频数据来腾出
    metadata 空间，U 盘（exFAT）上一旦中断就把音频截断——2026-11-24 已实测
    把张雨生《大海》里的一个文件写成了 6 秒。
    """
    import io
    from PIL import Image
    ext = p.suffix.lower()
    if ext != ".flac" and ext not in ID3_FORMATS:
        raise RuntimeError("格式不支持: {}".format(ext))

    before = p.stat().st_size
    tmp = p.with_name(p.name + ".mbtmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        shutil.copy2(str(p), str(tmp))

        if ext == ".flac":
            from mutagen.flac import Picture
            f = open_flac(tmp)
            if f is None:
                raise RuntimeError("FLAC 打不开")
            im = Image.open(img_path).convert("RGB")
            if max(im.size) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            while f.pictures:
                f.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = buf.getvalue()
            # width/height/depth 必须填：留空会写成 0x0，
            # Windows 资源管理器靠这几个字段画缩略图，0x0 就不显示了（实测）。
            pic.width = im.width
            pic.height = im.height
            pic.depth = 24          # RGB = 每像素 24 位
            f.add_picture(pic)
            f.save()
        else:
            # MP3 / WAV / DSF / DFF / AIFF：统一走 ID3v2 的 APIC
            from mutagen.id3 import ID3, APIC
            opener, need_add = _id3_opener(ext)
            obj = opener(str(tmp))
            if getattr(obj, "tags", None) is None:
                if need_add:
                    obj.add_tags()
                else:
                    obj.tags = ID3()
            im = Image.open(img_path).convert("RGB")
            if max(im.size) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            obj.tags.delall("APIC")
            obj.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                              data=buf.getvalue()))
            obj.save()

        # 校验副本：文件不能变小、必须能正常解析、封面必须到位
        after = tmp.stat().st_size
        if after < before:
            raise RuntimeError("副本比原文件小（{} < {}），已放弃替换".format(after, before))
        if ext == ".flac":
            chk = FLAC(str(tmp))
            if not chk.pictures:
                raise RuntimeError("副本读不到封面，已放弃替换")
            if chk.info.length <= 0:
                raise RuntimeError("副本音频参数异常，已放弃替换")
            if abs(chk.info.length - FLAC(str(p)).info.length) > 0.05:
                raise RuntimeError("副本时长与原文件不一致，已放弃替换")
        else:
            if not has_cover(tmp, ext=ext):
                raise RuntimeError("副本读不到 APIC，已放弃替换")

        os.replace(str(tmp), str(p))   # 同盘原子替换
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 命名模板
# 用户可自定义文件名模板，变量用 <名字> 或 {名字}，两种写法都认。
# 默认值保持原来的规则：普通「标题 - 演出者 - 专辑」，古典「标题 - 作曲家」。
DEFAULT_TEMPLATE_POP = "{标题} - {演出者} - {专辑}"
DEFAULT_TEMPLATE_CLASSICAL = "{标题} - {作曲家}"

TEMPLATE_VARS = {
    "标题": ("title",), "title": ("title",),
    "演出者": ("artist", "albumartist"), "艺术家": ("artist", "albumartist"),
    "歌手": ("artist", "albumartist"), "artist": ("artist", "albumartist"),
    "专辑": ("album",), "album": ("album",),
    "作曲家": ("composer",), "composer": ("composer",),
    "音轨号": ("tracknumber",), "track": ("tracknumber",), "tracknumber": ("tracknumber",),
    "碟号": ("discnumber",), "disc": ("discnumber",),
    "年份": ("date", "year"), "year": ("date", "year"),
    "流派": ("genre",), "genre": ("genre",),
}
# 特殊变量（不从标签取）
TEMPLATE_SPECIAL = ("原名", "stem", "filename", "格式", "ext")

NAME_TOKEN = re.compile(r"([<{]([^<>{}]+)[>}])")
SEP_RUN = re.compile(r"(\s*[-–—_]\s*){2,}")


def template_vars_help():
    """给界面用：可用的变量清单（中文名优先）。"""
    names = ["标题", "演出者", "专辑", "作曲家", "音轨号", "碟号", "年份", "流派", "原名", "格式"]
    return "、".join("<{}>".format(n) for n in names)


def render_name_template(tag_getter, template, stem="", ext=""):
    """按模板生成文件名主体。tag_getter(key) 返回标签字符串（没有返回空）。

    空变量会连同它旁边的分隔符一起收掉，不会留下「 - - 」这种残渣。
    """
    template = (template or "").strip()
    if not template:
        return ""

    def resolve(name):
        key = name.strip()
        if key in ("原名", "stem", "filename"):
            return stem or ""
        if key in ("格式", "ext"):
            return (ext or "").lstrip(".")
        keys = TEMPLATE_VARS.get(key)
        if not keys:
            return ""
        for k in keys:
            v = tag_getter(k)
            if v:
                return str(v).strip()
        return ""

    pieces = []
    pos = 0
    for m in NAME_TOKEN.finditer(template):
        if m.start() > pos:
            pieces.append(("lit", template[pos:m.start()]))
        pieces.append(("var", resolve(m.group(2))))
        pos = m.end()
    if pos < len(template):
        pieces.append(("lit", template[pos:]))

    out = []
    for kind, s in pieces:
        if kind == "var":
            out.append(s or "")
        else:
            # 字面段里的分隔符：如果前后都是空变量，这里可能留下冗余
            out.append(s)
    text = "".join(out)

    # 收拾空变量留下的残渣：连续分隔符合成一个，首尾分隔符去掉
    text = SEP_RUN.sub(" - ", text)
    text = re.sub(r"^\s*[-–—_]\s*", "", text)
    text = re.sub(r"\s*[-–—_]\s*$", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .")
    return text


def build_name_by_template(f, template, stem="", ext=""):
    """对某个 mutagen 对象套用模板；返回清理后的文件名主体。"""

    def get(key):
        v = f.get(key) if hasattr(f, "get") else None
        if not v:
            return ""
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        s = str(v).strip()
        if key == "tracknumber":
            # 曲目号补零到两位，便于排序
            m = re.match(r"(\d+)", s)
            if m:
                return m.group(1).zfill(2)
        if key == "date":
            return s[:4]
        return s

    return render_name_template(get, template, stem=stem, ext=ext)

TAG_FIELDS = {
    "title":  ("title",  "TIT2"),
    "artist": ("artist", "TPE1"),
    "album":  ("album",  "TALB"),
    "albumartist": ("albumartist", "TPE2"),
    "date":   ("date",   "TDRC"),
    "genre":  ("genre",  "TCON"),
    "tracknumber": ("tracknumber", "TRCK"),
}

# 能写标签的格式：flac 走 Vorbis comment，其余走 ID3v2
ID3_FORMATS = {".mp3", ".wav", ".dsf", ".dff", ".aif", ".aiff"}
SUPPORTED_TAG_FORMATS = {".flac"} | ID3_FORMATS


def editable_fields(p):
    """这个文件能改哪些标签字段；空列表表示该格式不支持。"""
    if p.suffix.lower() in SUPPORTED_TAG_FORMATS:
        return list(TAG_FIELDS)
    return []


class _Mp3Id3(object):
    """把裸 ID3 对象包装成和 WAVE/DSF 一样的「有 .tags + .save()」接口。"""

    def __init__(self, path):
        from mutagen.id3 import ID3
        try:
            self.tags = ID3(path)
        except Exception:
            self.tags = ID3()

    def add_tags(self):
        pass

    def save(self):
        self.tags.save()


def _id3_opener(ext):
    """按扩展名返回 (打开函数, 是否需要 add_tags)。注意 ext 要传原文件扩展名，
    不能用临时副本的 .tagtmp，否则格式判断会失效。"""
    ext = (ext or "").lower()
    if ext == ".mp3":
        return _Mp3Id3, False
    if ext == ".wav":
        from mutagen.wave import WAVE
        return (lambda path: WAVE(path)), True
    if ext in (".dsf", ".dff"):
        from mutagen.dsf import DSF
        return (lambda path: DSF(path)), True
    if ext in (".aif", ".aiff"):
        from mutagen.aiff import AIFF
        return (lambda path: AIFF(path)), True
    raise RuntimeError("不支持的格式: {}".format(ext))


def _apply_id3(obj, frame_key, value, need_add):
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TCON, TRCK
    cls = {"TIT2": TIT2, "TPE1": TPE1, "TALB": TALB,
           "TPE2": TPE2, "TDRC": TDRC, "TCON": TCON, "TRCK": TRCK}[frame_key]
    if obj.tags is None:
        if not need_add:
            from mutagen.id3 import ID3
            obj.tags = ID3()
        else:
            obj.add_tags()
    obj.tags.delall(frame_key)
    if value:
        obj.tags.add(cls(encoding=3, text=[value]))
    obj.save()


def _read_id3_value(p, frame_key, ext):
    try:
        opener, _need = _id3_opener(ext)
        obj = opener(str(p))
        if obj.tags is None:
            return ""
        fr = obj.tags.get(frame_key)
        if fr is None:
            return ""
        txt = getattr(fr, "text", None)
        return str(txt[0]) if txt else ""
    except Exception:
        return ""


def read_tag(p: Path, field: str, ext: str = None):
    """统一读取一个标签字段（跨格式）。ext 缺省按 p 的扩展名，读临时副本时要显式传。"""
    if field not in TAG_FIELDS:
        return ""
    flac_key, id3_key = TAG_FIELDS[field]
    ext = (ext or p.suffix).lower()
    try:
        if ext == ".flac":
            f = FLAC(str(p))
            v = (f.get(flac_key) or [""])[0]
            return str(v)
        if ext in ID3_FORMATS:
            return _read_id3_value(p, id3_key, ext)
    except Exception:
        pass
    return ""


def write_tag(p: Path, field: str, value: str):
    """安全地写一个标签字段（跨格式）：副本上写 → 回读校验 → 原子替换。

    FLAC 用 Vorbis comment；MP3 / WAV / DSF / DFF / AIFF 用 ID3v2 帧
    （WAV 与 DSF 原本没有标签容器时会先 add_tags 建一个）。
    原文件在成功前不会被改动；格式不支持时抛 RuntimeError。
    """
    if field not in TAG_FIELDS:
        raise ValueError("不支持的字段: {}（可用：{}）".format(field, "/".join(TAG_FIELDS)))
    flac_key, id3_key = TAG_FIELDS[field]
    ext = p.suffix.lower()
    if ext not in SUPPORTED_TAG_FORMATS:
        raise RuntimeError("该格式（{}）暂不支持编辑标签".format(ext or "无扩展名"))

    before = p.stat().st_size
    tmp = p.with_name(p.name + ".tagtmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    value = (value or "").strip()
    try:
        shutil.copy2(str(p), str(tmp))

        if ext == ".flac":
            f = FLAC(str(tmp))
            if value:
                f[flac_key] = value
            elif flac_key in (f.tags or {}):
                del f[flac_key]
            f.save()
        else:
            opener, need_add = _id3_opener(ext)
            obj = opener(str(tmp))
            _apply_id3(obj, id3_key, value, need_add)

        got = read_tag(tmp, field, ext=ext)
        if value and got != value:
            raise RuntimeError("回读校验失败（写入 {!r} 读出 {!r}），已放弃替换".format(value, got))
        if tmp.stat().st_size < before * 0.5:
            raise RuntimeError("副本异常变小（{} -> {}），已放弃替换".format(before, tmp.stat().st_size))
        os.replace(str(tmp), str(p))
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def rename_file(p: Path, new_stem: str, dry=False):
    """只改文件名（保留扩展名与所在目录）。返回新路径；dry 时只返回目标路径。"""
    new_stem = safe_name(new_stem)
    if not new_stem:
        raise ValueError("新文件名不能为空")
    dst = p.with_name(new_stem + p.suffix)
    if dst == p:
        return p
    if dst.exists():
        raise RuntimeError("同目录已存在同名文件：{}".format(dst.name))
    if not dry:
        p.rename(dst)
    return dst


# ---------------------------------------------------------------- scan
def cmd_scan(args, log):
    root = Path(args.root)
    rows = []
    agg = {"files": 0, "bytes": 0, "no_cover": 0, "by_rate": {}, "by_channels": {},
           "by_ext": {}, "bad": 0}

    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        rec = {"path": str(p.relative_to(root)), "size": p.stat().st_size,
               "ext": p.suffix.lower(), "rate": "", "bits": "", "channels": "",
               "cover": "", "artist": "", "album": "", "title": ""}
        if p.suffix.lower() == ".flac":
            f = open_flac(p)
            if f is None:
                rec["bad"] = 1
                agg["bad"] += 1
            else:
                rec["rate"] = f.info.sample_rate
                rec["bits"] = f.info.bits_per_sample
                rec["channels"] = f.info.channels
                rec["cover"] = len(f.pictures)
                rec["artist"] = tag(f, "artist") or tag(f, "albumartist")
                rec["album"] = tag(f, "album")
                rec["title"] = tag(f, "title")
                if not f.pictures:
                    agg["no_cover"] += 1
                k = "{}Hz/{}bit".format(f.info.sample_rate, f.info.bits_per_sample)
                agg["by_rate"][k] = agg["by_rate"].get(k, 0) + 1
                agg["by_channels"][f.info.channels] = agg["by_channels"].get(f.info.channels, 0) + 1
        agg["files"] += 1
        agg["bytes"] += rec["size"]
        agg["by_ext"][rec["ext"]] = agg["by_ext"].get(rec["ext"], 0) + 1
        rows.append(rec)

    out_dir = Path(args.out) if args.out else root / "_musiclib报告"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "scan.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["path"])
        w.writeheader()
        w.writerows(rows)

    md = ["# 音乐库扫描报告", "", "生成时间：{}".format(time.strftime("%Y-%m-%d %H:%M:%S")),
          "根目录：`{}`".format(root), "",
          "| 项 | 值 |", "|---|---|",
          "| 文件总数 | {} |".format(agg["files"]),
          "| 总大小 | {} |".format(human(agg["bytes"])),
          "| 无封面 | {} |".format(agg["no_cover"]),
          "| 读取失败 | {} |".format(agg["bad"]), ""]
    md += ["## 规格分布", "", "| 规格 | 数量 |", "|---|---|"]
    for k, v in sorted(agg["by_rate"].items(), reverse=True):
        md.append("| {} | {} |".format(k, v))
    md += ["", "## 声道分布", "", "| 声道 | 数量 |", "|---|---|"]
    for k, v in sorted(agg["by_channels"].items()):
        md.append("| {} | {} |".format(k, v))
    md += ["", "## 扩展名分布", "", "| 扩展名 | 数量 |", "|---|---|"]
    for k, v in sorted(agg["by_ext"].items(), key=lambda x: -x[1]):
        md.append("| {} | {} |".format(k, v))
    md_path = out_dir / "scan.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    log("scan", "完成", files=agg["files"], size=human(agg["bytes"]),
        no_cover=agg["no_cover"], csv=str(csv_path), md=str(md_path))
    return 0


# ---------------------------------------------------------------- rename
def build_name(f, mode):
    artist = tag(f, "artist") or tag(f, "albumartist")
    album = tag(f, "album")
    title = tag(f, "title")
    comp = tag(f, "composer")
    genre = tag(f, "genre").lower()
    classical = bool(comp) or "classical" in genre
    if mode == "pop":
        classical = False
    elif mode == "classical":
        classical = True
    if classical:
        base = " - ".join(x for x in (title, comp) if x)
    else:
        base = " - ".join(x for x in (title, artist, album) if x)
    return base or "", classical


def template_for(mode, classical, custom=None):
    """决定用哪个模板：命令行/界面给了自定义模板就用它，否则用内置默认。"""
    if custom:
        return custom
    if mode == "classical" or (mode == "auto" and classical):
        return DEFAULT_TEMPLATE_CLASSICAL
    return DEFAULT_TEMPLATE_POP


def cmd_rename(args, log):
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False), log)
    plan = []
    stats = {"classical": 0, "pop": 0, "same": 0}

    for p in iter_flac(root):
        f = open_flac(p)
        if f is None:
            continue
        base, classical = build_name(f, args.mode)
        if getattr(args, "template", None):
            tpl_name = build_name_by_template(
                f, args.template, stem=p.stem, ext=p.suffix)
            if tpl_name:
                base = tpl_name
            else:
                stats["tpl_empty"] = stats.get("tpl_empty", 0) + 1
        if not base:
            base = p.stem
        stats["classical" if classical else "pop"] += 1
        new = p.with_name(safe_name(base, args.max_len) + ".flac")
        if new == p:
            stats["same"] += 1
            continue
        plan.append((p, new, "古典" if classical else "普通"))

    seen = {}
    for i, (src, dst, kind) in enumerate(plan):
        key = str(dst).lower()
        if key in seen:
            seen[key] += 1
            dst = dst.with_name(dst.stem + " ({})".format(seen[key]) + ".flac")
            plan[i] = (src, dst, kind)
        else:
            seen[key] = 1

    log("rename", "计划", total=len(plan), 古典=stats["classical"],
        模板无值=stats.get("tpl_empty", 0), 模板=(args.template or "（默认）"),
        普通=stats["pop"], 已合规=stats["same"], apply=args.apply)
    for src, dst, kind in plan[: args.show]:
        log("rename", "{} -> {}".format(src.name, dst.name), 类型=kind)

    done = 0
    if args.apply:
        for src, dst, _ in plan:
            try:
                if dst.exists():
                    log("rename", "跳过(目标已存在)", src=str(src))
                    continue
                src.rename(dst)
                done += 1
            except Exception as exc:
                log("rename", "失败", src=str(src), error=str(exc))
    log("rename", "结束", 已改名=done, 模式="dry-run" if not args.apply else "apply")
    return 0


# ---------------------------------------------------------------- flatten
def cmd_flatten(args, log):
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False), log)
    moved = 0
    plan = []
    for album in sorted([d for d in root.rglob("*") if d.is_dir()]):
        if DOWNMIX_DIRNAME in album.parts:
            continue
        flacs = [p for p in album.rglob("*.flac") if DOWNMIX_DIRNAME not in p.parts]
        if not flacs:
            continue
        deep = [p for p in flacs if p.parent != album]
        if not deep:
            continue
        for src in deep:
            dst = album / src.name
            if dst.exists() and args.apply:
                log("flatten", "跳过(同名已存在)", dst=str(dst))
                continue
            plan.append((src, dst))

    for src, dst in plan[: args.show]:
        log("flatten", "{} -> {}".format(src.parent.name + "/" + src.name, dst.parent.name + "/" + dst.name))
    log("flatten", "计划", total=len(plan), apply=args.apply)

    if args.apply:
        for src, dst in plan:
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as exc:
                log("flatten", "失败", src=str(src), error=str(exc))
        if args.prune_empty:
            removed = 0
            for d in sorted(root.rglob("*"), key=lambda x: -len(str(x))):
                if d.is_dir() and d.name != DOWNMIX_DIRNAME:
                    try:
                        if not any(d.iterdir()):
                            d.rmdir()
                            removed += 1
                    except OSError:
                        pass
            log("flatten", "清理空目录", 删除=removed)
    log("flatten", "结束", 移动=moved)
    return 0


# ---------------------------------------------------------------- cover
def cmd_cover(args, log):
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False), log)
    default_img = Path(args.image) if args.image else None

    # 五个桶，一个桶只装一种含义，互不重叠：
    #   write       本次写入成功（或 dry-run 下"将会写入"）
    #   have        本来就有封面，正确跳过 —— 是"已完成"
    #   no_img      没有任何可用图片 —— 是"没做"，不是跳过
    #   unsupported 格式不支持（DSF/WAV…）—— 既不是失败也不是跳过
    #   failed      读取/写入真的出错
    stat = {"write": 0, "have": 0, "no_img": 0, "unsupported": 0, "failed": 0,
            "downmix_scan": 0}
    no_img_dirs = {}          # 目录 -> [文件名]
    unsupported_ext = {}      # 扩展名 -> [文件名]
    failed_list = []

    def audio_files():
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                yield p

    for p in audio_files():
        rel = str(p.relative_to(root))
        if DOWNMIX_DIRNAME in p.parts:
            stat["downmix_scan"] += 1
        ext = p.suffix.lower()
        try:
            have = has_cover(p)
        except Exception as exc:
            stat["failed"] += 1
            failed_list.append((rel, "读取标签失败: {}".format(exc)))
            continue
        if have is None:
            stat["unsupported"] += 1
            unsupported_ext.setdefault(ext, []).append(rel)
            continue
        if have and not args.overwrite:
            stat["have"] += 1
            continue

        img_path, why, io_error = (default_img, "命令行指定", False)
        if img_path is None:
            img_path, why, io_error = find_cover_image(p, root)
        if img_path is None:
            if io_error:
                stat["failed"] += 1
                failed_list.append((rel, why))
            else:
                stat["no_img"] += 1
                d = str(p.parent.relative_to(root)) or "."
                no_img_dirs.setdefault(d, []).append(p.name)
            continue
        try:
            if not args.apply:
                stat["write"] += 1
                log("cover", "将写入", file=rel, 图=img_path.name, 来源=why)
                continue
            embed_cover(p, img_path, args.max_px, args.quality)
            stat["write"] += 1
            log("cover", "写入", file=rel, 图=img_path.name, 来源=why)
        except Exception as exc:
            stat["failed"] += 1
            failed_list.append((rel, "{}: {}".format(why, exc)))

    log("cover", "结束",
        写入=stat["write"], 已有封面=stat["have"], 无图=stat["no_img"],
        不支持格式=stat["unsupported"], 失败=stat["failed"],
        其中5dot1降混目录=stat["downmix_scan"],
        模式="dry-run" if not args.apply else "apply")
    log("cover", "口径说明",
        含义="写入=本次写好；已有封面=本来就有的正确跳过（已完成）；"
             "无图=目录链上没有可用图片（是没做，不是跳过）；"
             "不支持格式=工具不写该格式，不算失败；失败=读写真的出错")

    # 每个桶都下钻到具体文件，不做「前 N 条」截断
    for ext in sorted(unsupported_ext):
        files = unsupported_ext[ext]
        log("cover", "不支持格式", 扩展名=ext, 数量=len(files),
            files="; ".join(files))
    for d in sorted(no_img_dirs, key=lambda k: -len(no_img_dirs[k])):
        files = no_img_dirs[d]
        log("cover", "无图目录", 目录=d, 数量=len(files), files="; ".join(files))
    for f, why in failed_list:
        log("cover", "失败明细", file=f, reason=why)
    return 0


# ---------------------------------------------------------------- downmix
PAN = ("[0:a]pan=stereo|"
       "c0=1.0*FL+0.707*FC+0.707*BL|"
       "c1=1.0*FR+0.707*FC+0.707*BR,volume=0.7[a]")


def cmd_downmix(args, log):
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False), log)
    ff = args.ffmpeg or shutil.which("ffmpeg")
    if not ff:
        log("downmix", "找不到 ffmpeg，用 --ffmpeg 指定")
        return 2

    todo = []
    for p in sorted(root.rglob("*.flac")):
        if DOWNMIX_DIRNAME in p.parts:
            continue
        f = open_flac(p)
        if f and f.info.channels > 2:
            todo.append(p)

    log("downmix", "计划", 多声道文件=len(todo), apply=args.apply)
    ok = failed = 0
    for src in todo:
        dst = (Path(args.out) if args.out else src.parent / DOWNMIX_DIRNAME) / src.name
        log("downmix", "{} -> {}".format(src.name[:60], dst.parent.name + "/" + dst.name[:50]))
        if not args.apply:
            ok += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = run_hidden([ff, "-y", "-v", "error", "-i", str(src),
                                "-filter_complex", PAN, "-map", "[a]",
                                "-c:a", "flac", "-sample_fmt", "s32", str(dst)],
                               capture_output=True, text=True, timeout=900)
            if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                ok += 1
                if args.delete_source:
                    src.unlink()
            else:
                failed += 1
                log("downmix", "失败", file=str(src), err=(r.stderr or "")[-160:])
        except Exception as exc:
            failed += 1
            log("downmix", "异常", file=str(src), error=str(exc))
    log("downmix", "结束", 成功=ok, 失败=failed)
    return 0


# ---------------------------------------------------------------- cleanup
def cmd_cleanup(args, log):
    """删旧文件：按声道分别计数，新集覆盖旧集才删；不匹配一律保留。"""
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False) and getattr(args, "force", False), log)
    old_rate = int(args.old_rate)

    def norm(s):
        s = s.lower()
        s = re.sub(r"^\[[^\]]*\]", "", s)
        s = re.sub(r"^cd\d+\s+", "", s)
        s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
        return " ".join(s.split())

    new_dirs, old_dirs = {}, {}
    for d in root.iterdir():
        if not d.is_dir():
            continue
        flacs = [p for p in d.rglob("*.flac") if DOWNMIX_DIRNAME not in p.parts]
        if not flacs:
            continue
        f = open_flac(flacs[0])
        if f is None:
            continue
        if f.info.sample_rate != old_rate and f.info.sample_rate > old_rate:
            new_dirs.setdefault(norm(d.name), []).append((d, len(flacs)))
        elif f.info.sample_rate == old_rate:
            old_dirs.setdefault(norm(d.name), []).append((d, len(flacs)))

    def find_new(key):
        best, bl = None, 0
        for nk, items in new_dirs.items():
            if nk.startswith(key) or key.startswith(nk):
                ln = min(len(nk), len(key))
                if ln > bl:
                    best, bl = items[0], ln
        return best

    deleted = kept = 0
    for key, olds in sorted(old_dirs.items()):
        hit = find_new(key)
        for od, _cnt in olds:
            flacs = [p for p in od.rglob("*.flac") if DOWNMIX_DIRNAME not in p.parts]
            two = [p for p in flacs if (open_flac(p) and open_flac(p).info.channels == 2)]
            six = [p for p in flacs if (open_flac(p) and open_flac(p).info.channels > 2)]
            if hit is None:
                log("cleanup", "保留(无对应新目录)", dir=od.name[:48])
                kept += len(two) + len(six)
                continue
            nd, ncnt = hit
            mc = nd / DOWNMIX_DIRNAME
            mcnt = len(list(mc.glob("*.flac"))) if mc.exists() else 0
            targets = []
            if ncnt >= len(two):
                targets += two
            else:
                log("cleanup", "保留(新集不足)", dir=od.name[:44], 新=ncnt, 旧=len(two))
                kept += len(two)
            if six:
                if mcnt >= len(six):
                    targets += six
                else:
                    log("cleanup", "保留(降混不足)", dir=od.name[:44], 降混=mcnt, 旧6ch=len(six))
                    kept += len(six)
            for p in targets:
                if args.apply and args.force:
                    try:
                        p.unlink()
                        deleted += 1
                    except Exception as exc:
                        log("cleanup", "删除失败", file=str(p), error=str(exc))
                else:
                    deleted += 1
                    if len(targets) <= 4:
                        log("cleanup", "将删除 {}".format(p.name[:56]))

    log("cleanup", "结束", 可删=deleted, 保留=kept,
        模式=("apply+force" if (args.apply and args.force) else "dry-run"))
    return 0


# ---------------------------------------------------------------- verify
def cmd_verify(args, log):
    root = Path(args.root)
    issues = {"zero_rate": [], "no_cover": [], "dup_name": [], "empty_dir": [],
              "leftover": [], "bad": []}
    seen = {}
    total = 0
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".iso", ".dsf", ".dff"}:
            issues["leftover"].append(str(p.relative_to(root)))
        if not p.is_file() or p.suffix.lower() != ".flac":
            continue
        total += 1
        f = open_flac(p)
        if f is None:
            issues["bad"].append(str(p.relative_to(root)))
            continue
        if f.info.sample_rate <= 0 or f.info.bits_per_sample <= 0:
            issues["zero_rate"].append(str(p.relative_to(root)))
        if not f.pictures:
            issues["no_cover"].append(str(p.relative_to(root)))
        key = str(p).lower()
        seen[key] = seen.get(key, 0) + 1

    for d in sorted(root.rglob("*")):
        if d.is_dir() and not any(d.iterdir()):
            issues["empty_dir"].append(str(d.relative_to(root)))

    report = ["# 音乐库体检报告", "", "生成：{}".format(time.strftime("%Y-%m-%d %H:%M:%S")),
              "根目录：`{}`".format(root), "FLAC 总数：{}".format(total), ""]
    labels = {"zero_rate": "采样率/位深异常(0)", "no_cover": "无内嵌封面",
              "dup_name": "同名冲突", "empty_dir": "空目录",
              "leftover": "ISO/DSD 残留", "bad": "无法解析"}
    for k, label in labels.items():
        items = issues[k]
        report.append("## {}（{}）".format(label, len(items)))
        if items:
            for it in items[: args.show]:
                report.append("- `{}`".format(it))
            if len(items) > args.show:
                report.append("- … 另有 {} 条".format(len(items) - args.show))
        else:
            report.append("- 无")
        report.append("")

    out = Path(args.out) if args.out else root / "_musiclib报告"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "verify.md"
    path.write_text("\n".join(report) + "\n", encoding="utf-8")
    log("verify", "完成", flac=total, 报告=str(path),
        异常=len(issues["zero_rate"]), 无封面=len(issues["no_cover"]),
        空目录=len(issues["empty_dir"]), 残留=len(issues["leftover"]))
    return 0



# ---------------------------------------------------------------- lyrics
LRCLIB = "https://lrclib.net/api/search"
LRCLIB_UA = "musiclib/0.1 (https://github.com/minterinz/musiclib)"
LRCLIB_MIN_INTERVAL = 1.0        # 每秒最多 1 次，官方要求别打太猛
_last_call = [0.0]


def _lrclib_query(artist, title, album):
    """返回候选列表；失败返回空。"""
    import urllib.parse
    import urllib.request

    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if album:
        params["album_name"] = album
    url = LRCLIB + "?" + urllib.parse.urlencode(params)
    gap = time.time() - _last_call[0]
    if gap < LRCLIB_MIN_INTERVAL:
        time.sleep(LRCLIB_MIN_INTERVAL - gap)
    _last_call[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _pick(cands, duration=None):
    """优先时长最接近、且有同步歌词的候选。"""
    if not cands:
        return None
    best, best_score = None, 1e9
    for c in cands:
        if not (c.get("syncedLyrics") or c.get("plainLyrics")):
            continue
        score = 0.0
        if duration:
            d = c.get("duration")
            if d:
                score += abs(float(d) - float(duration))
            else:
                score += 5
        if not c.get("syncedLyrics"):
            score += 2
        if score < best_score:
            best, best_score = c, score
    if best is None:
        return None
    if duration and best.get("duration"):
        if abs(float(best["duration"]) - float(duration)) > 10:
            return None
    return best


def cmd_lyrics(args, log):
    root = Path(args.root)
    assert_writable(root, getattr(args, "apply", False), log)
    want_lrc = args.save_lrc
    done = skip = miss = fail = 0
    for p in iter_flac(root):
        if args.limit and (done + skip + miss + fail) >= args.limit:
            break
        f = open_flac(p)
        if f is None:
            fail += 1
            continue
        if (f.get("lyrics") or f.get("unsyncedlyrics")) and not args.overwrite:
            skip += 1
            continue
        artist = tag(f, "artist") or tag(f, "albumartist")
        title = tag(f, "title")
        album = tag(f, "album")
        if not title:
            miss += 1
            continue
        cands = _lrclib_query(artist, title, album)
        hit = _pick(cands, f.info.length if f.info.length else None)
        if hit is None:
            miss += 1
            log("lyrics", "未找到 {}".format(p.name[:56]), artist=artist)
            continue
        synced = hit.get("syncedLyrics") or ""
        plain = hit.get("plainLyrics") or ""
        log("lyrics", "{} <- {} ({})".format(p.name[:50], hit.get("trackName", "")[:30],
                                            "同步" if synced else "纯文本"))
        if not args.apply:
            done += 1
            continue
        try:
            if synced:
                f["LYRICS"] = synced
            elif plain:
                f["UNSYNCEDLYRICS"] = plain
            f.save()
            if want_lrc:
                side = p.with_suffix(".lrc")
                side.write_text(synced or plain, encoding="utf-8")
            done += 1
        except Exception as exc:
            fail += 1
            log("lyrics", "写入失败 {}".format(exc), file=str(p))
    log("lyrics", "结束", 写入=done, 已有=skip, 未找到=miss, 失败=fail,
        源="lrclib.net", 模式="apply" if args.apply else "dry-run")
    return 0



# ---------------------------------------------------------------- lyricfmt
LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
AD_WORDS = ("本歌词", "歌词制作", "歌词下载", "歌词由", "爱歌词", "欢迎光临",
            "www.", "http://", "https://", "QQ群", "千千静听", "酷狗音乐", "网易云音乐",
            "版权所有", "LRC歌词网", "整理制作")


def lrc_parse(text):
    """LRC -> [(秒, 文本)]，支持一行多时间标签。"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = list(LRC_TAG.finditer(line))
        if not stamps:
            continue
        body = LRC_TAG.sub("", line).strip()
        for m in stamps:
            mm, ss, frac = m.group(1), m.group(2), m.group(3) or "0"
            frac = (frac + "00")[:3]
            t = int(mm) * 60 + int(ss) + int(frac) / 1000.0
            out.append((t, body))
    out.sort(key=lambda x: x[0])
    return out


def lrc_clean(items, offset=0.0, strip_ads=False):
    res = []
    for t, body in items:
        if strip_ads and body and any(w in body for w in AD_WORDS):
            continue
        nt = max(0.0, t + offset)
        if not body:
            continue
        res.append((nt, body))
    return res


def to_srt(items):
    def ts(x):
        h = int(x // 3600)
        m = int(x % 3600 // 60)
        sec = int(x % 60)
        ms = int(round((x - int(x)) * 1000))
        if ms == 1000:
            ms = 0
            sec += 1
        return "{:02d}:{:02d}:{:02d},{:03d}".format(h, m, sec, ms)
    out = []
    for i, (t, body) in enumerate(items, 1):
        end = items[i][0] if i < len(items) else t + 4.0
        if end <= t:
            end = t + 2.0
        out.append("{}\n{} --> {}\n{}\n".format(i, ts(t), ts(end), body))
    return "\n".join(out)


def to_ass(items, title=""):
    def ts(x):
        h = int(x // 3600)
        m = int(x % 3600 // 60)
        s = x % 60
        return "{:d}:{:02d}:{:05.2f}".format(h, m, s)
    head = (
        "[Script Info]\nScriptType: v4.00+\nTitle: {}\nPlayResX: 1280\nPlayResY: 720\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,2,2,20,20,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ).format(title)
    lines = []
    for i, (t, body) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else t + 4.0
        if end <= t:
            end = t + 2.0
        lines.append("Dialogue: 0,{},{},Default,,0,0,0,,{}".format(ts(t), ts(end), body))
    return head + "\n".join(lines) + "\n"


def to_txt(items):
    return "\n".join(body for _, body in items) + "\n"


def to_lrc(items):
    def ts(x):
        m = int(x // 60)
        s = x - m * 60
        return "[{:02d}:{:05.2f}]".format(m, s)
    return "\n".join("{}{}".format(ts(t), body) for t, body in items) + "\n"


CONVERTERS = {"lrc": to_lrc, "srt": to_srt, "ass": to_ass, "txt": to_txt}

LYRIC_INPUT_EXT = (".lrc", ".srt", ".ass", ".ssa", ".smi", ".ksc", ".txt")


def cmd_lyricfmt(args, log):
    """歌词多格式互转 + 中文处理（引擎见 lyriceng.py）。"""
    import lyriceng

    src = Path(args.root)
    if src.is_file():
        files = [src]
    else:
        files = [p for p in src.rglob("*")
                 if p.is_file() and p.suffix.lower() in LYRIC_INPUT_EXT]
        # TXT 只在与其它歌词同目录且显式要求时才处理，避免把普通文本也当歌词
        if not getattr(args, "include_txt", False):
            files = [p for p in files if p.suffix.lower() != ".txt"]
    if not files:
        log("lyricfmt", "没有找到歌词文件", root=str(src),
            ext=",".join(LYRIC_INPUT_EXT))
        return 0

    outdir = Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
        # 输出目录若在输入目录里面，必须把它排除，否则会把刚生成的产物当输入再转一遍
        try:
            od = outdir.resolve()
            files = [p for p in files if not str(p.resolve()).startswith(str(od))]
        except Exception:
            pass

    done = 0
    stat = {"files": 0, "no_time": 0, "failed": 0, "written": 0}
    for p in files:
        stat["files"] += 1
        try:
            text, used_enc = lyriceng.read_lyric(p, args.encoding or None)
        except Exception as exc:
            stat["failed"] += 1
            log("lyricfmt", "读取失败", file=p.name, error=str(exc))
            continue
        src_fmt = args.src if args.src != "auto" else (p.suffix.lower().lstrip(".") or "auto")
        if src_fmt == "ssa":
            src_fmt = "ass"
        ly = lyriceng.process(
            text, src_fmt=src_fmt,
            offset=args.offset, do_strip_ads=args.strip_ads,
            hans=args.to_hans, hant=args.to_hant,
            pinyin=args.pinyin, do_split=args.split, do_merge=args.merge,
        )
        if not ly.lines:
            stat["no_time"] += 1
            log("lyricfmt", "解析不出歌词行", file=p.name, 编码=used_enc, 源格式=src_fmt)
            continue
        for fmt in args.to:
            try:
                body = lyriceng.render(ly, fmt, title=p.stem)
            except Exception as exc:
                stat["failed"] += 1
                log("lyricfmt", "生成失败", file=p.name, fmt=fmt, error=str(exc))
                continue
            dst = (outdir or p.parent) / (p.stem + "." + fmt)
            log("lyricfmt", "{} -> {}  {} 行  (源编码 {})".format(
                p.name[:36], dst.name, len(ly.lines), used_enc))
            if args.apply:
                dst.write_text(body, encoding="utf-8")
                stat["written"] += 1
                done += 1

    log("lyricfmt", "结束",
        文件=stat["files"], 输出=stat["written"], 无可用行=stat["no_time"],
        失败=stat["failed"], 输出格式=",".join(args.to),
        偏移=args.offset, 去广告=args.strip_ads,
        简繁="→简" if args.to_hans else ("→繁" if args.to_hant else "不变"),
        拼音=args.pinyin or "不加", 拆分=args.split, 合并=args.merge,
        模式="apply" if args.apply else "dry-run")
    return 0


# ---------------------------------------------------------------- main
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="musiclib", description="音乐库整理 / 修改工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  musiclib.py scan    --root /mnt/sd\n"
               "  musiclib.py rename  --root /mnt/sd --apply\n"
               "  musiclib.py flatten --root /mnt/sd --apply --prune-empty\n"
               "  musiclib.py cover   --root /mnt/sd --image cover.jpg --apply\n"
               "  musiclib.py downmix --root /mnt/sd --apply --delete-source\n"
               "  musiclib.py cleanup --root /mnt/sd --old-rate 44100 --apply --force\n"
               "  musiclib.py verify  --root /mnt/sd\n")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, applyable=True):
        p.add_argument("--root", required=True, help="音乐库根目录")
        p.add_argument("--log", help="JSONL 日志路径")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--show", type=int, default=20, help="控制台最多显示多少条明细")
        if applyable:
            p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")

    p = sub.add_parser("scan", help="扫描统计")
    common(p, applyable=False)
    p.add_argument("--out", help="报告输出目录")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("rename", help="规范命名（可自定义模板）")
    common(p)
    p.add_argument("--mode", choices=["auto", "pop", "classical"], default="auto")
    p.add_argument("--max-len", type=int, default=150)
    p.add_argument("--template", help="文件名模板，如 \"{标题} - {演出者}\"；"
                                      "可用变量见 --template 说明（默认：普通=标题-演出者-专辑，古典=标题-作曲家）")
    p.add_argument("--show-vars", action="store_true", help="打印可用模板变量后退出")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("flatten", help="目录扁平化")
    common(p)
    p.add_argument("--prune-empty", action="store_true")
    p.set_defaults(func=cmd_flatten)

    p = sub.add_parser("cover", help="封面内嵌")
    common(p)
    p.add_argument("--image", help="统一封面图；缺省用各目录下的 cover.jpg")
    p.add_argument("--max-px", type=int, default=700)
    p.add_argument("--quality", type=int, default=86)
    p.add_argument("--overwrite", action="store_true", help="已有封面也重写")
    p.set_defaults(func=cmd_cover)

    p = sub.add_parser("downmix", help="5.1 → 立体声")
    common(p)
    p.add_argument("--ffmpeg", help="ffmpeg 路径")
    p.add_argument("--out", help="输出根目录（默认各专辑下 5.1降混/）")
    p.add_argument("--delete-source", action="store_true", help="降混成功后删除多声道原件")
    p.set_defaults(func=cmd_downmix)

    p = sub.add_parser("cleanup", help="旧文件清理")
    common(p)
    p.add_argument("--old-rate", type=int, default=44100, help="旧采样率，默认 44100")
    p.add_argument("--force", action="store_true", help="确实删除（不加则只列计划）")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("verify", help="库体检")
    common(p, applyable=False)
    p.add_argument("--out", help="报告输出目录")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("lyricfmt", help="歌词多格式互转与中文处理（LRC/SRT/ASS/SMI/KSC ↔ LRC/SRT/ASS/TXT）")
    p.add_argument("--root", required=True, help="歌词文件，或含歌词的目录")
    p.add_argument("--src", default="auto",
                   choices=["auto", "lrc", "srt", "ass", "ssa", "smi", "ksc", "txt"],
                   help="输入格式，默认按扩展名自动判断")
    p.add_argument("--to", nargs="+", default=["srt"], choices=["lrc", "srt", "ass", "txt"])
    p.add_argument("--offset", type=float, default=0.0, help="时间整体偏移秒数，可负数")
    p.add_argument("--strip-ads", action="store_true", help="删除广告/来源/网址行")
    p.add_argument("--to-hans", action="store_true", help="转简体")
    p.add_argument("--to-hant", action="store_true", help="转繁体")
    p.add_argument("--pinyin", choices=["both", "append", "only"],
                   help="加拼音：both=下行拼音 / append=同行括号 / only=只留拼音")
    p.add_argument("--split", action="store_true", help="长句按标点拆成多行")
    p.add_argument("--merge", action="store_true", help="短句合并成一行")
    p.add_argument("--encoding", default=None,
                   help="源文件编码；默认自动识别 UTF-8/UTF-16/GBK/Big5")
    p.add_argument("--include-txt", action="store_true",
                   help="把 .txt 也当歌词处理（默认跳过，避免误伤普通文本）")
    p.add_argument("--out", help="输出目录（默认与源同目录）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--log")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--show", type=int, default=20)
    p.set_defaults(func=cmd_lyricfmt)

    p = sub.add_parser("lyrics", help="批量取歌词写入 FLAC 标签（源 lrclib.net）")
    common(p)
    p.add_argument("--overwrite", action="store_true", help="已有歌词也覆盖")
    p.add_argument("--save-lrc", action="store_true", help="同时存同名 .lrc 文件")
    p.add_argument("--limit", type=int, help="只处理前 N 个（试跑用）")
    p.set_defaults(func=cmd_lyrics)

    args = ap.parse_args(argv)
    if getattr(args, "show_vars", False):
        print("可用变量：" + template_vars_help())
        return 0

    root = Path(args.root)
    # lyricfmt 允许直接指向单个歌词文件，其余子命令要求目录
    accepts_file = args.cmd in ("lyricfmt",)
    if not (root.is_dir() or (accepts_file and root.is_file())):
        sys.stderr.write("路径不存在或类型不对（{}）: {}\n".format(
            "文件或目录" if accepts_file else "需要目录", root))
        return 2

    log = Log(Path(args.log) if args.log else None, quiet=args.quiet)
    try:
        return args.func(args, log) or 0
    except KeyboardInterrupt:
        log("main", "中断")
        return 6
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
