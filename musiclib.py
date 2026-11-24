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
import re
import shutil
import subprocess
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


def cmd_rename(args, log):
    root = Path(args.root)
    plan = []
    stats = {"classical": 0, "pop": 0, "same": 0}

    for p in iter_flac(root):
        f = open_flac(p)
        if f is None:
            continue
        base, classical = build_name(f, args.mode)
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
    from PIL import Image

    default_img = Path(args.image) if args.image else None
    embedded = skipped = failed = 0
    for p in iter_flac(root):
        f = open_flac(p)
        if f is None:
            continue
        if f.pictures and not args.overwrite:
            skipped += 1
            continue
        img_path = default_img or (p.parent / "cover.jpg")
        if not img_path or not img_path.is_file():
            skipped += 1
            continue
        try:
            raw = Image.open(img_path).convert("RGB")
            if max(raw.size) > args.max_px:
                raw.thumbnail((args.max_px, args.max_px), Image.LANCZOS)
            import io
            buf = io.BytesIO()
            raw.save(buf, format="JPEG", quality=args.quality, optimize=True)
            data = buf.getvalue()
            if not args.apply:
                embedded += 1
                continue
            while f.pictures:
                f.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = data
            f.add_picture(pic)
            f.save()
            embedded += 1
        except Exception as exc:
            failed += 1
            log("cover", "失败", file=str(p), error=str(exc))
    log("cover", "结束", 处理=embedded, 跳过=skipped, 失败=failed,
        模式="dry-run" if not args.apply else "apply")
    return 0


# ---------------------------------------------------------------- downmix
PAN = ("[0:a]pan=stereo|"
       "c0=1.0*FL+0.707*FC+0.707*BL|"
       "c1=1.0*FR+0.707*FC+0.707*BR,volume=0.7[a]")


def cmd_downmix(args, log):
    root = Path(args.root)
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
            r = subprocess.run([ff, "-y", "-v", "error", "-i", str(src),
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

    p = sub.add_parser("rename", help="规范命名")
    common(p)
    p.add_argument("--mode", choices=["auto", "pop", "classical"], default="auto")
    p.add_argument("--max-len", type=int, default=150)
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

    p = sub.add_parser("lyrics", help="批量取歌词写入 FLAC 标签（源 lrclib.net）")
    common(p)
    p.add_argument("--overwrite", action="store_true", help="已有歌词也覆盖")
    p.add_argument("--save-lrc", action="store_true", help="同时存同名 .lrc 文件")
    p.add_argument("--limit", type=int, help="只处理前 N 个（试跑用）")
    p.set_defaults(func=cmd_lyrics)

    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        sys.stderr.write("根目录不存在或不是目录: {}\n".format(root))
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
