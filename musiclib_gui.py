#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musiclib GUI — 音乐库整理/修改 绿色软件（图形界面）

界面流程
  1) 选来源：整个文件夹（递归）或手动勾选一批文件
  2) 勾步骤：规范命名 / 目录扁平化 / 封面内嵌 / 5.1降混 / 清理旧文件
  3) 选输出：可直接原地处理，也可复制到指定目录
  4) 预览：只列计划，不动任何文件
  5) 开始：后台线程执行，实时日志 + 进度条，可随时停止

依赖：tkinter（Python 自带）、mutagen、Pillow（setup 已装）、ffmpeg（包内自带）
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 复用核心逻辑
try:
    from musiclib import (AUDIO_EXT, DOWNMIX_DIRNAME, PAN, build_name,
                          iter_flac, open_flac, safe_name, tag,
                          _lrclib_query, _pick)
except Exception:  # 单文件打包后可能改名，退化为内置副本
    from musiclib_core import (AUDIO_EXT, DOWNMIX_DIRNAME, PAN, build_name,
                               iter_flac, open_flac, safe_name, tag,
                               _lrclib_query, _pick)

APP_TITLE = "musiclib 0.1.2 — 音乐库整理工具"
COLOR_BG = "#f5f6f8"


def human(n):
    f = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return "{:.1f}{}".format(f, u)
        f /= 1024.0
    return "{:.1f}TB".format(f)


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_ffmpeg():
    for cand in (app_dir() / "bin" / "ffmpeg.exe", app_dir() / "ffmpeg.exe"):
        if cand.is_file():
            return str(cand)
    return shutil.which("ffmpeg") or ""


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.total = 0
        self.done = 0

        root.title(APP_TITLE)
        root.geometry("1020x720")
        root.minsize(880, 620)
        root.configure(bg=COLOR_BG)
        try:
            root.iconbitmap(default="")
        except Exception:
            pass

        self._build_ui()
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # 来源
        f1 = ttk.LabelFrame(self.root, text=" 1. 选择来源 ")
        f1.pack(fill="x", **pad)

        self.mode = tk.StringVar(value="dir")
        ttk.Radiobutton(f1, text="整个文件夹（含子目录）", variable=self.mode,
                        value="dir", command=self._toggle_mode).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Radiobutton(f1, text="手动选择文件（可多选）", variable=self.mode,
                        value="files", command=self._toggle_mode).grid(row=0, column=1, sticky="w", padx=8)

        self.src_var = tk.StringVar()
        ttk.Entry(f1, textvariable=self.src_var).grid(row=1, column=0, columnspan=3,
                                                      sticky="ew", padx=8, pady=4)
        self.btn_dir = ttk.Button(f1, text="选择文件夹…", command=self.pick_dir)
        self.btn_dir.grid(row=1, column=3, padx=4)
        self.btn_files = ttk.Button(f1, text="选择文件…", command=self.pick_files)
        self.btn_files.grid(row=1, column=4, padx=4)
        f1.columnconfigure(0, weight=1)

        self.sel_info = ttk.Label(f1, text="未选择", foreground="#666")
        self.sel_info.grid(row=2, column=0, columnspan=5, sticky="w", padx=8, pady=(0, 6))
        self.files = []

        # 步骤
        f2 = ttk.LabelFrame(self.root, text=" 2. 选择要做的处理 ")
        f2.pack(fill="x", **pad)

        self.v_rename = tk.BooleanVar(value=True)
        self.v_flatten = tk.BooleanVar(value=True)
        self.v_cover = tk.BooleanVar(value=True)
        self.v_downmix = tk.BooleanVar(value=True)
        self.v_lyrics = tk.BooleanVar(value=True)
        self.v_cleanup = tk.BooleanVar(value=False)
        self.v_copy = tk.BooleanVar(value=False)

        ttk.Checkbutton(f2, text="规范命名（普通=音乐名-歌手-专辑，古典=标题-作曲家，不加序号）",
                        variable=self.v_rename).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="目录扁平化（把子目录里的音频提到专辑第一层，并清理空目录）",
                        variable=self.v_flatten).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="封面内嵌", variable=self.v_cover).grid(row=2, column=0, sticky="w", padx=8, pady=2)

        self.cover_mode = tk.StringVar(value="perdir")
        ttk.Radiobutton(f2, text="各目录 cover.jpg", variable=self.cover_mode,
                        value="perdir").grid(row=2, column=1, sticky="w")
        ttk.Radiobutton(f2, text="统一图片：", variable=self.cover_mode,
                        value="single").grid(row=2, column=2, sticky="w")
        self.cover_path = tk.StringVar()
        ttk.Entry(f2, textvariable=self.cover_path, width=28).grid(row=2, column=3, sticky="w")
        ttk.Button(f2, text="浏览…", command=self.pick_cover, width=8).grid(row=2, column=4, padx=4)

        ttk.Checkbutton(f2, text="5.1 → 立体声降混（BS.775 权重，-3dB 防削波）",
                        variable=self.v_downmix).grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="歌词内嵌（源 lrclib.net，写入 FLAC 的 LYRICS 标签，可另存 .lrc）",
                        variable=self.v_lyrics).grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="清理旧文件（新集覆盖校验通过才删，危险，需二次确认）",
                        variable=self.v_cleanup).grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=2)

        # 输出
        f3 = ttk.LabelFrame(self.root, text=" 3. 输出位置 ")
        f3.pack(fill="x", **pad)
        ttk.Checkbutton(f3, text="处理完复制到指定目录（保留专辑子目录结构）",
                        variable=self.v_copy, command=self._toggle_copy).grid(row=0, column=0, columnspan=2,
                                                                             sticky="w", padx=8, pady=4)
        self.out_var = tk.StringVar()
        self.ent_out = ttk.Entry(f3, textvariable=self.out_var, state="disabled")
        self.ent_out.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self.btn_out = ttk.Button(f3, text="选择输出目录…", command=self.pick_out, state="disabled")
        self.btn_out.grid(row=1, column=1, padx=4)
        f3.columnconfigure(0, weight=1)

        # 按钮
        f4 = ttk.Frame(self.root)
        f4.pack(fill="x", **pad)
        self.btn_preview = ttk.Button(f4, text="预览（不做任何修改）", command=lambda: self.start(dry=True))
        self.btn_preview.pack(side="left", padx=4)
        self.btn_run = ttk.Button(f4, text="开始执行", command=lambda: self.start(dry=False))
        self.btn_run.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(f4, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.ff = find_ffmpeg()
        ttk.Label(f4, text=("ffmpeg: " + (Path(self.ff).name if self.ff else "未找到！")).ljust(20),
                  foreground="#2b7a2b" if self.ff else "#b00020").pack(side="left", padx=12)

        self.pbar = ttk.Progressbar(f4, mode="determinate")
        self.pbar.pack(side="right", fill="x", expand=True, padx=8)

        # 4) 结果区（表格 + 日志两个页签）
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)

        # ---- 页签一：文件清单表格 ----
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="文件清单")

        bar = ttk.Frame(tab1)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="扫描清单", command=self.scan_table).pack(side="left", padx=4)
        ttk.Button(bar, text="只看缺项", command=self.filter_missing).pack(side="left", padx=4)
        ttk.Button(bar, text="显示全部", command=self.filter_all).pack(side="left", padx=4)
        self.summary = ttk.Label(bar, text="未扫描")
        self.summary.pack(side="left", padx=12)

        cols = ("title", "artist", "album", "fmt", "dur", "sr", "cover", "lyrics", "size")
        heads = {"title": ("歌曲标题", 240), "artist": ("艺术家", 150), "album": ("唱片标题", 260),
                 "fmt": ("格式", 60), "dur": ("时长", 70), "sr": ("规格", 90),
                 "cover": ("封面", 50), "lyrics": ("歌词", 50), "size": ("大小", 70)}
        tf = ttk.Frame(tab1)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=14)
        for c in cols:
            text, w = heads[c]
            self.tree.heading(c, text=text,
                              command=lambda cc=c: self.sort_tree(cc))
            self.tree.column(c, width=w, anchor="w", stretch=(c in ("title", "album")))
        ysb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree.tag_configure("miss", background="#FFF3CD")
        self.tree.tag_configure("ok", background="#EAF7EA")
        self.tree.bind("<Double-1>", self.on_row_dbl)

        # ---- 页签二：日志 ----
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="日志")
        self.txt = tk.Text(tab2, wrap="none", bg="#111", fg="#d8ffd8",
                           insertbackground="#d8ffd8", font=("Consolas", 9))
        sb = ttk.Scrollbar(tab2, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.status = ttk.Label(self.root, text="就绪", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _toggle_mode(self):
        folder = self.mode.get() == "dir"
        self.btn_dir.configure(state="normal" if folder else "disabled")
        self.btn_files.configure(state="disabled" if folder else "normal")

    def _toggle_copy(self):
        st = "normal" if self.v_copy.get() else "disabled"
        self.ent_out.configure(state=st)
        self.btn_out.configure(state=st)

    # ------------------------------------------------------------ 表格视图
    def _source_files(self):
        if self.mode.get() == "dir":
            root = Path(self.src_var.get()) if self.src_var.get() else None
            if not root or not root.is_dir():
                return []
            return [p for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() in AUDIO_EXT
                    and DOWNMIX_DIRNAME not in p.parts]
        return [p for p in self.files if p.exists()]

    def scan_table(self):
        files = self._source_files()
        if not files:
            messagebox.showwarning("提示", "请先选择文件夹或文件")
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.summary.configure(text="扫描中… 0/{}".format(len(files)))
        threading.Thread(target=self._scan_worker, args=(files,), daemon=True).start()

    def _scan_worker(self, files):
        rows = []
        for i, p in enumerate(files, 1):
            title = artist = album = ""
            fmt = p.suffix.lower().lstrip(".")
            dur = sr = "-"
            has_cover = has_lyric = False
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            f = open_flac(p)
            if f is not None:
                title = tag(f, "title") or p.stem
                artist = tag(f, "artist") or tag(f, "albumartist")
                album = tag(f, "album")
                secs = int(f.info.length or 0)
                dur = "{}:{:02d}".format(secs // 60, secs % 60)
                sr = "{}Hz/{}bit".format(f.info.sample_rate, f.info.bits_per_sample)
                if f.info.channels > 2:
                    sr += "/{}ch".format(f.info.channels)
                has_cover = bool(f.pictures)
                has_lyric = bool(f.get("lyrics") or f.get("unsyncedlyrics"))
            else:
                title = p.stem
                fmt += "?"
            rows.append((title, artist, album, fmt, dur, sr,
                         "✓" if has_cover else "✗",
                         "✓" if has_lyric else "✗",
                         human(size) if isinstance(size, int) else "-"))
            if i % 50 == 0:
                self.q.put(("tablescan", (i, len(files), None)))
        self.q.put(("tablescan", (len(files), len(files), rows)))

    def _fill_table(self, rows):
        for r in rows:
            miss = ("✗" in (r[6], r[7]))
            self.tree.insert("", "end", values=r, tags=("miss",) if miss else ("ok",))
        total = len(rows)
        no_cover = sum(1 for r in rows if r[6] == "✗")
        no_lyric = sum(1 for r in rows if r[7] == "✗")
        self.summary.configure(
            text="共 {} 个 · 缺封面 {} · 缺歌词 {}".format(total, no_cover, no_lyric))

    def filter_missing(self):
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if v and (v[6] == "✗" or v[7] == "✗"):
                self.tree.move(iid, "", "end")

    def filter_all(self):
        for iid in self.tree.get_children():
            self.tree.move(iid, "", "end")

    def sort_tree(self, col):
        idx = self.tree["columns"].index(col)
        items = [(self.tree.item(i, "values"), i) for i in self.tree.get_children()]

        def keyf(t):
            v = t[0][idx] if idx < len(t[0]) else ""
            return (v == "✗", str(v).lower())
        items.sort(key=keyf, reverse=getattr(self, "_desc", False))
        self._desc = not getattr(self, "_desc", False)
        for pos, (_, iid) in enumerate(items):
            self.tree.move(iid, "", pos)

    def on_row_dbl(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        v = self.tree.item(sel[0], "values")
        messagebox.showinfo("条目", "标题: {}\n艺术家: {}\n唱片: {}\n格式: {}\n时长: {}\n规格: {}\n封面: {}\n歌词: {}".format(*v))

    # ------------------------------------------------------------ 选择
    def pick_dir(self):
        d = filedialog.askdirectory(title="选择音乐库根目录")
        if d:
            self.src_var.set(d)
            self.files = []
            self.sel_info.configure(text="已选择文件夹：{}".format(d))

    def pick_files(self):
        fs = filedialog.askopenfilenames(
            title="选择要处理的音频文件（可多选）",
            filetypes=[("音频文件", "*.flac *.dsf *.dff *.wav *.ape *.mp3 *.m4a *.aiff *.ogg *.wv"),
                       ("全部文件", "*.*")])
        if fs:
            self.files = [Path(x) for x in fs]
            self.src_var.set("{} 个文件".format(len(self.files)))
            self.sel_info.configure(text="已选择 {} 个文件：{}".format(
                len(self.files), ", ".join(p.name for p in self.files[:3]) +
                (" …" if len(self.files) > 3 else "")))

    def pick_cover(self):
        f = filedialog.askopenfilename(title="选择封面图片",
                                       filetypes=[("图片", "*.jpg *.jpeg *.png")])
        if f:
            self.cover_path.set(f)
            self.cover_mode.set("single")

    def pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    # ------------------------------------------------------------ 运行
    def log(self, msg):
        self.q.put(("log", msg))

    def start(self, dry):
        if self.worker and self.worker.is_alive():
            return
        mode = self.mode.get()
        if mode == "dir":
            root = Path(self.src_var.get()) if self.src_var.get() else None
            if not root or not root.is_dir():
                messagebox.showwarning("提示", "请先选择一个存在的文件夹")
                return
        else:
            if not self.files:
                messagebox.showwarning("提示", "请先选择文件")
                return

        opts = {
            "mode": mode,
            "root": Path(self.src_var.get()) if mode == "dir" else None,
            "files": list(self.files),
            "rename": self.v_rename.get(),
            "flatten": self.v_flatten.get(),
            "cover": self.v_cover.get(),
            "cover_mode": self.cover_mode.get(),
            "cover_path": self.cover_path.get(),
            "downmix": self.v_downmix.get(),
            "lyrics": self.v_lyrics.get(),
            "cleanup": self.v_cleanup.get(),
            "copy": self.v_copy.get(),
            "out": self.out_var.get(),
            "dry": dry,
            "ffmpeg": self.ff,
        }

        if not dry and opts["cleanup"]:
            if not messagebox.askyesno("确认删除",
                                       "「清理旧文件」会删除旧版本文件。\n"
                                       "工具已做覆盖校验（新集少于旧集则保留），\n"
                                       "但请确认已有备份。继续？"):
                return

        self.txt.delete("1.0", "end")
        self.stop_flag.clear()
        self.done = 0
        self.pbar.configure(value=0, maximum=100)
        self.btn_run.configure(state="disabled")
        self.btn_preview.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text=("预览中…" if dry else "执行中…"))

        self.worker = threading.Thread(target=self._run, args=(opts,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.status.configure(text="正在停止…")

    def _run(self, o):
        try:
            self.pipeline(o)
            self.q.put(("done", "预览完成（未修改任何文件）" if o["dry"] else "全部完成"))
        except Exception as exc:
            self.q.put(("log", "!! 异常: {}".format(exc)))
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("done", "执行中断：{}".format(exc)))

    # ------------------------------------------------------------ 处理流程
    def pipeline(self, o):
        """执行整条流水线。dry=True 时只打印计划，绝不改动文件。"""
        dry = o["dry"]
        tag = "[预览] " if dry else ""
        log = self.log
        root = o["root"]
        root = Path(root) if root else None

        def collect():
            if o["mode"] == "dir":
                return [p for p in root.rglob("*")
                        if p.is_file() and p.suffix.lower() in AUDIO_EXT
                        and DOWNMIX_DIRNAME not in p.parts]
            return [p for p in o["files"] if p.exists() and p.suffix.lower() in AUDIO_EXT]

        files = collect()
        self.total = max(1, len(files))
        self.q.put(("total", self.total))
        log("{}待处理文件：{} 个".format(tag, len(files)))
        if not files:
            return

        # ---------------- 1) 规范命名 ----------------
        if o["rename"]:
            log("{}—— 规范命名 ——".format(tag))
            seen, n, mapped = {}, 0, []
            for p in files:
                if self.stop_flag.is_set():
                    log("已停止")
                    return
                f = open_flac(p)
                if f is None:
                    mapped.append(p)
                    continue
                base, _is_cls = build_name(f, "auto")
                base = base or p.stem
                new = p.with_name(safe_name(base) + ".flac")
                if new == p:
                    mapped.append(p)
                    self._tick()
                    continue
                key = str(new).lower()
                if key in seen:
                    seen[key] += 1
                    new = new.with_name(new.stem + " ({})".format(seen[key]) + ".flac")
                else:
                    seen[key] = 1
                log("{}  {} -> {}".format(tag, p.name[:60], new.name[:70]))
                if dry:
                    mapped.append(p)
                else:
                    try:
                        if not new.exists():
                            p.rename(new)
                            n += 1
                        mapped.append(new if new.exists() else p)
                    except Exception as exc:
                        log("  失败: {}".format(exc))
                        mapped.append(p)
                self._tick()
            if not dry:
                files = mapped
            log("{}命名完成，实际改名 {} 个".format(tag, n))

        # ---------------- 2) 目录扁平化 ----------------
        if o["flatten"] and o["mode"] == "dir":
            log("{}—— 目录扁平化 ——".format(tag))
            moved = 0
            for p in list(files):
                if self.stop_flag.is_set():
                    log("已停止")
                    return
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                if len(rel.parts) < 2:
                    continue
                album = root / rel.parts[0]
                if p.parent == album or DOWNMIX_DIRNAME in p.parts:
                    continue
                dst = album / p.name
                log("{}  {}/{} -> {}/{}".format(tag, p.parent.name[:24], p.name[:36],
                                                album.name[:24], dst.name[:36]))
                if not dry:
                    try:
                        if not dst.exists():
                            shutil.move(str(p), str(dst))
                            moved += 1
                    except Exception as exc:
                        log("  失败: {}".format(exc))
                self._tick()
            if not dry:
                removed = 0
                for d in sorted(root.rglob("*"), key=lambda x: -len(str(x))):
                    if d.is_dir() and d.name != DOWNMIX_DIRNAME:
                        try:
                            if not any(d.iterdir()):
                                d.rmdir()
                                removed += 1
                        except OSError:
                            pass
                files = collect()
                log("{}扁平化完成，移动 {} 个，清理空目录 {} 个".format(tag, moved, removed))
            else:
                log("{}扁平化预览结束".format(tag))

        # ---------------- 3) 封面内嵌 ----------------
        if o["cover"]:
            log("{}—— 封面内嵌 ——".format(tag))
            try:
                import io
                from PIL import Image
                from mutagen.flac import Picture
            except ImportError:
                log("!! 缺少 Pillow，跳过封面")
            else:
                single = Path(o["cover_path"]) if (o["cover_mode"] == "single" and o["cover_path"]) else None
                if o["cover_mode"] == "single" and not single:
                    log("!! 选了统一图片却没指定文件，改为各目录 cover.jpg")
                ok = skip = fail = 0
                for p in files:
                    if self.stop_flag.is_set():
                        log("已停止")
                        return
                    f = open_flac(p)
                    if f is None:
                        fail += 1
                        self._tick()
                        continue
                    if f.pictures:
                        skip += 1
                        self._tick()
                        continue
                    img = single or (p.parent / "cover.jpg")
                    if not img or not img.is_file():
                        skip += 1
                        self._tick()
                        continue
                    try:
                        im = Image.open(img).convert("RGB")
                        if max(im.size) > 700:
                            im.thumbnail((700, 700), Image.LANCZOS)
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=86, optimize=True)
                        if not dry:
                            pic = Picture()
                            pic.type = 3
                            pic.mime = "image/jpeg"
                            pic.desc = "Cover"
                            pic.data = buf.getvalue()
                            f.add_picture(pic)
                            f.save()
                        ok += 1
                    except Exception as exc:
                        fail += 1
                        log("  封面失败 {}: {}".format(p.name[:40], exc))
                    self._tick()
                log("{}封面完成：写入 {}，跳过 {}，失败 {}".format(tag, ok, skip, fail))

        # ---------------- 4) 5.1 降混 ----------------
        if o["downmix"]:
            log("{}—— 5.1 → 立体声 ——".format(tag))
            if not o["ffmpeg"]:
                log("!! 未找到 ffmpeg，跳过降混")
            else:
                cnt = fail = 0
                for p in files:
                    if self.stop_flag.is_set():
                        log("已停止")
                        return
                    f = open_flac(p)
                    if f is None or f.info.channels <= 2:
                        continue
                    dst = p.parent / DOWNMIX_DIRNAME / p.name
                    log("{}  {}/{} → {}/".format(tag, p.parent.name[:24], p.name[:40], DOWNMIX_DIRNAME))
                    if dry:
                        cnt += 1
                    else:
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            r = subprocess.run([o["ffmpeg"], "-y", "-v", "error", "-i", str(p),
                                                "-filter_complex", PAN, "-map", "[a]",
                                                "-c:a", "flac", "-sample_fmt", "s32", str(dst)],
                                               capture_output=True, text=True, timeout=900)
                            if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                                cnt += 1
                                src_img = None
                                if o.get("cover"):
                                    single = Path(o["cover_path"]) if (
                                        o["cover_mode"] == "single" and o.get("cover_path")) else None
                                    cand = single or (p.parent / "cover.jpg")
                                    if cand and cand.is_file():
                                        src_img = cand
                                if src_img:
                                    try:
                                        self._embed_cover(dst, src_img)
                                    except Exception as exc:
                                        log("  降混补封面失败: {}".format(exc))
                            else:
                                fail += 1
                                log("  降混失败 {}: {}".format(p.name[:40], (r.stderr or "")[-120:]))
                        except Exception as exc:
                            fail += 1
                            log("  降混异常: {}".format(exc))
                    self._tick()
                log("{}降混完成 {} 个，失败 {} 个".format(tag, cnt, fail))

        # ---------------- 4.5) 歌词内嵌 ----------------
        if o.get("lyrics"):
            log("{}—— 歌词内嵌 ——".format(tag))
            got = have = miss = fail = 0
            for p in files:
                if self.stop_flag.is_set():
                    log("已停止")
                    return
                f = open_flac(p)
                if f is None:
                    fail += 1
                    self._tick()
                    continue
                if f.get("lyrics") or f.get("unsyncedlyrics"):
                    have += 1
                    self._tick()
                    continue
                artist = tag(f, "artist") or tag(f, "albumartist")
                title = tag(f, "title")
                album = tag(f, "album")
                if not title:
                    miss += 1
                    self._tick()
                    continue
                try:
                    cands = _lrclib_query(artist, title, album)
                    hit = _pick(cands, f.info.length if f.info.length else None)
                except Exception as exc:
                    fail += 1
                    log("  歌词查询异常: {}".format(exc))
                    self._tick()
                    continue
                if hit is None:
                    miss += 1
                    log("{}  未找到歌词: {}".format(tag, p.name[:52]))
                    self._tick()
                    continue
                synced = hit.get("syncedLyrics") or ""
                plain = hit.get("plainLyrics") or ""
                log("{}  {} <- {}".format(tag, p.name[:46],
                                          hit.get("trackName", "")[:34]))
                if not dry:
                    try:
                        if synced:
                            f["LYRICS"] = synced
                        else:
                            f["UNSYNCEDLYRICS"] = plain
                        f.save()
                        got += 1
                    except Exception as exc:
                        fail += 1
                        log("  写入失败: {}".format(exc))
                else:
                    got += 1
                self._tick()
            log("{}歌词完成：写入 {}，已有 {}，未找到 {}，失败 {}".format(
                tag, got, have, miss, fail))

        # ---------------- 5) 清理旧文件 ----------------
        if o["cleanup"] and o["mode"] == "dir":
            log("{}—— 清理旧文件 ——".format(tag))
            import re as _re

            def norm(s):
                s = s.lower()
                s = _re.sub(r"^\[[^\]]*\]", "", s)
                s = _re.sub(r"^cd\d+\s+", "", s)
                s = _re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
                return " ".join(s.split())

            new_dirs, old_dirs = {}, {}
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                fl = [q for q in d.rglob("*.flac") if DOWNMIX_DIRNAME not in q.parts]
                if not fl:
                    continue
                f0 = open_flac(fl[0])
                if f0 is None:
                    continue
                bucket = new_dirs if f0.info.sample_rate > 44100 else old_dirs
                bucket.setdefault(norm(d.name), []).append((d, len(fl)))

            def find_new(key):
                best, bl = None, 0
                for nk, items in new_dirs.items():
                    if nk.startswith(key) or key.startswith(nk):
                        ln = min(len(nk), len(key))
                        if ln > bl:
                            best, bl = items[0], ln
                return best

            gone = kept = 0
            for key, olds in old_dirs.items():
                hit = find_new(key)
                for od, _c in olds:
                    fl = [q for q in od.rglob("*.flac") if DOWNMIX_DIRNAME not in q.parts]
                    two = [q for q in fl if (open_flac(q) and open_flac(q).info.channels == 2)]
                    six = [q for q in fl if (open_flac(q) and open_flac(q).info.channels > 2)]
                    if hit is None:
                        log("{}  保留（无对应新目录）：{}".format(tag, od.name[:46]))
                        kept += len(two) + len(six)
                        continue
                    nd, ncnt = hit
                    mc = nd / DOWNMIX_DIRNAME
                    mcnt = len(list(mc.glob("*.flac"))) if mc.exists() else 0
                    targets = []
                    if ncnt >= len(two):
                        targets += two
                    else:
                        log("{}  保留（新集不足 {}<{}）：{}".format(tag, ncnt, len(two), od.name[:40]))
                        kept += len(two)
                    if six:
                        if mcnt >= len(six):
                            targets += six
                        else:
                            log("{}  保留（降混不足 {}<{}）：{}".format(tag, mcnt, len(six), od.name[:40]))
                            kept += len(six)
                    for q in targets:
                        log("{}  删除 {}".format(tag, q.relative_to(root)))
                        if dry:
                            gone += 1
                        else:
                            try:
                                q.unlink()
                                gone += 1
                            except Exception as exc:
                                log("  删除失败: {}".format(exc))
            log("{}清理完成：删除 {}，保留 {}（原因见上）".format(tag, gone, kept))

        # ---------------- 6) 复制到指定目录 ----------------
        if o["copy"] and o["out"]:
            log("{}—— 复制到指定目录 ——".format(tag))
            out = Path(o["out"])
            if not dry:
                files = collect()
            n = 0
            for p in files:
                if not p.exists():
                    continue
                try:
                    rel = p.relative_to(root) if (o["mode"] == "dir" and root) else Path(p.name)
                except ValueError:
                    rel = Path(p.name)
                dst = out / rel
                log("{}  {} -> {}".format(tag, p.name[:44], str(rel.parent)))
                if not dry:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if not dst.exists():
                            shutil.copy2(p, dst)
                            n += 1
                    except Exception as exc:
                        log("  复制失败: {}".format(exc))
                self._tick()
            log("{}复制完成 {} 个 -> {}".format(tag, n, out))

    def _embed_cover(self, flac_path, img_path, max_px=700, quality=86):
        """把图片写进指定 FLAC（已有封面则跳过）。"""
        import io
        from PIL import Image
        from mutagen.flac import Picture
        f = open_flac(flac_path)
        if f is None or f.pictures:
            return False
        im = Image.open(img_path).convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = buf.getvalue()
        f.add_picture(pic)
        f.save()
        return True

    def _tick(self):
        self.done += 1
        self.q.put(("tick", self.done))

    # ------------------------------------------------------------ 队列
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.txt.insert("end", payload + "\n")
                    self.txt.see("end")
                elif kind == "total":
                    self.pbar.configure(maximum=max(1, payload), value=0)
                elif kind == "tick":
                    self.pbar.configure(value=payload)
                    self.status.configure(text="进度 {}/{}".format(payload, int(self.pbar["maximum"])))
                elif kind == "tablescan":
                    done, total, rows = payload
                    if rows is not None:
                        self._fill_table(rows)
                    else:
                        self.summary.configure(text="扫描中… {}/{}".format(done, total))
                elif kind == "done":
                    self.status.configure(text=payload)
                    self.btn_run.configure(state="normal")
                    self.btn_preview.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.txt.insert("end", "\n==== {} ====\n".format(payload))
                    self.txt.see("end")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
