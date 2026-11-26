# -*- coding: utf-8 -*-
"""把 I:\ 上的 WAV 全部转成 FLAC —— 逐字节无损，保留标签与封面。

转换参数：
  ffmpeg -c:a flac -compression_level 8 -map_metadata 0
  · compression_level 8 = 最高压缩（只影响体积，不影响音频）
  · 不指定 -ar / -sample_fmt，让 ffmpeg 沿用原参数（48k/24bit 等一律不动）

无损验证：把两边的解码样本统一成 s32le 原始流算 MD5，必须完全相同。

封面：ffmpeg 会把 WAV 里的 mjpeg 封面转成 PNG 带过去；
      再用 musiclib 的 embed_cover 重写成 JPEG 并补上 width/height/depth
      （Windows 资源管理器靠这几个字段画缩略图）。

原 WAV 一律保留，不删。
"""
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"E:\我的文档\deepseek-harness\default-workspace\musiclib")
import musiclib as M

FFMPEG = r"G:\桌面\musiclib-0.1.4\bin\ffmpeg.exe"
if not Path(FFMPEG).exists():
    FFMPEG = "ffmpeg.exe"

ROOT = Path("I:\\")


def raw_md5(path: Path) -> str:
    """把音频解码成 s32le 原始样本流，算 MD5（消除采样格式差异）。"""
    p = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return hashlib.md5(p.stdout).hexdigest()


def main():
    wavs = sorted(p for p in ROOT.glob("*.wav"))
    # 也扫子目录
    for sub in ROOT.iterdir():
        if sub.is_dir():
            wavs += sorted(sub.glob("*.wav"))
    if not wavs:
        print("没有 WAV 文件")
        return

    print(f"找到 {len(wavs)} 个 WAV，共 {sum(p.stat().st_size for p in wavs)/1024/1024:.0f} MB")
    print("=" * 78)

    total_ok = total_fail = 0
    saved = 0
    report = []

    for i, wav in enumerate(wavs, 1):
        print(f"\n[{i}/{len(wavs)}] {wav.name}")
        target = wav.with_suffix(".flac")
        if target.exists():
            print(f"  跳过：{target.name} 已存在")
            continue

        tmp = wav.with_name(wav.stem + ".w2f.tmp.flac")
        if tmp.exists():
            tmp.unlink()

        t0 = time.time()
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", str(wav),
             "-map", "0:a:0", "-c:a", "flac", "-compression_level", "8",
             "-map_metadata", "0", str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not tmp.exists():
            print(f"  [失败] 转换: {r.stderr[:200]}")
            total_fail += 1
            report.append((wav.name, "转换失败", ""))
            continue

        # 无损校验
        try:
            m1 = raw_md5(wav)
            m2 = raw_md5(tmp)
        except Exception as exc:
            print(f"  [失败] 校验: {exc}")
            tmp.unlink(missing_ok=True)
            total_fail += 1
            continue

        if m1 != m2:
            print(f"  [失败] 无损校验不一致！WAV={m1[:12]} FLAC={m2[:12]}")
            tmp.unlink(missing_ok=True)
            total_fail += 1
            report.append((wav.name, "无损校验失败", ""))
            continue

        # 重写封面为 JPEG（补 width/height/depth，确保 Windows 能显示）
        try:
            raw_cover = M.get_cover_bytes(wav)
            if raw_cover:
                img = wav.with_name(wav.stem + ".w2f.cover.jpg")
                img.write_bytes(raw_cover)
                M.embed_cover(tmp, img, max_px=700)
                img.unlink(missing_ok=True)
        except Exception as exc:
            print(f"  封面重写跳过: {exc}")

        os.replace(tmp, target)
        dt = time.time() - t0
        a, b = wav.stat().st_size, target.stat().st_size
        saved += (a - b)
        total_ok += 1
        print(f"  [OK] {a/1024/1024:.1f} MB -> {b/1024/1024:.1f} MB  ({dt:.1f}s)  无损校验通过")
        report.append((wav.name, "OK", f"{a/1024/1024:.1f}->{b/1024/1024:.1f} MB"))

    print("\n" + "=" * 78)
    print(f"完成：成功 {total_ok} / 失败 {total_fail}")
    if saved > 0:
        print(f"体积节省：{saved/1024/1024:.0f} MB")
    print("\n明细：")
    for name, st, note in report:
        print(f"  {st:12} {name[:50]:52} {note}")


if __name__ == "__main__":
    main()
