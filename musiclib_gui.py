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
import json
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

# 多源歌词（LRCLIB / 网易云 / QQ音乐 / Lyrics.ovh）
try:
    import lyrics_multi
except Exception:
    lyrics_multi = None

# 复用核心逻辑
try:
    from musiclib import (AUDIO_EXT, DOWNMIX_DIRNAME, PAN, build_name,
                          iter_flac, open_flac, safe_name, tag,
                          find_cover_image, has_cover, embed_cover, get_cover_bytes,
                          editable_fields, write_tag, read_tag, rename_file, wav_to_flac, split_cue_album, find_cue_audio, parse_cue, run_hidden,
                          build_name_by_template, template_vars_help,
                          DEFAULT_TEMPLATE_POP, DEFAULT_TEMPLATE_CLASSICAL,
                          _lrclib_query, _pick)
except Exception:  # 单文件打包后可能改名，退化为内置副本
    from musiclib_core import (AUDIO_EXT, DOWNMIX_DIRNAME, PAN, build_name,
                               iter_flac, open_flac, safe_name, tag,
                               find_cover_image, has_cover, embed_cover, get_cover_bytes,
                               editable_fields, write_tag, read_tag, rename_file,
                               build_name_by_template, template_vars_help,
                               DEFAULT_TEMPLATE_POP, DEFAULT_TEMPLATE_CLASSICAL,
                               _lrclib_query, _pick)

def _build_stamp():
    """把 exe 自身的编译时间塞进标题栏 —— 用户一眼就能确认跑的是不是新版。

    之前反复出现「我改了、用户说没变」，其实是他还开着旧 exe。
    标题里带时间，这种事一眼就能分辨。
    """
    try:
        import os
        import time as _t
        p = sys.executable if getattr(sys, "frozen", False) else __file__
        return _t.strftime("%m-%d %H:%M", _t.localtime(os.path.getmtime(p)))
    except Exception:
        return ""


_stamp = _build_stamp()
APP_TITLE = "musiclib 0.14 — 音乐库整理工具" + (
    "  [构建 {}]".format(_stamp) if _stamp else "")

# 表格列定义与索引（改动列顺序只需改这里）
# 处理过程中的半成品文件名：绝不能当成音频列进表格
# （踩过：列表里冒出「离开.mltmp」这种行，还会被后续操作当成真文件）
import re as _re
TMP_NAME_PAT = _re.compile(r"\.(mltmp|mbtmp|tagtmp|w2f|bak|tmp)(\.|$)", _re.I)


def _is_temp_name(p):
    return bool(TMP_NAME_PAT.search(p.name))


COLS = ("chk", "title", "artist", "album", "fmt", "dur", "sr", "cover", "lyrics", "size")
I_CHK = COLS.index("chk")        # 0 勾选列
I_COVER = COLS.index("cover")    # 7
I_LYRIC = COLS.index("lyrics")   # 8
I_TITLE = COLS.index("title")
I_ARTIST = COLS.index("artist")
I_ALBUM = COLS.index("album")
CHECK_ON = "☑"
CHECK_OFF = "☐"
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


STATE_FILE = None      # 运行时确定（app_dir/state.json）
CACHE_FILE = None      # 清单缓存（app_dir/list_cache.json）


def _state_paths():
    """状态文件放在 exe 同级，跟着绿色包走；不可写时退回用户目录。"""
    global STATE_FILE, CACHE_FILE
    if STATE_FILE is None:
        d = app_dir()
        try:
            t = d / ".wtest"
            t.write_text("x", encoding="utf-8")
            t.unlink()
        except Exception:
            d = Path.home() / ".musiclib"
            d.mkdir(parents=True, exist_ok=True)
        STATE_FILE = d / "state.json"
        CACHE_FILE = d / "list_cache.json"
    return STATE_FILE, CACHE_FILE


def find_ffmpeg():
    for cand in (app_dir() / "bin" / "ffmpeg.exe", app_dir() / "ffmpeg.exe"):
        if cand.is_file():
            return str(cand)
    return shutil.which("ffmpeg") or ""


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        # 缩略图单独一条队列：它是重活，混在主队列里会把进度/日志挤到后面
        self.q_thumb = queue.Queue()
        # 后台线程要主线程干活（弹窗等）时的请求队列：
        # root.after() 只能在主线程调，从后台线程调会抛
        # "main thread is not in main loop"（实测踩过）。
        self._ui_requests = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.total = 0
        self.done = 0
        self.logfile = None
        self.logpath = None

        root.title(APP_TITLE)
        root.geometry("1020x720")
        root.minsize(880, 620)
        root.configure(bg=COLOR_BG)
        try:
            root.iconbitmap(default="")
        except Exception:
            pass

        self._build_ui()
        self._poll_job = self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._save_job = None
        self._restore_job = self.root.after(300, self._restore_state)   # 界面画完再恢复，避免卡启动
        self._load_split_done()
        self.root.after(400, self._bind_autosave)   # 与恢复解耦：首启没有 state.json 也要开自动保存

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
        self.v_wav2flac = tk.BooleanVar(value=False)
        self.v_cuesplit = tk.BooleanVar(value=False)   # 整轨按 CUE 分割   # WAV → FLAC（默认关，转换会改文件）
        self.v_cleanup = tk.BooleanVar(value=False)
        self.v_copy = tk.BooleanVar(value=False)

        ttk.Checkbutton(f2, text="规范命名（按下面的模板生成文件名，不加序号）",
                        variable=self.v_rename).grid(row=0, column=0, sticky="w", padx=8, pady=2)

        # 命名模板：默认保持「标题 - 演出者 - 专辑」，用户可改
        ttk.Label(f2, text="模板：").grid(row=0, column=1, sticky="e")
        self.name_tpl = tk.StringVar(value=DEFAULT_TEMPLATE_POP)
        ent_tpl = ttk.Entry(f2, textvariable=self.name_tpl, width=34)
        ent_tpl.grid(row=0, column=2, sticky="w", padx=2)
        tpl_btn = ttk.Menubutton(f2, text="预设 ▾", width=8)
        tpl_menu = tk.Menu(tpl_btn, tearoff=0)
        for label, tpl in (
            ("标题 - 演出者 - 专辑（默认）", DEFAULT_TEMPLATE_POP),
            ("演出者 - 标题", "{演出者} - {标题}"),
            ("标题 - 作曲家（古典）", DEFAULT_TEMPLATE_CLASSICAL),
            ("音轨号. 标题", "{音轨号}. {标题}"),
            ("音轨号. 标题 - 演出者", "{音轨号}. {标题} - {演出者}"),
            ("演出者 - 专辑 - 音轨号. 标题", "{演出者} - {专辑} - {音轨号}. {标题}"),
            ("标题（只留标题）", "{标题}"),
            ("原名（不改名）", "{原名}"),
        ):
            tpl_menu.add_command(label=label,
                                 command=lambda t=tpl: self.name_tpl.set(t))
        tpl_btn["menu"] = tpl_menu
        tpl_btn.grid(row=0, column=3, sticky="w", padx=2)
        ttk.Button(f2, text="变量…", width=6,
                   command=self.show_template_vars).grid(row=0, column=4, sticky="w", padx=2)

        ttk.Checkbutton(f2, text="目录扁平化（把子目录里的音频提到专辑第一层，并清空目录）",
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

        ttk.Checkbutton(f2, text="WAV → FLAC（逐字节无损，保留标签与封面；转完可选删原 WAV）",
                        variable=self.v_wav2flac).grid(row=6, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="整轨按 CUE 分割成单曲（无损；只处理「单文件 cue + 整轨」的专辑，分轨专辑不动）",
                        variable=self.v_cuesplit).grid(row=7, column=0, columnspan=4, sticky="w", padx=8, pady=2)
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
        self.btn_refresh = ttk.Button(bar, text="刷新清单", command=self.scan_table)
        self.btn_refresh.pack(side="left", padx=4)
        self.v_syncname = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="改标题时同步改文件名", variable=self.v_syncname).pack(
            side="left", padx=(10, 4))
        chk_btn = ttk.Menubutton(bar, text="勾选 ▾", width=8)
        chk_menu = tk.Menu(chk_btn, tearoff=0)
        chk_menu.add_command(label="全选", command=self.check_all)
        chk_menu.add_command(label="全不选", command=self.check_none)
        chk_menu.add_command(label="反选", command=self.check_invert)
        chk_menu.add_command(label="只勾缺项（缺封面或缺歌词）", command=self.check_missing)
        chk_menu.add_separator()
        chk_menu.add_command(label="把表格里高亮选中的行勾上", command=self.check_selection)
        chk_btn["menu"] = chk_menu
        chk_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="只看缺项", command=self.filter_missing).pack(side="left", padx=4)
        ttk.Button(bar, text="显示全部", command=self.filter_all).pack(side="left", padx=4)
        ttk.Button(bar, text="编辑选中", command=self.edit_selected).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="批量改标签", command=self.batch_edit_tags).pack(side="left", padx=2)
        ttk.Button(bar, text="批量设封面", command=self.bulk_set_cover).pack(side="left", padx=4)
        ttk.Button(bar, text="按模板改名(勾选)", command=self.rename_checked).pack(side="left", padx=4)
        ttk.Button(bar, text="粘贴曲目清单", command=self.paste_track_list).pack(side="left", padx=4)
        self.summary = ttk.Label(bar, text="未扫描")
        self.summary.pack(side="left", padx=12)

        cols = COLS
        heads = {"chk": ("选", 30),
                 "title": ("歌曲标题", 205), "artist": ("艺术家", 125), "album": ("唱片标题", 215),
                 "fmt": ("格式", 48), "dur": ("时长", 58), "sr": ("规格", 78),
                 "cover": ("封面", 48), "lyrics": ("歌词", 42), "size": ("大小", 58)}
        tf = ttk.Frame(tab1)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=14,
                                 selectmode="extended")
        self.tree.heading("#0", text="封面图")
        self.tree.column("#0", width=60, minwidth=60, stretch=False, anchor="center")
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
        self.tree.tag_configure("split", background="#E3F2FD")   # 已切分未删原件
        self.tree.tag_configure("ok", background="#EAF7EA")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_cell_dbl)
        self.tree.bind("<Motion>", self._on_hover)
        self.tree.bind("<Leave>", self._hide_preview)
        self.tree.bind("<Button-3>", self._on_right_click)
        # tkinter 的 Treeview 默认没有 Ctrl+A，必须自己绑
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)
        self.root.bind("<Control-a>", self._select_all_rows)
        self.root.bind("<Control-A>", self._select_all_rows)

        # 缩略图 / 大图缓存：必须持有引用，否则被 GC 回收后格子变空白
        self._thumbs = {}
        self._bigpics = {}
        self._row_pics = {}
        self._preview_win = None
        self._preview_iid = None
        self._check_state = {}       # 路径 -> 是否勾选（刷新表格时据此恢复）
        self._edit_entry = None      # 就地编辑用的 Entry
        self._edit_ctx = None        # 就地编辑上下文（iid/idx/cname/path/cur）

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

        # ---- 页签三：歌词工具 ----
        self._build_lyric_tab(nb)

    # ------------------------------------------------------------ 歌词工具页签
    def _build_lyric_tab(self, nb):
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text="歌词工具")

        # 源
        lf1 = ttk.LabelFrame(tab3, text=" 歌词来源（LRC / SRT / ASS / SSA / SMI / KSC） ")
        lf1.pack(fill="x", padx=10, pady=6)
        row = ttk.Frame(lf1)
        row.pack(fill="x", padx=8, pady=6)
        self.ly_src = tk.StringVar()
        ttk.Entry(row, textvariable=self.ly_src, width=64).pack(side="left", padx=4)
        ttk.Button(row, text="选文件…", width=9,
                   command=self._ly_pick_file).pack(side="left", padx=3)
        ttk.Button(row, text="选目录…", width=9,
                   command=self._ly_pick_dir).pack(side="left", padx=3)

        # 转换选项
        lf2 = ttk.LabelFrame(tab3, text=" 处理与输出 ")
        lf2.pack(fill="x", padx=10, pady=6)

        r1 = ttk.Frame(lf2); r1.pack(fill="x", padx=8, pady=3)
        ttk.Label(r1, text="输出格式：").pack(side="left")
        self.ly_lrc = tk.BooleanVar(value=True)
        self.ly_srt = tk.BooleanVar(value=True)
        self.ly_ass = tk.BooleanVar(value=False)
        self.ly_txt = tk.BooleanVar(value=False)
        for txt, var in (("LRC", self.ly_lrc), ("SRT", self.ly_srt),
                         ("ASS", self.ly_ass), ("TXT", self.ly_txt)):
            ttk.Checkbutton(r1, text=txt, variable=var).pack(side="left", padx=3)
        ttk.Label(r1, text="　时间偏移(秒)：").pack(side="left")
        self.ly_offset = tk.StringVar(value="0")
        ttk.Entry(r1, textvariable=self.ly_offset, width=7).pack(side="left")

        r2 = ttk.Frame(lf2); r2.pack(fill="x", padx=8, pady=3)
        ttk.Label(r2, text="简繁：").pack(side="left")
        self.ly_zh = tk.StringVar(value="keep")
        for txt, val in (("不变", "keep"), ("转简体", "hans"), ("转繁体", "hant")):
            ttk.Radiobutton(r2, text=txt, variable=self.ly_zh,
                            value=val).pack(side="left", padx=2)
        ttk.Label(r2, text="　拼音：").pack(side="left")
        self.ly_py = tk.StringVar(value="")
        for txt, val in (("不加", ""), ("另行", "both"), ("同行", "append"), ("只留拼音", "only")):
            ttk.Radiobutton(r2, text=txt, variable=self.ly_py,
                            value=val).pack(side="left", padx=2)

        r3 = ttk.Frame(lf2); r3.pack(fill="x", padx=8, pady=3)
        self.ly_ads = tk.BooleanVar(value=True)
        self.ly_split = tk.BooleanVar(value=False)
        self.ly_merge = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="删除广告/网址行", variable=self.ly_ads).pack(side="left", padx=3)
        ttk.Checkbutton(r3, text="长句拆分", variable=self.ly_split).pack(side="left", padx=3)
        ttk.Checkbutton(r3, text="短句合并", variable=self.ly_merge).pack(side="left", padx=3)
        ttk.Label(r3, text="　输出目录(留空=原目录)：").pack(side="left")
        self.ly_out = tk.StringVar()
        ttk.Entry(r3, textvariable=self.ly_out, width=30).pack(side="left", padx=3)
        ttk.Button(r3, text="选…", width=5, command=self._ly_pick_out).pack(side="left")

        r4 = ttk.Frame(lf2); r4.pack(fill="x", padx=8, pady=6)
        ttk.Button(r4, text="预览（不写文件）", width=16,
                   command=lambda: self._ly_run(True)).pack(side="left", padx=4)
        ttk.Button(r4, text="开始转换", width=12,
                   command=lambda: self._ly_run(False)).pack(side="left", padx=4)
        self.ly_status = ttk.Label(r4, text="", foreground="#0a6")
        self.ly_status.pack(side="left", padx=10)

        ttk.Label(tab3, text="说明：编码自动识别（UTF-8/UTF-16/GBK/Big5）；"
                             "QRC/QLY/KRC 为加密格式，本工具不处理。",
                  foreground="#666").pack(anchor="w", padx=14, pady=(0, 6))

    def _ly_pick_file(self):
        fs = filedialog.askopenfilenames(
            title="选择歌词文件",
            filetypes=[("歌词", "*.lrc *.srt *.ass *.ssa *.smi *.ksc *.txt"),
                       ("全部文件", "*.*")])
        if fs:
            self.ly_src.set(";".join(fs))

    def _ly_pick_dir(self):
        d = filedialog.askdirectory(title="选择含歌词的目录")
        if d:
            self.ly_src.set(d)

    def _ly_pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.ly_out.set(d)

    def _ly_run(self, dry):
        raw = self.ly_src.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择歌词文件或目录")
            return
        targets = raw.split(";")
        fmts = [n for n, v in (("lrc", self.ly_lrc), ("srt", self.ly_srt),
                               ("ass", self.ly_ass), ("txt", self.ly_txt)) if v.get()]
        if not fmts:
            messagebox.showwarning("提示", "至少选一种输出格式")
            return
        try:
            offset = float(self.ly_offset.get() or 0)
        except ValueError:
            messagebox.showwarning("提示", "时间偏移要填数字，例如 0.5 或 -1.2")
            return

        opts = dict(formats=fmts, offset=offset,
                    strip_ads=self.ly_ads.get(),
                    hans=self.ly_zh.get() == "hans",
                    hant=self.ly_zh.get() == "hant",
                    pinyin=(self.ly_py.get() or None),
                    do_split=self.ly_split.get(), do_merge=self.ly_merge.get(),
                    out=self.ly_out.get().strip() or None, dry=dry)

        self.ly_status.configure(text="处理中…", foreground="#c60")
        threading.Thread(target=self._ly_worker, args=(targets, opts), daemon=True).start()

    def _ly_worker(self, targets, o):
        try:
            import lyriceng
            files = []
            for t in targets:
                p = Path(t)
                if p.is_file():
                    files.append(p)
                elif p.is_dir():
                    files += [q for q in p.rglob("*")
                              if q.is_file() and q.suffix.lower() in
                              (".lrc", ".srt", ".ass", ".ssa", ".smi", ".ksc")]
            if not files:
                self.q.put(("log", "[歌词] 没有找到歌词文件"))
                self.q.put(("lyric", "没有找到歌词文件"))
                return
            outdir = Path(o["out"]) if o["out"] else None
            if outdir:
                outdir.mkdir(parents=True, exist_ok=True)
                files = [p for p in files
                         if not str(p.resolve()).startswith(str(outdir.resolve()))]
            self.q.put(("log", "[歌词] 共 {} 个文件，输出 {}".format(
                len(files), "/".join(o["formats"]))))
            ok = bad = 0
            for p in files:
                try:
                    text, enc = lyriceng.read_lyric(p, None)
                except Exception as exc:
                    bad += 1
                    self.q.put(("log", "[歌词] 读取失败 {}: {}".format(p.name, exc)))
                    continue
                src_fmt = p.suffix.lower().lstrip(".") or "auto"
                if src_fmt == "ssa":
                    src_fmt = "ass"
                ly = lyriceng.process(
                    text, src_fmt=src_fmt, offset=o["offset"],
                    do_strip_ads=o["strip_ads"], hans=o["hans"], hant=o["hant"],
                    pinyin=o["pinyin"], do_split=o["do_split"], do_merge=o["do_merge"])
                if not ly.lines:
                    bad += 1
                    self.q.put(("log", "[歌词] 无可解析行 {}（编码 {}）".format(p.name, enc)))
                    continue
                for fmt in o["formats"]:
                    body = lyriceng.render(ly, fmt, title=p.stem)
                    dst = (outdir or p.parent) / (p.stem + "." + fmt)
                    self.q.put(("log", "[歌词] {} -> {}  {} 行{}".format(
                        p.name[:30], dst.name, len(ly.lines), "" if not o["dry"] else "（预览）")))
                    if not o["dry"]:
                        dst.write_text(body, encoding="utf-8")
                ok += 1
            msg = "[歌词] {} {} 个，失败 {}".format(
                "预览完成" if o["dry"] else "转换完成", ok, bad)
            self.q.put(("log", msg))
            self.q.put(("lyric", msg))
        except Exception as exc:
            self.q.put(("log", "[歌词] 异常: {}".format(exc)))
            self.q.put(("lyric", "出错: {}".format(exc)))

    def _toggle_mode(self):
        folder = self.mode.get() == "dir"
        self.btn_dir.configure(state="normal" if folder else "disabled")
        self.btn_files.configure(state="disabled" if folder else "normal")

    def _toggle_copy(self):
        st = "normal" if self.v_copy.get() else "disabled"
        self.ent_out.configure(state=st)
        self.btn_out.configure(state=st)

    def show_template_vars(self):
        vars_lines = template_vars_help().replace("、", "\n")
        messagebox.showinfo(
            "命名模板变量",
            "在模板里写 <变量> 或 {变量} 都行，例如：\n\n"
            "    {演出者} - {标题}\n"
            "    {音轨号}. {标题}\n"
            "    {标题} - {作曲家}\n\n"
            "可用变量：\n" + vars_lines + "\n\n"
            "说明：\n"
            "· 某个变量没有值 → 连同它旁边的分隔符一起省掉，不会留下「 - - 」\n"
            "· 音轨号补成两位（01、02），便于排序\n"
            "· 年份只取前 4 位\n"
            "· 文件名里不许出现的字符自动换成 -\n"
            "· 模板留空 → 普通用「标题 - 演出者 - 专辑」，古典用「标题 - 作曲家」")

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
        try:
            self._sync_check_state()     # 刷新前先记住当前勾选
            self.btn_refresh.configure(state="disabled")
        except Exception:
            pass
        files = self._source_files()
        if not files:
            messagebox.showwarning("提示", "请先选择文件夹或文件")
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.summary.configure(text="扫描中… 0/{}".format(len(files)))
        threading.Thread(target=self._scan_worker, args=(files,), daemon=True).start()

    @staticmethod
    def _pic_b64(data, size):
        """把封面原始字节做成 size×size 的 PNG base64；失败返回空串。"""
        try:
            from PIL import Image
            import io, base64
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((size - 4, size - 4), Image.LANCZOS)
            canvas = Image.new("RGB", (size, size), (246, 247, 249))
            canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return ""

    @staticmethod
    def _pic_b64_pair(data, small_sz=52, big_sz=260):
        """一次解码，出 (small_b64, big_b64)。

        原来对同一张图调了两次 _pic_b64，等于解码两次；
        3706 个文件就是 7412 次 PIL 解码，这是扫描慢的主因之一。
        """
        try:
            from PIL import Image
            import io
            import base64
            base = Image.open(io.BytesIO(data)).convert("RGB")
            out = []
            for size in (small_sz, big_sz):
                t = base.copy()
                t.thumbnail((size - 4, size - 4), Image.LANCZOS)
                canvas = Image.new("RGB", (size, size), (246, 247, 249))
                canvas.paste(t, ((size - t.width) // 2, (size - t.height) // 2))
                buf = io.BytesIO()
                canvas.save(buf, format="PNG")
                out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
            return out[0], out[1]
        except Exception:
            return "", ""

    def _scan_worker(self, files):
        rows = []
        files = [p for p in files if not _is_temp_name(p)]
        for i, p in enumerate(files, 1):
            title = artist = album = ""
            fmt = p.suffix.lower().lstrip(".")
            dur = sr = "-"
            has_cov = has_lyric = False
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
                has_cov = bool(f.pictures)
                has_lyric = bool(f.get("lyrics") or f.get("unsyncedlyrics"))
            else:
                # 非 FLAC（mp3 / wav / dsf）也读标签与封面
                title = read_tag(p, "title") or p.stem
                artist = read_tag(p, "artist") or read_tag(p, "albumartist")
                album = read_tag(p, "album")
                try:
                    hc = has_cover(p)   # 这里调的是模块函数，别被同名局部变量遮蔽
                    has_cov = bool(hc) if hc is not None else False
                except Exception as exc:
                    has_cov = False
                    self.q.put(("log", "[扫描] {} 判封面失败：{}".format(p.name[:40], exc)))
                has_lyric = False
                fmt = p.suffix.lower().lstrip(".") if editable_fields(p) else fmt + "?"
            # 封面缩略图（后台线程里用 PIL 转好，主线程只负责建 PhotoImage）
            # 缩略图一个都不在这里做：实测 3759 张要 157 秒，
            # 全塞进扫描阶段就是"点一下卡两分半"。
            # 这里只读标签（3770 个文件 1.14 秒），缩略图交给后台线程逐张补。
            small = big = ""
            # 注意：values 的元素个数必须与 COLS 完全一致（现在是 10 个，
            # 第 0 个是勾选列）。少一个会导致整行左移、封面列显示成歌词值。
            vals = [CHECK_ON, title, artist, album, fmt, dur, sr,
                    "✓" if has_cov else "✗",
                    "✓" if has_lyric else "✗",
                    human(size) if isinstance(size, int) else "-"]
            assert len(vals) == len(COLS), "列数与 COLS 不一致: {} vs {}".format(len(vals), len(COLS))
            rows.append((str(p), tuple(vals), small, big))
            if i % 40 == 0:
                self.q.put(("tablescan", (i, len(files), None)))
        self.q.put(("tablescan", (len(files), len(files), rows)))

    def _fill_table(self, rows):
        """分批插入：每批 150 行就让出一次事件循环，避免窗口"未响应"。

        原来是 for 循环一次性 tree.insert 三千多行，主线程全程占死。
        """
        self.row_paths = {}
        self._thumbs.clear()
        self._bigpics.clear()
        self._pending_fill = list(rows)
        self._fill_total = len(rows)
        self._fill_pos = 0
        self._fill_t0 = time.time()
        try:
            self.btn_refresh.configure(state="disabled", text="填充中…")
        except Exception:
            pass
        self.root.after(1, self._fill_batch)

    def _fill_batch(self, chunk=150):
        pend = getattr(self, "_pending_fill", None)
        if pend is None:
            return
        took = 0
        while pend and took < chunk:
            path, r, small, big = pend.pop(0)
            took += 1
            self._fill_pos += 1
            r = list(r)
            if len(r) < len(COLS):
                r += [""] * (len(COLS) - len(r))
            elif len(r) > len(COLS):
                r = r[:len(COLS)]
            if len(r) > I_CHK:
                r[I_CHK] = CHECK_ON if self._check_state.get(path, True) else CHECK_OFF
            r = tuple(r)
            miss = ("✗" in (r[I_COVER], r[I_LYRIC]))
            if str(Path(path).resolve()) in getattr(self, "_split_done", set()):
                # 已切分但原文件还在：标蓝，并在标题前加记号
                r = list(r)
                if not str(r[I_TITLE]).startswith("[已切分]"):
                    r[I_TITLE] = "[已切分] " + str(r[I_TITLE])
                r = tuple(r)
                tags = ("split",)
            else:
                tags = ("miss",) if miss else ("ok",)
            iid = None
            if small:
                try:
                    ph = tk.PhotoImage(data=small)
                except Exception:
                    ph = None
                if ph is not None:
                    try:
                        ph._b64 = small
                    except Exception:
                        pass
                    iid = self.tree.insert("", "end", image=ph, values=r, tags=tags)
                    self._thumbs[iid] = ph
                    if big:
                        try:
                            self._bigpics[iid] = tk.PhotoImage(data=big)
                        except Exception:
                            pass
            if iid is None:
                iid = self.tree.insert("", "end", text="✗", values=r, tags=tags)
            self.row_paths[iid] = path

        if pend:
            # 还有剩余：更新一下状态栏，下一轮继续
            try:
                self.status.configure(
                    text="填充清单 {}/{}…".format(self._fill_pos, self._fill_total))
            except Exception:
                pass
            self.root.after(1, self._fill_batch)
            return

        # 全部插完
        self._pending_fill = None
        total = self._fill_total
        no_cover = no_lyric = 0
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if len(v) > I_COVER and v[I_COVER] == "✗":
                no_cover += 1
            if len(v) > I_LYRIC and v[I_LYRIC] == "✗":
                no_lyric += 1
        self._update_summary(total, no_cover, no_lyric)
        self._sync_check_state()
        self._save_state()
        try:
            self.btn_refresh.configure(state="normal", text="刷新清单")
        except Exception:
            pass  # allow-swallow: 按钮已销毁时恢复状态无意义
        el = time.time() - self._fill_t0
        self.status.configure(
            text="清单 {} 行（{:.1f} 秒），缩略图后台生成中…".format(total, el))
        self.log("[清单] {} 行，{} 秒；缩略图后台生成中".format(total, round(el, 1)))
        self._start_thumb_worker()

    # ---------------------------------------------------- 缩略图后台补
    def _start_thumb_worker(self):
        """对缺缩略图的行起后台线程逐张生成，生成一张回主线程挂一张。"""
        if getattr(self, "_thumb_job", None):
            return
        items = []
        for iid, path in self.row_paths.items():
            if iid in self._thumbs:
                continue
            items.append((iid, path))
        if not items:
            return
        self._thumb_stop = False
        self._thumb_total = len(items)
        self._thumb_done = 0

        # 4 线程并行：PIL 解码会释放 GIL，实测能把 3181 行的补图从 ~30 秒压到 ~8 秒
        from concurrent.futures import ThreadPoolExecutor

        def one(item):
            iid, path = item
            if getattr(self, "_thumb_stop", False):
                return
            try:
                raw = get_cover_bytes(Path(path))
                if raw:
                    small, _ = self._pic_b64_pair(raw, 52, 52)
                    if small:
                        self.q_thumb.put((iid, small))
            except Exception:
                pass  # allow-swallow: 缩略图生成失败不影响其它行
            self._thumb_done += 1

        def work():
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(one, items))
            self.q.put(("thumb_done", None))

        t = threading.Thread(target=work, daemon=True, name="thumbfill")
        self._thumb_job = t
        t.start()


    # ---------------------------------------------------- 分轨后的收尾清理
    @staticmethod
    def _cue_sidecars(cue_path, audio):
        """列出「这张整轨切完后不再需要」的文件。

        保留：切出来的单曲、封面图（cover/folder/front）、Artwork 等扫描目录、
              以及 Info.txt 这类可能还有用的说明。
        清理：原整轨音频、cue 本身、抓轨日志/校验文件、封底/碟面图。
        """
        d = Path(cue_path).parent
        keep_cover = ("cover", "folder", "front", "albumart", "封面")
        out = [Path(audio), Path(cue_path)]
        for pat in ("*.log", "LOG.*", "*.m3u", "*.m3u8", "*.sfv", "*.md5", "list*.txt",
                    "Thumbs.db", "desktop.ini", "*.nfo"):
            try:
                out += [q for q in d.glob(pat) if q.is_file()]
            except OSError:
                pass
        for pat in ("back*.*", "disc*.*", "cd*.*", "inlay*.*", "tray*.*", "matrix*.*"):
            try:
                for q in d.glob(pat):
                    if not q.is_file():
                        continue
                    if q.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"):
                        continue
                    if any(k in q.stem.lower() for k in keep_cover):
                        continue
                    out.append(q)
            except OSError:
                pass
        # 去重、只留真实存在的文件
        seen, uniq = set(), []
        for q in out:
            try:
                r = q.resolve()
            except OSError:
                continue
            if r in seen or not q.is_file():
                continue
            seen.add(r)
            uniq.append(q)
        return uniq

    # ---------------------------------------------------- 已切分但未删原件
    def _mark_split(self, path):
        """记下「这张整轨已经切过了，但原文件还在」。

        用户要求：不删也要标出来，否则分不清哪些还没切、哪些切了没清。
        """
        try:
            self._split_done.add(str(Path(path).resolve()))
            self._save_split_done()
        except Exception:
            pass  # allow-swallow: 记录失败不影响切分结果

    def _load_split_done(self):
        import json
        self._split_done = set()
        try:
            f = app_dir() / "split_done.json"
            if f.is_file():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._split_done = set(data)
        except Exception:
            pass  # allow-swallow: 文件坏了当空集合，不影响启动

    def _save_split_done(self):
        import json
        try:
            (app_dir() / "split_done.json").write_text(
                json.dumps(sorted(self._split_done), ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass  # allow-swallow: 写不了只影响下次启动的标记

    def _clean_orphan_temps(self):
        """启动时清掉上次中断留下的半成品文件。

        规则（安全优先）：
          · 临时文件 + 原文件都在 → 原文件完整，删临时；
          · 只有临时、原文件不见了 → 保留，只在日志里警告（可能是唯一的数据）。
        踩过：进程被强杀时 finally 不执行，.tagtmp/.mltmp 就留在专辑目录里，
        还会让「同目录有几个音频」的判断误把整轨当成已分轨。
        """
        try:
            root = Path(self.src_var.get()) if self.src_var.get() else None
        except Exception:
            root = None
        if not root or not root.is_dir():
            return
        removed = kept = 0
        try:
            for f in root.rglob("*"):
                if not f.is_file() or not _is_temp_name(f):
                    continue
                # 去掉临时后缀得到原文件
                base = Path(_re.sub(r"\.(mltmp|mbtmp|tagtmp|w2f|bak|tmp)(\.[^.]+)?$", r"\2", f.name))
                orig = f.with_name(base.name)
                if orig.exists() and orig.stat().st_size > 0:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
                else:
                    kept += 1
                    self.log("[残留] {} 没有对应的原文件，保留（请人工确认）".format(f.name[:60]))
        except Exception:
            pass  # allow-swallow: 清理失败不影响启动
        if removed or kept:
            self.log("[残留] 清理 {} 个半成品文件，保留 {} 个（无原文件的）".format(removed, kept))

    def _bind_autosave(self):
        """任何选项/路径变化都自动落盘（防抖 0.8 秒），不必等关窗口。

        之前只在 on_close 保存，用户用任务管理器结束进程或直接重启电脑时
        上次的设置就丢了 —— 表现为「重启后什么都没恢复」。
        """
        watched = [self.mode, self.src_var, self.name_tpl, self.cover_mode,
                   self.cover_path, self.out_var,
                   self.v_rename, self.v_flatten, self.v_cover,
                   self.v_downmix, self.v_lyrics, self.v_cleanup, self.v_copy, self.v_wav2flac, self.v_cuesplit]
        if hasattr(self, "v_syncname"):
            watched.append(self.v_syncname)
        for var in watched:
            try:
                var.trace_add("write", lambda *a: self._save_state())
            except Exception:
                pass  # allow-swallow: 个别变量不支持 trace 时跳过，不影响其它项
        self.log("[状态] 已开启自动保存：改完选项 0.8 秒内落盘")

    def _sync_check_state(self):
        """把树里的 ☑/☐ 写回 _check_state，刷新表格时才能恢复。"""
        for iid in self.tree.get_children():
            p = self._row_path(iid)
            if p is None:
                continue
            v = self.tree.item(iid, "values")
            if v and len(v) > I_CHK:
                self._check_state[str(p)] = (v[I_CHK] == CHECK_ON)

    def _auto_refresh_table(self):
        """预览/执行结束后自动重扫表格；勾选状态由 _check_state 保留。"""
        self._hide_preview()
        try:
            if not self._source_files():
                return
            self.scan_table()
        except Exception:
            pass

    def _update_summary(self, total=None, no_cover=None, no_lyric=None):
        children = self.tree.get_children()
        if total is None:
            total = len(children)
        if no_cover is None or no_lyric is None:
            nc = nl = 0
            for iid in children:
                v = self.tree.item(iid, "values")
                if v and len(v) > I_LYRIC:
                    if v[I_COVER] == "✗":
                        nc += 1
                    if v[I_LYRIC] == "✗":
                        nl += 1
            no_cover, no_lyric = nc, nl
        checked = sum(1 for iid in children
                      if len(self.tree.item(iid, "values")) > I_CHK
                      and self.tree.item(iid, "values")[I_CHK] == CHECK_ON)
        self.summary.configure(
            text="共 {} 个 · 已勾选 {} · 缺封面 {} · 缺歌词 {}　（点第一列勾选，只勾的才生效）".format(
                total, checked, no_cover, no_lyric))

    # ------------------------------------------------------------ 勾选
    def _checked_paths(self):
        out = []
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if v and len(v) > I_CHK and v[I_CHK] == CHECK_ON:
                p = self._row_path(iid)
                if p is not None:
                    out.append(p)
        return out

    def _toggle_check(self, iid):
        vals = list(self.tree.item(iid, "values"))
        if len(vals) <= I_CHK:
            return
        vals[I_CHK] = CHECK_OFF if vals[I_CHK] == CHECK_ON else CHECK_ON
        self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self._save_state()

    def _on_tree_click(self, event):
        """单击勾选列切换选中。

        命中判断统一走「region 允许 cell/tree + 用列号定位」：
        Tk 对 #0（图列）返回 "tree"，严格只判 "cell" 会漏；这里勾选列是
        数据列（#1）虽返回 "cell"，但保持一致写法，避免以后挪列又踩坑。
        """
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("cell", "tree"):
            return
        if self.tree.identify_column(event.x) != "#{}".format(I_CHK + 1):
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_check(iid)

    def check_all(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if len(vals) > I_CHK:
                vals[I_CHK] = CHECK_ON
                self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self.status.configure(text="已全选")

    def check_none(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if len(vals) > I_CHK:
                vals[I_CHK] = CHECK_OFF
                self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self.status.configure(text="已全部取消勾选")

    def check_invert(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if len(vals) > I_CHK:
                vals[I_CHK] = CHECK_OFF if vals[I_CHK] == CHECK_ON else CHECK_ON
                self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self.status.configure(text="已反选")

    def check_missing(self):
        """只勾选缺封面或缺歌词的。"""
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if len(vals) > I_CHK:
                need = (vals[I_COVER] == "✗") or (vals[I_LYRIC] == "✗")
                vals[I_CHK] = CHECK_ON if need else CHECK_OFF
                self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self.status.configure(text="只勾选了缺项")

    def uncheck_selection(self):
        """取消勾选当前高亮的行。"""
        n = 0
        for iid in self.tree.selection():
            p = self.row_paths.get(iid)
            if not p:
                continue
            self._check_state[p] = False
            self.tree.set(iid, I_CHK, CHECK_OFF)
            n += 1
        self._sync_check_state()
        self.status.configure(text="已取消勾选 {} 行".format(n))

    def check_selection(self):
        """把表格里高亮选中的行勾上（其余保持不变）。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在表格里选中几行（Ctrl / Shift 多选）")
            return
        for iid in sel:
            vals = list(self.tree.item(iid, "values"))
            if len(vals) > I_CHK:
                vals[I_CHK] = CHECK_ON
                self.tree.item(iid, values=vals)
        self._sync_check_state()
        self._update_summary()
        self.status.configure(text="已勾选 {} 行".format(len(sel)))

    # ------------------------------------------------------------ 单元格编辑
    EDITABLE = {"title": I_TITLE, "artist": I_ARTIST, "album": I_ALBUM}

    def _row_path(self, iid):
        p = getattr(self, "row_paths", {}).get(iid)
        return Path(p) if p else None

    def _on_cell_dbl(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("cell", "tree"):
            return
        iid = self.tree.identify_row(event.y)
        colid = self.tree.identify_column(event.x)
        if not iid or not colid:
            return
        idx = int(colid[1:]) - 1
        cols = list(self.tree["columns"])
        if idx < 0 or idx >= len(cols):
            return
        cname = cols[idx]
        p = self._row_path(iid)
        if p is None:
            return

        if cname in self.EDITABLE:
            if not editable_fields(p):
                messagebox.showinfo(
                    "不能编辑",
                    "{} 格式不支持编辑标签。\n\n"
                    "可支持的格式：FLAC / MP3 / WAV / DSF / DFF / AIFF\n"
                    "这类文件可以改文件名：选中该行后点上方「编辑选中」。".format(
                        p.suffix.lower() or "无扩展名"))
                return
            self._inline_edit(iid, idx, cname, p)
            return

        if cname == "cover":
            self._one_cover(p, iid)
        elif cname == "lyrics":
            self._one_lyric(p, iid)
        else:
            self._show_detail(iid)

    # ---- 单元格就地编辑（可被外部提交，便于测试与「保存」按钮） ----
    def _inline_edit(self, iid, idx, cname, p):
        bbox = self.tree.bbox(iid, "#{}".format(idx + 1))
        if not bbox:
            self.log("[编辑] 取不到单元格位置，无法就地编辑")
            return
        x, y, w, h = bbox
        vals = list(self.tree.item(iid, "values"))
        cur = vals[idx] if idx < len(vals) else ""
        self._close_inline_edit()
        ed = tk.Entry(self.tree, font=("微软雅黑", 9), bd=1, relief="solid")
        ed.insert(0, cur)
        ed.select_range(0, "end")
        ed.place(x=x, y=y, width=max(w, 180), height=h)
        ed.focus_set()
        self._edit_entry = ed
        self._edit_ctx = {"iid": iid, "idx": idx, "cname": cname,
                          "path": p, "cur": cur, "done": False}
        ed.bind("<Return>", lambda e: self.commit_inline())
        ed.bind("<FocusOut>", lambda e: self.commit_inline())
        ed.bind("<Escape>", lambda e: self.cancel_inline())

    def _close_inline_edit(self):
        ed = getattr(self, "_edit_entry", None)
        if ed is not None:
            try:
                ed.destroy()
            except Exception:
                pass
        self._edit_entry = None
        self._edit_ctx = None

    def cancel_inline(self):
        self._close_inline_edit()

    def commit_inline(self):
        """提交就地编辑：写盘 → 回读比对 → 更新表格 → 同步缓存。"""
        ctx = getattr(self, "_edit_ctx", None)
        if not ctx or ctx.get("done"):
            return
        ctx["done"] = True
        ed = getattr(self, "_edit_entry", None)
        new = ed.get().strip() if ed is not None else ""
        self._close_inline_edit()

        p, cname, idx, iid = ctx["path"], ctx["cname"], ctx["idx"], ctx["iid"]
        if new == ctx["cur"]:
            self.status.configure(text="未改动")
            return
        try:
            write_tag(p, cname, new)
        except Exception as exc:
            messagebox.showerror("写入失败", "{}\n\n{}".format(p.name, exc))
            self.log("[编辑] 写入失败 {}: {}".format(p.name[:40], exc))
            self.status.configure(text="写入失败：{}".format(exc))
            return

        # 关键：从磁盘回读，确认真的落盘了
        back = read_tag(p, cname)
        if new and back != new:
            messagebox.showerror(
                "写入没有生效",
                "回读文件得到的仍是 {!r}，与输入 {!r} 不一致。\n\n{}".format(back, new, p))
            self.log("[编辑] 回读不一致 {}：期望 {!r} 实际 {!r}".format(p.name[:40], new, back))
            self.status.configure(text="写入未生效（回读不一致）")
            return

        vals = list(self.tree.item(iid, "values"))
        vals[idx] = new
        try:
            self.tree.tag_configure("edited", background="#D8ECFF")
        except Exception:
            pass
        self.tree.item(iid, values=vals, tags=("edited",))

        # 同步改文件名：用户的心智模型是「改了这条信息 → 本地文件名也要变」
        renamed = None
        if cname == "title" and getattr(self, "v_syncname", None) is not None \
                and self.v_syncname.get():
            try:
                renamed = rename_file(p, new)
                self.row_paths[iid] = str(renamed)
            except Exception as exc:
                self.log("[编辑] 标签已写入，但同步改名失败：{}".format(exc))
                renamed = None

        self._update_cache_row(renamed or p, vals, old_path=p)
        label = {"title": "标题", "artist": "艺术家", "album": "专辑"}.get(cname, cname)
        self.log("[编辑] {} {} -> {!r}（已回读确认落盘{}）".format(
            p.name[:40], label, new,
            "，文件名 → " + renamed.name[:40] if renamed else ""))
        msg = "已写入 {}：{} = {}".format(renamed.name[:26] if renamed else p.name[:26], label, new)
        if renamed:
            msg += "（文件名已同步）"
        self.status.configure(text=msg)

    def _update_cache_row(self, p, vals, old_path=None):
        """把改动同步进清单缓存，避免下次启动恢复成旧值。改名时用 old_path 定位。"""
        try:
            _sp, cp = _state_paths()
            if not cp.is_file():
                return
            data = json.loads(cp.read_text(encoding="utf-8"))
            key = str(old_path or p)
            for row in data.get("rows", []):
                if str(row[0]) == key:
                    row[0] = str(p)          # 改名后同步新路径
                    row[1] = list(vals)
                    break
            cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # allow-swallow: 缓存只是加速下次恢复，写不进去不影响音频文件


    def _one_cover(self, p, iid):
        """双击「封面」列：给这一个文件补封面。"""
        try:
            have = has_cover(p)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if have is None:
            messagebox.showinfo("不支持", "{} 格式本工具不写封面".format(p.suffix.lower()))
            return
        if have:
            if not messagebox.askyesno("已有封面", "{} 已有封面，要覆盖吗？".format(p.name)):
                return
        root = Path(self.src_var.get()) if self.mode.get() == "dir" and self.src_var.get() else None
        img, why, err = find_cover_image(p, root)
        if img is None:
            messagebox.showwarning("找不到图片",
                                   "该目录链上没有可用图片。\n可以点上方「选择统一封面」指定一张。")
            return
        src = self.cover_path.get().strip()
        if self.cover_mode.get() == "single" and src:
            img, why = Path(src), "界面指定"
        try:
            embed_cover(p, img)
        except Exception as exc:
            messagebox.showerror("写封面失败", str(exc))
            return
        vals = list(self.tree.item(iid, "values"))
        vals[I_COVER] = "✓"
        self.tree.item(iid, values=vals, tags=("miss",) if "✗" in (vals[I_COVER], vals[I_LYRIC]) else ("ok",))
        self.log("[封面] {} <- {} [{}]".format(p.name[:40], img.name, why))

    def _one_lyric(self, p, iid):
        """双击「歌词」列：给这一个文件取歌词。"""
        try:
            import lyriceng  # noqa: F401  仅确认引擎可用
        except Exception as exc:
            messagebox.showerror("缺少模块", str(exc))
            return
        if not messagebox.askyesno("取歌词", "按 标题/艺术家/专辑 查 lrclib.net 并写入 {} ？\n（需要联网）".format(p.name)):
            return
        try:
            _got, tag_name, text = _lrclib_query(p)
        except Exception as exc:
            messagebox.showerror("查询失败", str(exc))
            return
        if not _got:
            messagebox.showinfo("没找到", "lrclib 上没有匹配的歌词（或时长对不上被拒）。")
            return
        try:
            from mutagen.flac import FLAC as _FLAC
            import shutil as _sh, os as _os
            tmp = p.with_name(p.name + ".lyrtmp")
            _sh.copy2(str(p), str(tmp))
            f = _FLAC(str(tmp))
            f["LYRICS"] = text
            f.save()
            if not _FLAC(str(tmp)).get("lyrics"):
                raise RuntimeError("回读不到 LYRICS，已放弃替换")
            _os.replace(str(tmp), str(p))
        except Exception as exc:
            try:
                if 'tmp' in dir() and tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            messagebox.showerror("写入失败", str(exc))
            return
        vals = list(self.tree.item(iid, "values"))
        vals[I_LYRIC] = "✓"
        self.tree.item(iid, values=vals, tags=("miss",) if "✗" in (vals[I_COVER], vals[I_LYRIC]) else ("ok",))
        self.log("[歌词] {} 已写入 {} 字符".format(p.name[:40], len(text)))

    # ---------------------------------------------------- 线程安全的弹窗
    def _ask_on_main(self, fn, timeout=600):
        """在**主线程**执行 fn 并等它返回（供后台线程调用）。

        Tk 不是线程安全的：后台线程直接弹 messagebox 有时候能用、有时候整个卡死。
        这里用 after(0) 把窗口排到主线程事件队列，后台线程阻塞等结果。
        """
        box = {}
        done = threading.Event()
        # 注意：这里**不能**调 self.root.after —— 本函数跑在后台线程，
        # 而 tkinter 的所有调用（含 after）都必须在主线程。改成投递到队列，
        # 由主线程的 _poll 取出并执行（2026-11-30 实测报
        # "main thread is not in main loop"）。
        self._ui_requests.put((fn, box, done))
        if not done.wait(timeout):
            raise RuntimeError("等待用户操作超时")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    # ---------------------------------------------------- 半切状态确认
    def _split_partial_dialog(self, album_name, n_total, have_names, missing_names, audio_mb):
        """整轨还在、分轨只切了一部分时，先问清楚再动手。

        返回 "fill_del" / "fill_keep" / "skip"。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("这张已经切了一部分 — {}".format(album_name[:36]))
        dlg.geometry("620x420")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=album_name, font=("", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 2))
        ttk.Label(dlg, foreground="#555",
                  text="整轨 {} MB；共 {} 曲，目录里已有 {} 个单曲，缺 {} 首".format(
                      int(audio_mb), n_total, len(have_names), len(missing_names))
                  ).pack(anchor="w", padx=14)

        area = tk.Text(dlg, height=11, width=74, wrap="none")
        area.pack(fill="both", expand=True, padx=14, pady=8)
        area.insert("end", "已有的 {} 个：\n".format(len(have_names)))
        area.insert("end", "\n".join("  " + n for n in have_names[:8]) +
                    ("\n  …" if len(have_names) > 8 else "") + "\n\n")
        area.insert("end", "缺的 {} 首：\n".format(len(missing_names)))
        area.insert("end", "\n".join("  " + n for n in missing_names[:10]) +
                    ("\n  …" if len(missing_names) > 10 else ""))
        area.configure(state="disabled")

        choice = {"v": "skip"}

        def pick(v):
            choice["v"] = v
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=12)
        ttk.Button(btns, text="补齐缺的 {} 首，并删掉整轨".format(len(missing_names)),
                   command=lambda: pick("fill_del")).pack(side="left")
        ttk.Button(btns, text="只补齐，整轨留着",
                   command=lambda: pick("fill_keep")).pack(side="left", padx=8)
        ttk.Button(btns, text="跳过这张",
                   command=lambda: pick("skip")).pack(side="left")

        self.root.wait_window(dlg)
        return choice["v"]

    # ---------------------------------------------------- 切割后的收尾窗口
    def _split_finish_dialog(self, album_name, made, sidecars, preset_clean=False):
        """切完给用户一次机会：粘曲目表更正名字 + 决定要不要清理。

        返回 (粘贴的文本或 "", 是否清理)。
        用户原话：「切割后是否删除那一步添加个模板黏贴窗口，
        如果用户发现转的不对黏贴模板按模板来，不然就按 cue 来。」
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("切分完成 — {}".format(album_name[:40]))
        dlg.geometry("640x560")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="已切出 {} 个单曲：".format(len(made)),
                  font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 2))

        listbox = tk.Text(dlg, height=6, width=72, wrap="none")
        listbox.pack(fill="x", padx=12)
        listbox.insert("1.0", "\n".join("  " + Path(q).name for q in made[:20]) +
                       ("\n  …另有 {} 个".format(len(made) - 20) if len(made) > 20 else ""))
        listbox.configure(state="disabled")

        ttk.Label(dlg, text="曲名不对？把正确曲目表粘进来（留空则按 cue 原样）",
                  foreground="#555").pack(anchor="w", padx=12, pady=(12, 2))
        box = tk.Text(dlg, height=9, width=72, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=2)
        box.insert("1.0", "")

        hint = ttk.Label(dlg, text="", foreground="#0a7", justify="left")
        hint.pack(anchor="w", padx=12)

        def refresh(*_a):
            txt = box.get("1.0", "end").strip()
            if not txt:
                hint.configure(text="（空 = 按 cue 原样，不改名）", foreground="#999")
                return
            items = self._parse_track_list(txt)
            hint.configure(text="解析出 {} 首（{}）".format(
                len(items), "数量对得上" if len(items) == len(made) else
                "数量不一致，按文件名序号对应"), foreground="#0a7")
        box.bind("<KeyRelease>", refresh)
        refresh()

        do_clean = tk.BooleanVar(value=bool(preset_clean))
        total_mb = sum(q.stat().st_size for q in sidecars if q.exists()) / 1024 / 1024
        frame_clean = ttk.LabelFrame(dlg, text=" 收尾 ")
        frame_clean.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Checkbutton(
            frame_clean,
            text="清理 {} 个不再需要的文件（合计 {:.0f} MB）".format(len(sidecars), total_mb),
            variable=do_clean).pack(anchor="w", padx=8, pady=(6, 2))
        def _sz(q):
            try:
                return q.stat().st_size / 1024 / 1024
            except OSError:
                return 0.0

        ttk.Label(frame_clean, foreground="#666", justify="left",
                  text="  " + "\n  ".join(
                      "{}  ({:.1f} MB)".format(q.name, _sz(q))
                      for q in sidecars[:6]) +
                  ("\n  …另有 {} 个".format(len(sidecars) - 6) if len(sidecars) > 6 else "") +
                  "\n  封面图和 Artwork 会保留").pack(anchor="w", padx=8, pady=(0, 6))

        result = {"text": "", "clean": False}

        def ok():
            result["text"] = box.get("1.0", "end").strip()
            result["clean"] = bool(do_clean.get())
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="应用", command=ok).pack(side="left")
        ttk.Button(btns, text="什么都不做", command=dlg.destroy).pack(side="left", padx=8)

        box.focus_set()
        self.root.wait_window(dlg)
        return result["text"], result["clean"]

    # ---------------------------------------------------- 粘贴曲目清单
    @staticmethod
    def _parse_track_list(text):
        """把粘进来的清单解析成 [(序号, 曲名)]。

        支持的写法（混着来也行）：
            01. 不必等我
            1、我终于失去了妳
            02 让我自己走
            我的眼中只有妳          ← 没有序号，按出现顺序自动编号
        """
        import re as _r
        out = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            m = _r.match(r'^\s*(\d{1,3})\s*[.．、)\]：:]?\s*(.+?)\s*$', line)
            if m:
                out.append((int(m.group(1)), m.group(2).strip()))
            else:
                out.append((len(out) + 1, line))
        return out

    def paste_track_list(self):
        """粘贴曲目清单，给勾选/选中的行批量补文件名与标题标签。"""
        pairs = [iid for iid in (self.tree.selection() or self.tree.get_children())
                 if iid in self.row_paths]
        # 优先用勾选行；没勾就用全部
        checked = [(iid, self.row_paths[iid]) for iid in self.row_paths
                   if self._check_state.get(self.row_paths[iid], True)]
        if not checked:
            checked = [(iid, p) for iid, p in self.row_paths.items()]

        dlg = tk.Toplevel(self.root)
        dlg.title("粘贴曲目清单 — {} 个文件".format(len(checked)))
        dlg.geometry("620x520")
        dlg.transient(self.root)

        ttk.Label(dlg, text="把曲目表粘进来（一行一首，可带 01. / 1、 这类序号，不带也行）",
                  foreground="#555").pack(anchor="w", padx=10, pady=(10, 2))

        box = tk.Text(dlg, height=16, width=68, wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=4)
        box.insert("1.0", "01. 第一首\n02. 第二首\n…")

        preview = ttk.Label(dlg, text="", foreground="#0a7", justify="left")
        preview.pack(anchor="w", padx=10)

        def refresh_preview(*_a):
            items = self._parse_track_list(box.get("1.0", "end"))
            if not items:
                preview.configure(text="（还没粘内容）", foreground="#999")
                return
            head = "\n".join("  {:02d}  {}".format(n, t[:40]) for n, t in items[:4])
            more = "" if len(items) <= 4 else "\n  …共 {} 首".format(len(items))
            ok = len(items) == len(checked)
            preview.configure(
                text="解析出 {} 首，待处理 {} 个文件{}\n{}".format(
                    len(items), len(checked),
                    "" if ok else "（数量不一致，只按序号对应）", head + more),
                foreground="#0a7" if ok else "#c60")
        box.bind("<KeyRelease>", refresh_preview)
        refresh_preview()

        def do_apply():
            items = self._parse_track_list(box.get("1.0", "end"))
            if not items:
                preview.configure(text="没有解析到任何曲目", foreground="#c00")
                return
            by_no = {n: t for n, t in items}
            dlg.destroy()
            self.pbar.configure(maximum=max(1, len(checked)), value=0)
            self.status.configure(text="补全曲目信息 0/{}…".format(len(checked)))
            self.log("[清单] 开始补全 {} 个文件（解析到 {} 首）".format(len(checked), len(items)))

            def work():
                ok_n = fail_n = 0
                for i, (iid, p) in enumerate(checked, 1):
                    path = Path(p)
                    # 序号从文件名开头取（01 xxx.flac）；取不到就按顺序
                    import re as _r
                    m = _r.match(r'^(\d{1,3})', path.stem)
                    no = int(m.group(1)) if m else i
                    title = by_no.get(no)
                    if not title:
                        self.q.put(("log", "    [跳过] {}（清单里没有第 {} 首）".format(
                            path.name[:44], no)))
                        fail_n += 1
                        self.q.put(("tick", i))
                        continue
                    try:
                        # 先写标签，再改文件名
                        write_tag(path, "title", title)
                        new_stem = "{:02d} {}".format(no, title)
                        from musiclib import safe_name
                        new_stem = safe_name(new_stem)
                        newp = path
                        if path.stem != new_stem:
                            newp = rename_file(path, new_stem)
                        self.q.put(("log", "    [OK] {} → {}".format(
                            path.name[:34], Path(newp).name[:44])))
                        ok_n += 1
                    except Exception as exc:
                        self.q.put(("log", "    [失败] {}：{}".format(path.name[:34], exc)))
                        fail_n += 1
                    self.q.put(("tick", i))
                self.q.put(("log", "[清单] 完成：成功 {} 个，失败 {} 个".format(ok_n, fail_n)))
                self.q.put(("done", "曲目信息补全完成：成功 {}，失败 {}".format(ok_n, fail_n)))
                self.q.put(("autoscan", True))

            threading.Thread(target=work, daemon=True, name="tracklist").start()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="应用到 {} 个文件".format(len(checked)),
                   command=do_apply).pack(side="left")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=8)

        box.focus_set()

    def batch_edit_tags(self):
        """批量编辑选中行的标签：勾哪个字段改哪个，没勾的保持原样。

        为什么不复用 edit_selected：那个只处理 sel[0]，多选等于白选
        （用户实测：「多选右键编辑标签只会编辑队末的信息」）。
        """
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "先在表格里选中一行或多行（Ctrl 加选、Shift 范围选）")
            return
        pairs = []
        for iid in sel:
            p = self._row_path(iid)
            if p is not None:
                pairs.append((iid, p))
        if not pairs:
            return
        n = len(pairs)

        dlg = tk.Toplevel(self.root)
        dlg.title("批量编辑标签 — {} 个文件".format(n))
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("680x330")

        ttk.Label(
            dlg,
            text="勾选要改的字段，没勾的字段保持原样。只改标签，不动文件名。",
            foreground="#666",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 6))

        # 标题：每首不同，批量不给改
        use_title = tk.BooleanVar(value=False)
        use_artist = tk.BooleanVar(value=False)
        use_album = tk.BooleanVar(value=False)
        v_title, v_artist, v_album = tk.StringVar(), tk.StringVar(), tk.StringVar()

        ttk.Checkbutton(dlg, variable=use_title, state="disabled").grid(row=1, column=0, padx=(10, 4))
        ttk.Label(dlg, text="歌曲标题").grid(row=1, column=1, sticky="e", padx=4)
        e1 = ttk.Entry(dlg, textvariable=v_title, width=52, state="disabled")
        e1.grid(row=1, column=2, sticky="we", padx=(4, 10), pady=4)
        ttk.Label(dlg, text="（批量不改标题：每首不同）", foreground="#c60").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=4
        )

        ttk.Checkbutton(dlg, variable=use_artist).grid(row=3, column=0, padx=(10, 4))
        ttk.Label(dlg, text="艺术家").grid(row=3, column=1, sticky="e", padx=4)
        ttk.Entry(dlg, textvariable=v_artist, width=52).grid(
            row=3, column=2, sticky="we", padx=(4, 10), pady=4
        )

        ttk.Checkbutton(dlg, variable=use_album).grid(row=4, column=0, padx=(10, 4))
        ttk.Label(dlg, text="唱片标题").grid(row=4, column=1, sticky="e", padx=4)
        ttk.Entry(dlg, textvariable=v_album, width=52).grid(
            row=4, column=2, sticky="we", padx=(4, 10), pady=4
        )

        hint = ttk.Label(dlg, text="", foreground="#c60")
        hint.grid(row=5, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 0))

        def do_apply():
            if not (use_artist.get() or use_album.get()):
                hint.config(text="先勾选一个要改的字段")
                return
            plan = []
            if use_artist.get():
                plan.append(("artist", v_artist.get().strip()))
            if use_album.get():
                plan.append(("album", v_album.get().strip()))

            # 关键：不要在 UI 线程里写盘。51 个文件 × 2 个字段 = 102 次
            # write_tag，每次都要重写整个 FLAC 元数据块，在 U 盘上能卡几十秒，
            # 表现就是窗口「未响应」而 CPU 只有 4%。
            dlg.destroy()
            self.pbar.configure(maximum=max(1, len(pairs)), value=0)
            self.status.configure(text="批量改标签 0/{}…".format(len(pairs)))
            self.log("[批量编辑] 开始：{} 个文件，字段 {}".format(
                len(pairs), ", ".join(k for k, _ in plan)))

            def work():
                ok = fail = 0
                errs = []
                for i, (iid, p) in enumerate(pairs, 1):
                    row_ok = True
                    for key, val in plan:
                        try:
                            write_tag(p, key, val)
                        except Exception as exc:
                            row_ok = False
                            errs.append("{} / {}: {}".format(p.name[:30], key, exc))
                    if row_ok:
                        ok += 1
                    else:
                        fail += 1
                    self.q.put(("tick", i))
                self.q.put(("log", "[批量编辑] 成功 {} 个，失败 {} 个".format(ok, fail)))
                for e in errs[:20]:
                    self.q.put(("log", "    " + e))
                self.q.put(("done", "批量编辑完成：成功 {}，失败 {}".format(ok, fail)))
                self.q.put(("autoscan", None))

            threading.Thread(target=work, daemon=True, name="batchtag").start()

        btns = ttk.Frame(dlg)
        btns.grid(row=6, column=0, columnspan=3, pady=14)
        ttk.Button(btns, text="应用到 {} 个文件".format(n), command=do_apply).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=6)

        dlg.columnconfigure(2, weight=1)
        dlg.wait_window()

    def edit_selected(self):
        """工具栏「编辑选中」：打开对话框，可改标签与文件名。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在表格里选一行")
            return
        iid = sel[0]
        p = self._row_path(iid)
        if p is None:
            return
        v = list(self.tree.item(iid, "values"))
        can_tag = bool(editable_fields(p))

        dlg = tk.Toplevel(self.root)
        dlg.title("编辑 — {}".format(p.name))
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("640x300")

        vars_ = {}
        rowi = 0
        for key, label, idx in (("title", "歌曲标题", 0), ("artist", "艺术家", 1), ("album", "唱片标题", 2)):
            ttk.Label(dlg, text=label).grid(row=rowi, column=0, sticky="e", padx=8, pady=6)
            var = tk.StringVar(value=v[idx] if idx < len(v) else "")
            vars_[key] = var
            ent = ttk.Entry(dlg, textvariable=var, width=62,
                            state="normal" if can_tag else "disabled")
            ent.grid(row=rowi, column=1, sticky="we", padx=6)
            rowi += 1

        ttk.Label(dlg, text="文件名").grid(row=rowi, column=0, sticky="e", padx=8, pady=6)
        stem_var = tk.StringVar(value=p.stem)
        ttk.Entry(dlg, textvariable=stem_var, width=62).grid(row=rowi, column=1, sticky="we", padx=6)
        rowi += 1

        ttk.Label(dlg, text="路径：{}".format(p.parent), foreground="#666").grid(
            row=rowi, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        rowi += 1
        if not can_tag:
            ttk.Label(dlg, text="该格式（{}）不支持写标签，只能改文件名".format(p.suffix.lower()),
                      foreground="#c60").grid(row=rowi, column=0, columnspan=2, sticky="w", padx=8)
            rowi += 1

        btns = ttk.Frame(dlg)
        btns.grid(row=rowi, column=0, columnspan=2, pady=12)

        def do_save():
            changed = []
            if can_tag:
                for key, var in vars_.items():
                    new = var.get().strip()
                    idx = {"title": 0, "artist": 1, "album": 2}[key]
                    if new != (v[idx] if idx < len(v) else ""):
                        try:
                            write_tag(p, key, new)
                            v[idx] = new
                            changed.append(key)
                        except Exception as exc:
                            messagebox.showerror("写标签失败", "{}: {}".format(key, exc))
                            return
            new_stem = stem_var.get().strip()
            final_p = p
            if new_stem and new_stem != p.stem:
                try:
                    final_p = rename_file(p, new_stem)
                    changed.append("文件名")
                except Exception as exc:
                    messagebox.showerror("改名失败", str(exc))
                    return
            self.tree.item(iid, values=v)
            self.row_paths[iid] = str(final_p)
            self._update_cache_row(final_p, v)
            if can_tag:
                bad = []
                for key, idx2 in (("title", I_TITLE), ("artist", I_ARTIST), ("album", I_ALBUM)):
                    if str(v[idx2]) != read_tag(final_p, key):
                        bad.append(key)
                if bad:
                    messagebox.showwarning(
                        "部分字段没写进去",
                        "这些字段回读不一致：{}\n\n文件：{}".format("、".join(bad), final_p))
                    self.log("[编辑] 回读不一致：{} {}".format(final_p.name[:40], bad))
            self.log("[编辑] {} 已更新：{}（已回读确认）".format(
                final_p.name[:40], "、".join(changed) or "无变化"))
            self.status.configure(text="已保存：{}".format(final_p.name[:30]))
            dlg.destroy()

        ttk.Button(btns, text="保存", width=10, command=do_save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", width=10, command=dlg.destroy).pack(side="left", padx=6)

    # ------------------------------------------------------------ 悬停看大图
    def _on_hover(self, event):
        region = self.tree.identify("region", event.x, event.y)
        # 注意：Tk 对 #0 列（图列）返回的 region 是 "tree"，不是 "cell"，
        # 只判 "cell" 会导致悬停永远不触发（这个坑踩过一次）。
        if region not in ("cell", "tree"):
            self._hide_preview()
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._hide_preview()
            return
        is_pic = (region == "tree") or (col == "#0")
        if not is_pic:
            self._hide_preview()
            return
        if iid == self._preview_iid and self._preview_win is not None:
            return                      # 已经在显示同一行，不要反复重建
        big = self._bigpics.get(iid) or self._thumbs.get(iid)
        if big is None:
            self._hide_preview()
            return
        self._hide_preview()
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            lbl = tk.Label(win, image=big, bd=1, relief="solid", bg="#ffffff")
            lbl.pack()
            win.update_idletasks()
            x = event.x_root + 22
            y = event.y_root + 18
            win.geometry("+{}+{}".format(x, y))
            win.deiconify()
            win.lift()
            self._preview_win = win
            self._preview_iid = iid
        except Exception:
            self._preview_win = None

    def _hide_preview(self, _=None):
        if getattr(self, "_preview_win", None) is not None:
            try:
                self._preview_win.destroy()
            except Exception:
                pass
        self._preview_win = None
        self._preview_iid = None

    # ------------------------------------------------------------ 全选
    def _select_all_rows(self, event=None):
        """Ctrl+A：全选表格所有行。

        两件事要注意：
        ①tkinter 的 Treeview 默认**没有**这个绑定，不自己加就是"点了没反应"；
        ②焦点在输入框（内联编辑 / 路径框）里时，Ctrl+A 必须是"全选文本"，
          不能抢去选行——否则编辑到一半按 Ctrl+A 会把焦点弄跑。
        """
        w = None
        try:
            w = self.root.focus_get()
        except Exception:
            pass
        # 焦点在输入类控件上 → 放行给默认的"全选文本"，
        # 否则编辑到一半按 Ctrl+A 会跑成"全选行"
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, tk.Spinbox, ttk.Spinbox)):
            return None
        # 只在清单页生效
        if getattr(self, "tab", 0) not in (0, None):
            return None
        rows = self.tree.get_children()
        if rows:
            self.tree.selection_set(rows)
            self.tree.focus(rows[0])
            self.tree.see(rows[0])
            self.status.configure(text="已全选 {} 行（右键可批量编辑标签）".format(len(rows)))
        return "break"           # 阻止事件继续传给默认处理

    # ------------------------------------------------------------ 右键菜单
    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        p = self._row_path(iid)
        if p is None:
            return
        m = tk.Menu(self.root, tearoff=0)
        n_sel = len(self.tree.selection())
        m.add_command(label="编辑标签与文件名…（只改第一行）", command=self.edit_selected)
        if n_sel > 1:
            m.add_command(
                label="批量编辑选中 {} 行的标签…".format(n_sel),
                command=self.batch_edit_tags,
            )
        m.add_command(label="勾上选中的所有行", command=self.check_selection)
        m.add_command(label="取消勾选选中的所有行", command=self.uncheck_selection)
        m.add_separator()
        m.add_command(label="按当前模板改名（只改勾选行）", command=self.rename_checked)
        m.add_separator()
        m.add_command(label="给这一行设封面…", command=lambda: self._one_cover_pick(p, iid))
        m.add_command(label="给选中的所有行设同一张封面…", command=self.bulk_set_cover)
        m.add_separator()
        m.add_command(label="补这一段目录的封面（沿目录找图）", command=lambda: self._one_cover(p, iid))
        m.add_command(label="取这一行的歌词", command=lambda: self._one_lyric(p, iid))
        m.add_separator()
        m.add_command(label="粘贴曲目清单补全信息…", command=self.paste_track_list)
        m.add_command(label="重扫这一行", command=lambda: self._refresh_row(iid, p))
        m.add_separator()
        m.add_command(label="删除…（从清单移除，可选删本地文件）", command=self.delete_rows)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _one_cover_pick(self, p, iid):
        """手动挑一张图给这一行。"""
        f = filedialog.askopenfilename(
            title="选择封面图片", filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("全部", "*.*")])
        if not f:
            return
        self._apply_cover_to([(iid, p)], Path(f), "手动指定")

    def _apply_cover_to(self, pairs, img_path, why, max_px=700):
        ok = bad = 0
        for iid, p in pairs:
            try:
                embed_cover(p, img_path, max_px=max_px)
                vals = list(self.tree.item(iid, "values"))
                vals[I_COVER] = "✓"
                self._set_row_pic(iid, p)
                self.tree.item(iid, values=vals,
                               tags=("miss",) if "✗" in (vals[I_COVER], vals[I_LYRIC]) else ("ok",))
                ok += 1
                self.log("[封面] {} <- {} [{}]".format(p.name[:40], img_path.name, why))
            except Exception as exc:
                bad += 1
                self.log("[封面] 失败 {}: {}".format(p.name[:40], exc))
        msg = "封面写入成功 {} 个，失败 {} 个（图：{}）".format(ok, bad, img_path.name)
        self.log(msg)
        self.status.configure(text=msg)

    def _set_row_pic(self, iid, p):
        """写入封面后立刻刷新该行的缩略图。"""
        raw = get_cover_bytes(p)
        if not raw:
            return
        small = self._pic_b64(raw, 52)
        big = self._pic_b64(raw, 260)
        if small:
            try:
                ph = tk.PhotoImage(data=small)
                self._thumbs[iid] = ph
                self.tree.item(iid, image=ph, text="")
                if big:
                    self._bigpics[iid] = tk.PhotoImage(data=big)
            except Exception:
                pass

    def rename_checked(self):
        """只对「已勾选」的行按当前模板改名（先预览，再确认）。"""
        pairs = []
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if v and len(v) > I_CHK and v[I_CHK] == CHECK_ON:
                p = self._row_path(iid)
                if p is not None:
                    pairs.append((iid, p))
        if not pairs:
            messagebox.showinfo("提示", "先在表格第一列勾选要改名的行")
            return

        tpl = self.name_tpl.get().strip() or DEFAULT_TEMPLATE_POP
        plan = []
        for iid, p in pairs:
            f = open_flac(p)
            if f is None:
                base = ""
            else:
                try:
                    base = build_name_by_template(f, tpl, stem=p.stem, ext=p.suffix)
                except Exception:
                    base = ""
            base = base or p.stem
            new = p.with_name(safe_name(base) + p.suffix)
            if new != p:
                plan.append((iid, p, new))

        if not plan:
            messagebox.showinfo("无需改名", "勾选的 {} 个文件按当前模板算下来都不需要改名。".format(len(pairs)))
            return

        # 先看标题是不是 Track01 这种占位符 —— 那种情况按模板改名只会把
        # 占位符原样搬到文件名里，越改越乱（2026-11-30 用户截图证过）。
        import re as _r
        placeholder = []
        for _iid, _p, _n in plan:
            _f = open_flac(_p)
            if _f is None:
                continue
            try:
                _t = (_f.get("title") or [""])[0].strip()
            except Exception:
                _t = ""
            if _t and _r.match(r'^track\\s*\\d+$', _t, _r.I):
                placeholder.append(_p)
        if placeholder:
            head = "\n".join("  " + q.name[:50] for q in placeholder[:8])
            more2 = "" if len(placeholder) <= 8 else "\n  …另有 {} 个".format(len(placeholder) - 8)
            if messagebox.askyesno(
                    "标题是占位符",
                    "这 {} 个文件的标题都是 Track01/Track02 这种占位符：\n\n{}{}\n\n"
                    "按模板改名只会把占位符原样搬到文件名里（结果是「Track01 - 赵传 - 我终于失去了你.flac」）。\n\n"
                    "要改用「粘贴曲目清单」按真实曲目表改名吗？（推荐）".format(
                        len(placeholder), head, more2)):
                self.paste_track_list()
                return

        preview = "\n".join("{}  ->  {}".format(p.name[:44], n.name[:54]) for _i, p, n in plan[:12])
        more = "" if len(plan) <= 12 else "\n…另有 {} 个".format(len(plan) - 12)
        if not messagebox.askyesno(
                "确认改名",
                "模板：{}\n\n已勾选 {} 个，其中 {} 个需要改名：\n\n{}{}\n\n"
                "会同时把模板里的标题/演出者/专辑同步写进标签（只补空字段，不覆盖已有值）；"
                  "目标同名会跳过。继续？".format(
                    tpl, len(pairs), len(plan), preview, more)):
            return

        ok = bad = skipped = 0
        for iid, p, new in plan:
            try:
                if new.exists():
                    skipped += 1
                    self.log("[改名] 跳过（同名已存在） {}".format(new.name[:50]))
                    continue
                p.rename(new)
                self.row_paths[iid] = str(new)
                ok += 1
                self.log("[改名] {} -> {}".format(p.name[:40], new.name[:50]))
                # 同步标签：按模板把「标题/演出者/专辑」补回去（只补空字段）
                synced = self._sync_tags_from_name(new, tpl)
                if synced:
                    self.log("        同步标签：{}".format("、".join(synced)))
            except Exception as exc:
                bad += 1
                self.log("[改名] 失败 {}: {}".format(p.name[:40], exc))
        msg = "改名完成：成功 {}，同名跳过 {}，失败 {}（模板 {}）".format(ok, skipped, bad, tpl)
        self.log(msg)
        self.status.configure(text=msg)
        self._update_summary()

    def _sync_tags_from_name(self, path, tpl):
        """按模板把标题/演出者/专辑补进标签（只补空字段）。返回补了哪些。"""
        from pathlib import Path as _P
        path = _P(path)
        f = open_flac(path)
        fields = (("title", "{标题}", "title"),
                  ("artist", "{演出者}", "artist"),
                  ("album", "{专辑}", "album"))
        done = []
        for key, var, label in fields:
            if var not in (tpl or ""):
                continue
            try:
                cur = (f.get(key) or [""])[0] if f is not None else ""
            except Exception:
                cur = ""
            if cur:
                continue          # 已有值，不动
            try:
                val = build_name_by_template(f, var, stem=path.stem, ext=path.suffix) \
                    if f is not None else ""
            except Exception:
                val = ""
            if not val or val == " ":
                continue
            try:
                write_tag(path, key, val)
                done.append(label)
            except Exception:
                pass  # allow-swallow: 单个字段写失败不影响改名结果
        return done

    def bulk_set_cover(self):
        """给表格里选中的所有行设同一张封面（自动缩放到封面尺寸）。"""
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "先在表格里选中要设封面的行（可按住 Ctrl / Shift 多选）")
            return
        pairs = []
        for iid in sel:
            p = self._row_path(iid)
            if p is not None:
                pairs.append((iid, p))
        if not pairs:
            return
        f = filedialog.askopenfilename(
            title="选择封面图片（会缩小到 700px 以内再内嵌）",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("全部", "*.*")])
        if not f:
            return
        if not messagebox.askyesno(
                "确认",
                "给选中的 {} 个文件设同一张封面？\n\n图：{}\n\n"
                "已有封面的会被覆盖；写入走副本+校验，原文件在成功前不动。".format(
                    len(pairs), Path(f).name)):
            return
        self.status.configure(text="正在写入封面…")
        threading.Thread(target=self._apply_cover_to, args=(pairs, Path(f), "批量指定"),
                         daemon=True).start()

    def _refresh_row(self, iid, p):
        """重扫一行，刷新所有列与缩略图。"""
        try:
            vals = list(self.tree.item(iid, "values"))
            # 注意：第 0 列是勾选列，标题/艺术家/专辑必须用常量索引，
            # 写 vals[0] 会把标题塞进勾选列，整行看起来就"乱了"（踩过）。
            vals[I_TITLE] = read_tag(p, "title") or p.stem
            vals[I_ARTIST] = read_tag(p, "artist") or read_tag(p, "albumartist")
            vals[I_ALBUM] = read_tag(p, "album")
            hc = has_cover(p)
            vals[I_COVER] = "✓" if hc else "✗"
            if hc is None:
                self.log("[重扫] {} 该格式不支持读封面".format(p.suffix.lower()))
            self.tree.item(iid, values=vals,
                           tags=("miss",) if "✗" in (vals[I_COVER], vals[I_LYRIC]) else ("ok",))
            self._set_row_pic(iid, p)
            self.log("[重扫] {}".format(p.name[:50]))
        except Exception as exc:
            self.log("[重扫] 失败 {}: {}".format(p.name[:40], exc))
            messagebox.showerror("重扫失败", "{}\n\n{}".format(p.name, exc))

    def _under_readonly(self, path):
        """判断路径是否落在只读根（Z: 保种源等）。"""
        try:
            from musiclib import READONLY_ROOTS
        except Exception:
            READONLY_ROOTS = ["z:"]
        s = str(path).lower().replace("/", "\\")
        for r in READONLY_ROOTS:
            rr = str(r).lower().rstrip("\\")
            if s == rr or s.startswith(rr + "\\"):
                return True
        return False

    def delete_rows(self):
        """删除：二次确认 + 可选「同时删除本地文件」。默认只从清单移除。"""
        sel = list(self.tree.selection())
        if not sel:
            sel = [iid for iid in self.tree.get_children()
                   if len(self.tree.item(iid, "values")) > I_CHK
                   and self.tree.item(iid, "values")[I_CHK] == CHECK_ON]
            src = "已勾选"
        else:
            src = "选中"
        pairs = [(iid, self._row_path(iid)) for iid in sel]
        pairs = [(i, q) for i, q in pairs if q is not None]
        if not pairs:
            messagebox.showinfo("提示", "先选中要删除的行（Ctrl / Shift 多选），或先勾选")
            return

        names = [q.name for _i, q in pairs[:12]]
        more = "" if len(pairs) <= 12 else "\n…另有 {} 个".format(len(pairs) - 12)
        ro = [q for _i, q in pairs if self._under_readonly(q)]

        dlg = tk.Toplevel(self.root)
        dlg.title("确认删除")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="将处理 {} 个{}文件：".format(len(pairs), src),
                  font=("微软雅黑", 10, "bold")).pack(anchor="w")
        lst = tk.Text(body, height=min(10, max(3, len(names))), width=64, wrap="none")
        lst.pack(fill="both", expand=True, pady=8)
        lst.insert("1.0", "\n".join(names) + more)
        lst.configure(state="disabled")

        v_delfile = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(body, text="同时删除磁盘上的文件（不可恢复！）", variable=v_delfile)
        cb.pack(anchor="w", pady=(0, 4))
        if ro:
            cb.configure(state="disabled")
            ttk.Label(body, text="选中的文件里有 {} 个位于只读盘（如 Z: 保种源），\n"
                                 "这批不会被删除，只从清单移除。".format(len(ro)),
                      foreground="#b00").pack(anchor="w", pady=(0, 4))

        ttk.Label(body, text="不勾选 = 只从清单里移除，磁盘文件原样保留。",
                  foreground="#555").pack(anchor="w")

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        result = {"ok": False}

        def do_delete():
            result["ok"] = True
            dlg.destroy()

        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="删除", command=do_delete).pack(side="right", padx=4)
        dlg.wait_window()

        if not result["ok"]:
            return

        del_file = bool(v_delfile.get())
        removed = deleted = failed = kept_ro = 0
        for iid, q in pairs:
            if del_file:
                if self._under_readonly(q):
                    kept_ro += 1
                    self.log("[删除] 只读盘，仅移出清单：{}".format(q))
                else:
                    try:
                        q.unlink()
                        deleted += 1
                        self.log("[删除] 已删文件：{}".format(q))
                    except Exception as exc:
                        failed += 1
                        self.log("[删除] 删文件失败 {}：{}".format(q, exc))
                        continue
            try:
                self.row_paths.pop(iid, None)
                self._thumbs.pop(iid, None)
                self._bigpics.pop(iid, None)
                self._check_state.pop(str(q), None)
                self.tree.delete(iid)
                removed += 1
            except Exception:
                pass
            self._drop_cache_row(q)

        self._update_summary()
        self._save_state()
        msg = "已从清单移除 {} 个".format(removed)
        if del_file:
            msg += "；删除文件 {} 个".format(deleted)
            if failed:
                msg += "，失败 {}".format(failed)
            if kept_ro:
                msg += "，{} 个在只读盘未删".format(kept_ro)
        self.log("[删除] " + msg)
        self.status.configure(text=msg)

    def _drop_cache_row(self, path):
        """把删掉的行从清单缓存里去掉，免得下次启动又冒出来。"""
        try:
            _sp, cp = _state_paths()
            if not cp.is_file():
                return
            data = json.loads(cp.read_text(encoding="utf-8"))
            rows = [r for r in data.get("rows", []) if str(r[0]) != str(path)]  # allow-index: 缓存行首元素是路径，不是表格列
            data["rows"] = rows
            cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # allow-swallow: 缓存只是加速下次恢复，删不掉不影响主流程

    def _show_detail(self, iid):
        v = self.tree.item(iid, "values")
        p = self._row_path(iid)
        messagebox.showinfo("条目详情", "标题: {}\n艺术家: {}\n唱片: {}\n格式: {}\n时长: {}\n规格: {}\n封面: {}\n歌词: {}\n大小: {}\n\n文件: {}".format(
            *v, p if p else "?"))

    def filter_split(self):
        """把「已切分但原文件还在」的整轨移到最上面，方便统一清理。

        与「只看缺项」同一种做法：只重排序，不隐藏其它行——
        隐藏后用户就分不清列表里到底有什么了。
        """
        done = getattr(self, "_split_done", set())
        hit = []
        rest = []
        for iid in self.tree.get_children():
            path = self.row_paths.get(iid, "")
            (hit if path and str(Path(path).resolve()) in done else rest).append(iid)
        pos = 0
        for iid in hit + rest:
            self.tree.move(iid, "", pos)
            pos += 1
        self.status.configure(text="已切分未删原件：{} 个（已排到最前）".format(len(hit)))
        if not hit:
            self.summary.configure(text="没有「已切分但原文件还在」的整轨")

    def filter_missing(self):
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if v and (v[I_COVER] == "✗" or v[I_LYRIC] == "✗"):
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
            self._save_state(now=True)      # 选完立刻落盘，不等关窗

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
        if self.logfile:
            try:
                self.logfile.write("{}\n".format(msg))
                self.logfile.flush()
            except Exception:
                pass

    def _open_logfile(self, dry):
        """每次运行都单独落一份日志，关掉窗口也能事后查哪个文件失败。"""
        try:
            d = app_dir() / "logs"
            d.mkdir(exist_ok=True)
            name = "musiclib-{}-{}.txt".format(
                time.strftime("%Y%m%d-%H%M%S"), "preview" if dry else "apply")
            self.logpath = d / name
            self.logfile = open(self.logpath, "w", encoding="utf-8")
            self.log("日志文件：{}".format(self.logpath))
        except Exception:
            self.logfile = None
            self.logpath = None

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
            "checked": [str(p) for p in self._checked_paths()],
            "mode": mode,
            "root": Path(self.src_var.get()) if mode == "dir" else None,
            "files": list(self.files),
            "rename": self.v_rename.get(),
            "template": (self.name_tpl.get().strip() or DEFAULT_TEMPLATE_POP),
            "flatten": self.v_flatten.get(),
            "cover": self.v_cover.get(),
            "cover_mode": self.cover_mode.get(),
            "cover_path": self.cover_path.get(),
            "downmix": self.v_downmix.get(),
            "wav2flac": self.v_wav2flac.get(),
            "cuesplit": self.v_cuesplit.get(),
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
        if self.logfile:
            try:
                self.logfile.close()
            except Exception:
                pass  # allow-swallow: 关窗时关日志文件，失败无后果
        self._open_logfile(dry)
        self.pbar.configure(value=0, maximum=100)
        self.btn_run.configure(state="disabled")
        self.btn_preview.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text=("预览中…" if dry else "执行中…"))

        # 跑流水线期间停掉补图：补图的 tk.PhotoImage 会占满主线程的 after() 轮次，
        # worker 线程拿不到 GIL ——表现就是「点了预览一直卡在预览中，日志一个字不出」。
        self._thumb_stop = True
        self.q.put(("log", "[诊断] 准备启动流水线线程…"))
        self.worker = threading.Thread(target=self._run, args=(opts,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.status.configure(text="正在停止…")

    def _run(self, o):
        self.q.put(("log", "[诊断] worker 已启动"))
        try:
            self.pipeline(o)
            self.q.put(("done", "预览完成（未修改任何文件）" if o["dry"] else "全部完成"))
        except Exception as exc:
            self.q.put(("log", "!! 异常: {}".format(exc)))
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("done", "执行中断：{}".format(exc)))
        finally:
            self.q.put(("log", ""))
            # 预览/执行结束后自动刷新表格 —— 不用再手动点「刷新清单」
            self.q.put(("autoscan", True))
            self.q.put(("save_state", True))
            self.q.put(("log", "本次日志已保存：{}".format(self.logpath or "（写日志失败）")))
            try:
                if self.logfile:
                    self.logfile.flush()
            except Exception:
                pass

    # ------------------------------------------------------------ 处理流程
    def pipeline(self, o):
        """执行整条流水线。dry=True 时只打印计划，绝不改动文件。"""
        self._last_steps = []
        dry = o["dry"]
        pfx = "[预览] " if dry else ""
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
        chk = o.get("checked") or []
        if chk:
            allow = set(chk)
            before = len(files)
            files = [p for p in files if str(p) in allow]
            log("{}本次只处理勾选的 {} 个（范围内共 {} 个）".format(pfx, len(files), before))
        self.total = max(1, len(files))
        self.q.put(("total", self.total))
        log("{}待处理文件：{} 个".format(pfx, len(files)))
        if not files:
            return

        # ---------------- 1) 规范命名 ----------------
        if o["rename"]:
            (self._last_steps.append("规范命名") if hasattr(self, "_last_steps") else None)
            log("{}—— 规范命名 ——".format(pfx))
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
                tpl = o.get("template") or DEFAULT_TEMPLATE_POP
                if tpl:
                    try:
                        tpl_name = build_name_by_template(
                            f, tpl, stem=p.stem, ext=p.suffix)
                        if tpl_name:
                            base = tpl_name
                    except Exception as exc:
                        log("  模板套用失败，退回默认：{}".format(exc))
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
                log("{}  {} -> {}".format(pfx, p.name[:60], new.name[:70]))
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
            log("{}命名完成，实际改名 {} 个".format(pfx, n))

        # ---------------- 2) 目录扁平化 ----------------
        if o["flatten"] and o["mode"] == "dir":
            (self._last_steps.append("目录扁平化") if hasattr(self, "_last_steps") else None)
            log("{}—— 目录扁平化 ——".format(pfx))
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
                log("{}  {}/{} -> {}/{}".format(pfx, p.parent.name[:24], p.name[:36],
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
                log("{}扁平化完成，移动 {} 个，清理空目录 {} 个".format(pfx, moved, removed))
            else:
                log("{}扁平化预览结束".format(pfx))

        # ---------------- 2.5) WAV → FLAC ----------------
        if o.get("wav2flac"):
            (self._last_steps.append("WAV→FLAC") if hasattr(self, "_last_steps") else None)
            log("{}—— WAV → FLAC（逐字节无损）——".format(pfx))
            wavs = [p for p in files if p.suffix.lower() == ".wav"]
            if not wavs:
                log("  没有 WAV，跳过")
            else:
                log("  找到 {} 个 WAV，共 {:.0f} MB".format(
                    len(wavs), sum(x.stat().st_size for x in wavs) / 1024 / 1024))
            done_n = fail_n = 0
            for p in wavs:
                if self.stop_flag.is_set():
                    log("已停止")
                    return
                if dry:
                    log("  [计划] {} → {}.flac".format(p.name[:60], p.stem[:60]))
                    self._tick()
                    continue
                try:
                    t0 = time.time()
                    newp = wav_to_flac(p, o.get("ffmpeg") or "ffmpeg", verify=True)
                    a = p.stat().st_size
                    b = newp.stat().st_size
                    log("  [OK] {}  {:.1f} → {:.1f} MB（{:.1f}s，无损校验通过）".format(
                        newp.name[:54], a / 1024 / 1024, b / 1024 / 1024, time.time() - t0))
                    done_n += 1
                except Exception as exc:
                    fail_n += 1
                    log("  [失败] {}：{}".format(p.name[:54], exc))
                self._tick()
            if not dry:
                log("  WAV→FLAC 完成 {} 个，失败 {} 个".format(done_n, fail_n))
                # 后续步骤要作用在 FLAC 上，重新收集一遍
                files = [p for p in collect()]
                log("  后续步骤改为处理 {} 个文件（含新转的 FLAC）".format(len(files)))

        # ---------------- 2.8) 整轨按 CUE 分割 ----------------
        if o.get("cuesplit"):
            (self._last_steps.append("整轨分割") if hasattr(self, "_last_steps") else None)
            log("{}—— 整轨按 CUE 分割 ——".format(pfx))
            # 只看「单文件 cue」：FILE 只有一个，且它指向的文件是本专辑唯一音频
            targets = []
            seen_audio = set()
            for cfile in sorted(root.rglob("*.cue")) if root else []:
                try:
                    fname, trks, dtitle, dperf = parse_cue(cfile)
                except Exception:
                    continue
                if len(trks) < 2:
                    continue
                src = find_cue_audio(cfile, fname)
                if src is None:
                    continue
                # 多文件 cue（每轨一个文件）跳过
                stem = src.stem.lower()
                cue_stem = cfile.stem.lower()
                key = str(src.resolve())        # 按音频去重：同一张专辑可能有多个 cue 副本
                if key in seen_audio:
                    log("  [跳过] {}（同一音频已由另一个 cue 处理）".format(cfile.name[:44]))
                    continue
                # 同目录里若还散落着 01.xxx 这类文件，说明是分轨专辑
                siblings = [x for x in src.parent.iterdir()
                            if x.is_file() and x.suffix.lower() in AUDIO_EXT
                            and x != src
                            and not _is_temp_name(x)]
                # 判据不能是"同目录有别的音频"——整轨目录里常放着试听轨、切到一半的残片。
                # 真正的分轨专辑是"音频个数接近 cue 的曲目数"。
                # 阈值要严：切到 11/16 也会被判成"已分轨"从而永远补不齐（实际踩过）。
                # 只有音频数几乎等于曲目数（≥95%）才认为这张专辑已经是分轨的。
                if siblings and (len(siblings) + 1) >= max(2, int(len(trks) * 0.95)):
                    log("  [跳过] {}（同目录有 {} 个音频，接近 {} 曲，属于分轨专辑）".format(
                        cfile.name[:50], len(siblings), len(trks)))
                    continue
                if siblings:
                    log("  [提示] {} 同目录另有 {} 个音频，但曲目数是 {}，仍按整轨处理".format(
                        cfile.name[:40], len(siblings), len(trks)))
                seen_audio.add(key)
                targets.append((cfile, src, trks, dtitle, siblings))

            if not targets:
                log("  库里没有需要分割的整轨（其余文件都是已分轨的单曲，这个功能不处理）")
            cut_n = skip_n = fail_n2 = 0
            for cfile, src, trks, dtitle, existing in targets:
                log("  {}  {} 曲  {:.0f} MB".format(
                    src.name[:52], len(trks), src.stat().st_size / 1024 / 1024))
                existing = [x for x in (existing or []) if not _is_temp_name(x)]
                partial = 0 < len(existing) < max(2, int(len(trks) * 0.95))
                force_clean = None
                if partial and not dry:
                    # 整轨 + 部分分轨：先问清楚再动手
                    have_n = {x.stem for x in existing}

                    def _missing():
                        miss = []
                        for t in trks:
                            nm = t.get("title") or "track{:02d}".format(t["index"])
                            if not any(nm in h for h in have_n):
                                miss.append("{:02d} {}".format(t["index"], nm))
                        return miss or ["（文件名匹配不上，仍按 cue 补齐）"]

                    try:
                        pick = self._ask_on_main(
                            lambda: self._split_partial_dialog(
                                src.name, len(trks), sorted(have_n),
                                _missing(), src.stat().st_size / 1024 / 1024))
                    except Exception as exc:
                        log("  [提示] 确认窗口出错（{}），按「只补齐」处理".format(exc))
                        pick = "fill_keep"
                    if pick == "skip":
                        log("  [跳过] {}（用户选择跳过）".format(src.name[:52]))
                        self._tick()
                        continue
                    force_clean = (pick == "fill_del")
                    log("  [补切] {}（已有 {} / 共 {}，{}）".format(
                        src.name[:46], len(existing), len(trks),
                        "切完删整轨" if force_clean else "整轨保留"))

                if dry:
                    log("  [计划] 按 cue 切成 {} 个单曲".format(len(trks)))
                    self._tick()
                    continue
                try:
                    t0 = time.time()

                    # 逐轨打日志：切 16 轨要十几秒，一行不写用户不知道在干什么
                    def _on_track(i, total, out_path, trk, note=""):
                        name = Path(out_path).name
                        log("      [{:>2}/{}] {}{}".format(
                            i, total, name[:56], ("  " + note) if note else ""))
                        self._tick()

                    audio, planned = split_cue_album(
                        cfile, o.get("ffmpeg") or "ffmpeg", apply=True, on_track=_on_track)
                    el = time.time() - t0
                    got = len([q for q in planned if Path(q).exists()])
                    log("  [OK] 切成 {}/{} 个单曲（{:.1f}s）".format(got, len(planned), el))
                    cut_n += 1
                    made = [q for q in planned if Path(q).exists()]
                    if len(made) < len(planned):
                        log("  [警告] 应有 {} 轨，实际只生成 {} 轨 —— 原整轨保留未删".format(
                            len(planned), len(made)))
                    else:   # 切完总是问一次：清理还是保留，由用户在确认框里决定
                        # 整轨切完之后，原音频、cue、抓轨日志、封底/碟面图都不再需要
                        sc = self._cue_sidecars(cfile, audio)
                        # 弹收尾窗口（主线程）：可粘曲目表更正名字，再决定清理与否
                        try:
                            pasted, do_clean = self._ask_on_main(
                                lambda: self._split_finish_dialog(
                                    src.name, made, sc,
                                    preset_clean=(force_clean is True)))
                        except Exception as exc:
                            pasted, do_clean = "", False
                            log("  [提示] 收尾窗口出错（{}），按 cue 原样保留".format(exc))

                        # 用户粘了模板 → 按模板改名（用文件名序号对应）
                        if pasted:
                            items = self._parse_track_list(pasted)
                            by_no = {n: t for n, t in items}
                            fixed = 0
                            for q in made:
                                qp = Path(q)
                                m = _re.match(r'^(\d{1,3})', qp.stem)
                                no = int(m.group(1)) if m else 0
                                title = by_no.get(no)
                                if not title:
                                    continue
                                try:
                                    write_tag(qp, "title", title)
                                    new_stem = safe_name("{:02d} {}".format(no, title))
                                    newp = rename_file(qp, new_stem) if qp.stem != new_stem else qp
                                    log("      改名 {} → {}".format(
                                        qp.name[:26], Path(newp).name[:40]))
                                    fixed += 1
                                except Exception as exc:
                                    log("      改名失败 {}：{}".format(qp.name[:30], exc))
                            log("  按模板更正了 {} 个文件名".format(fixed))

                        if do_clean:
                            gone = 0
                            for q in sc:
                                try:
                                    q.unlink()
                                    gone += 1
                                    log("  [删除] {}".format(q.name[:62]))
                                except Exception as exc:
                                    log("  [删除失败] {}：{}".format(q.name[:50], exc))
                            log("  已清理 {}/{} 个文件".format(gone, len(sc)))
                        else:
                            self._mark_split(audio)
                            log("  [保留] {}（未清理，原文件仍在）".format(audio.name[:56]))
                except Exception as exc:
                    fail_n2 += 1
                    log("  [失败] {}：{}".format(cfile.name[:44], exc))
                self._tick()

            # 小结：说清"为什么只动了几张"
            if targets or dry:
                log("  ── 整轨小结：候选 {} 张，实际切 {} 张，失败 {} 张".format(
                    len(targets), cut_n, fail_n2))
                if len(targets) < len(files):
                    log("     （其余 {} 个文件本来就是分轨单曲，本功能不处理）".format(
                        len(files) - len(targets)))
            if not dry:
                files = [p for p in collect()]
                log("  后续步骤改为处理 {} 个文件".format(len(files)))

        # ---------------- 3) 封面内嵌 ----------------
        if o["cover"]:
            (self._last_steps.append("封面内嵌") if hasattr(self, "_last_steps") else None)
            log("{}—— 封面内嵌 ——".format(pfx))
            try:
                from PIL import Image  # noqa: F401  仅探测依赖
            except ImportError:
                log("!! 缺少 Pillow，跳过封面")
            else:
                single = Path(o["cover_path"]) if (o["cover_mode"] == "single" and o["cover_path"]) else None
                if o["cover_mode"] == "single" and not single:
                    log("!! 选了统一图片却没指定文件，改为自动查找")
                n_write = n_have = n_noimg = n_unsup = n_fail = 0
                no_img_dirs = {}      # 目录 -> [文件名]
                unsup_ext = {}        # 扩展名 -> [文件名]
                fail_list = []
                cover_files = list(files)
                if root is not None and o["mode"] == "dir":
                    extra = [q for q in root.rglob("*")
                             if q.is_file() and DOWNMIX_DIRNAME in q.parts
                             and q.suffix.lower() in AUDIO_EXT]
                    if extra:
                        cover_files = cover_files + extra
                        log("  含「{}」目录 {} 个文件（该目录不参与改名/降混，封面仍处理）".format(
                            DOWNMIX_DIRNAME, len(extra)))
                log("  口径：写入=本次写好｜已有封面=本来就有的正确跳过(已完成)"
                    "｜无图=目录链上没有可用图片(没做，不是跳过)｜不支持格式=不写该格式(不算失败)｜失败=读写出错")
                for p in cover_files:
                    if self.stop_flag.is_set():
                        log("已停止")
                        return
                    ext = p.suffix.lower()
                    rel = str(p)
                    try:
                        have = has_cover(p)
                    except Exception as exc:
                        n_fail += 1
                        fail_list.append((rel, "读取标签失败: {}".format(exc)))
                        self._tick()
                        continue
                    if have is None:
                        n_unsup += 1
                        unsup_ext.setdefault(ext, []).append(rel)
                        self._tick()
                        continue
                    if have:
                        n_have += 1
                        self._tick()
                        continue
                    img, why, io_err = (single, "界面指定", False) if single else find_cover_image(p, root)
                    if img is None:
                        if io_err:
                            n_fail += 1
                            fail_list.append((rel, why))
                        else:
                            n_noimg += 1
                            d = str(p.parent)
                            no_img_dirs.setdefault(d, []).append(p.name)
                        self._tick()
                        continue
                    try:
                        if not dry:
                            embed_cover(p, img)
                        n_write += 1
                        log("  封面{} {} ← {} [{}]".format(
                            "将写入" if dry else "写入", p.name[:40], img.name[:28], why))
                    except Exception as exc:
                        n_fail += 1
                        fail_list.append((rel, "{}: {}".format(why, exc)))
                        log("  封面失败 {}: {}".format(p.name[:40], exc))
                    self._tick()
                log("")
                log("{}封面完成：写入 {}｜已有封面 {}｜无图 {}｜不支持格式 {}｜失败 {}".format(
                    pfx, n_write, n_have, n_noimg, n_unsup, n_fail))
                # 下面每一条都下钻到具体文件，不截断
                for ext in sorted(unsup_ext):
                    fl = unsup_ext[ext]
                    log("  不支持格式 {}（{} 个，本工具不写该格式封面，不算失败）：".format(ext, len(fl)))
                    for f in fl:
                        log("    {}".format(f))
                for d in sorted(no_img_dirs, key=lambda k: -len(no_img_dirs[k])):
                    fl = no_img_dirs[d]
                    log("  无图目录 {}（{} 个文件，这些是「没做」）：".format(d, len(fl)))
                    for f in fl:
                        log("    {}".format(f))
                for f, why in fail_list:
                    log("  真失败 {} -> {}".format(f, why))

        # ---------------- 4) 5.1 降混 ----------------
        if o["downmix"]:
            (self._last_steps.append("5.1 降混") if hasattr(self, "_last_steps") else None)
            log("{}—— 5.1 → 立体声 ——".format(pfx))
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
                    log("{}  {}/{} → {}/".format(pfx, p.parent.name[:24], p.name[:40], DOWNMIX_DIRNAME))
                    if dry:
                        cnt += 1
                    else:
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            r = run_hidden([o["ffmpeg"], "-y", "-v", "error", "-i", str(p),
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
                log("{}降混完成 {} 个，失败 {} 个".format(pfx, cnt, fail))

        # ---------------- 4.5) 歌词内嵌 ----------------
        if o.get("lyrics"):
            (self._last_steps.append("歌词内嵌") if hasattr(self, "_last_steps") else None)
        if o.get("lyrics"):
            log("{}—— 歌词内嵌 ——".format(pfx))
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
                # 多源查询：LRCLIB → 网易云 → QQ音乐 → Lyrics.ovh
                synced = plain = ""
                src_name = ""
                try:
                    if lyrics_multi is not None:
                        dur = f.info.length if f.info.length else None
                        text, src_name, has_lrc = lyrics_multi.fetch_lyrics(
                            title, artist, album, dur)
                        if text:
                            if has_lrc:
                                synced = text
                            else:
                                plain = text
                    else:
                        cands = _lrclib_query(artist, title, album)
                        hit = _pick(cands, f.info.length if f.info.length else None)
                        if hit is not None:
                            synced = hit.get("syncedLyrics") or ""
                            plain = hit.get("plainLyrics") or ""
                            src_name = "LRCLIB"
                except Exception as exc:
                    fail += 1
                    log("  歌词查询异常: {}".format(exc))
                    self._tick()
                    continue
                if not (synced or plain):
                    miss += 1
                    log("{}  未找到歌词: {}".format(pfx, p.name[:52]))
                    self._tick()
                    continue
                log("{}  {} <- {}（{}）".format(
                    pfx, p.name[:42], (src_name or "?"),
                    "带时间轴" if synced else "纯文本"))
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
                self._tick()
            log("{}歌词完成：写入 {}，已有 {}，未找到 {}，失败 {}".format(
                pfx, got, have, miss, fail))

        # ---------------- 5) 清理旧文件 ----------------
        if o["cleanup"] and o["mode"] == "dir":
            (self._last_steps.append("清理旧文件") if hasattr(self, "_last_steps") else None)
            log("{}—— 清理旧文件 ——".format(pfx))
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
                        log("{}  保留（无对应新目录）：{}".format(pfx, od.name[:46]))
                        kept += len(two) + len(six)
                        continue
                    nd, ncnt = hit
                    mc = nd / DOWNMIX_DIRNAME
                    mcnt = len(list(mc.glob("*.flac"))) if mc.exists() else 0
                    targets = []
                    if ncnt >= len(two):
                        targets += two
                    else:
                        log("{}  保留（新集不足 {}<{}）：{}".format(pfx, ncnt, len(two), od.name[:40]))
                        kept += len(two)
                    if six:
                        if mcnt >= len(six):
                            targets += six
                        else:
                            log("{}  保留（降混不足 {}<{}）：{}".format(pfx, mcnt, len(six), od.name[:40]))
                            kept += len(six)
                    for q in targets:
                        log("{}  删除 {}".format(pfx, q.relative_to(root)))
                        if dry:
                            gone += 1
                        else:
                            try:
                                q.unlink()
                                gone += 1
                            except Exception as exc:
                                log("  删除失败: {}".format(exc))
            log("{}清理完成：删除 {}，保留 {}（原因见上）".format(pfx, gone, kept))

        # ---------------- 6) 复制到指定目录 ----------------
        if o["copy"] and o["out"]:
            log("{}—— 复制到指定目录 ——".format(pfx))
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
                log("{}  {} -> {}".format(pfx, p.name[:44], str(rel.parent)))
                if not dry:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if not dst.exists():
                            shutil.copy2(p, dst)
                            n += 1
                    except Exception as exc:
                        log("  复制失败: {}".format(exc))
                self._tick()
            log("{}复制完成 {} 个 -> {}".format(pfx, n, out))

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

    # ------------------------------------------------------------ 状态记忆
    def _collect_state(self):
        """把界面上需要记住的东西收集成一个 dict。"""
        st = {
            "mode": self.mode.get(),
            "src": self.src_var.get().strip(),
            "files": [str(x) for x in self.files],
            "template": self.name_tpl.get().strip(),
            "cover_mode": self.cover_mode.get(),
            "cover_path": self.cover_path.get().strip(),
            "out": self.out_var.get().strip(),
            "flags": {
                "rename": self.v_rename.get(), "flatten": self.v_flatten.get(),
                "cover": self.v_cover.get(), "downmix": self.v_downmix.get(),
                "lyrics": self.v_lyrics.get(), "cleanup": self.v_cleanup.get(),
                "copy": self.v_copy.get(),
            },
            "syncname": bool(self.v_syncname.get()) if hasattr(self, "v_syncname") else True,
            "checked": dict(getattr(self, "_check_state", {})),
            "cols": {c: self.tree.column(c, "width") for c in COLS},
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return st

    def _save_state(self, now=False):
        """写 state.json；连续改动用防抖，避免每次点勾选都写盘。"""
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass  # allow-swallow: 窗口销毁后取消回调本来就会失败
            self._save_job = None
        if not now:
            self._save_job = self.root.after(800, lambda: self._save_state(True))
            return
        self._save_job = None
        try:
            sp, _cp = _state_paths()
            sp.write_text(json.dumps(self._collect_state(), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            # 不许静默：状态存不下来必须能被看见（曾因漏 import json 导致整段静默失效）
            try:
                self.log("[状态] 保存失败：{}: {}".format(type(exc).__name__, exc))
                self.status.configure(text="状态保存失败：{}".format(exc))
            except Exception:
                pass  # allow-swallow: 连日志都写不了时无计可施

    def _save_cache(self, rows):
        """把清单缓存下来（含小缩略图），下次启动直接显示，不用重扫。"""
        try:
            _sp, cp = _state_paths()
            data = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}
            cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.log("[缓存] 写清单缓存失败（不影响使用）：{}".format(exc))

    def _load_cache(self):
        try:
            _sp, cp = _state_paths()
            if not cp.is_file():
                return None
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _restore_state(self):
        """启动时恢复上次的来源、选项、清单与勾选。"""
        try:
            sp, _cp = _state_paths()
            if not sp.is_file():
                return
            st = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return

        try:
            if st.get("mode"):
                self.mode.set(st["mode"])
                self._toggle_mode()
            if st.get("src"):
                self.src_var.set(st["src"])
                self.sel_info.configure(text="上次使用：{}".format(st["src"]))
            fl = st.get("files") or []
            if fl:
                self.files = [Path(x) for x in fl if Path(x).exists()]
            if st.get("template"):
                self.name_tpl.set(st["template"])
            if st.get("cover_mode"):
                self.cover_mode.set(st["cover_mode"])
            if st.get("cover_path"):
                self.cover_path.set(st["cover_path"])
            if st.get("out"):
                self.out_var.set(st["out"])
            flags = st.get("flags") or {}
            for k, var in (("rename", self.v_rename), ("flatten", self.v_flatten),
                           ("cover", self.v_cover), ("downmix", self.v_downmix),
                           ("lyrics", self.v_lyrics), ("cleanup", self.v_cleanup),
        ("wav2flac", self.v_wav2flac), ("cuesplit", self.v_cuesplit),
                           ("copy", self.v_copy)):
                if k in flags:
                    var.set(bool(flags[k]))
            self._toggle_copy()
            if "syncname" in st and hasattr(self, "v_syncname"):
                self.v_syncname.set(bool(st["syncname"]))
            if st.get("checked"):
                self._check_state = dict(st["checked"])
            for col, wdt in (st.get("cols") or {}).items():
                if col in COLS and wdt:
                    try:
                        self.tree.column(col, width=int(wdt))
                    except Exception:
                        pass
        except Exception:
            pass

        # 恢复清单（不重扫）
        cache = self._load_cache()
        if st.get("src") and not (cache and cache.get("rows")):
            self.status.configure(
                text="上次用的是 {} — 点「刷新清单」加载清单".format(st["src"]))
        if cache and cache.get("rows"):
            try:
                self._fill_table([tuple(r) for r in cache["rows"]])
                self.status.configure(
                    text="已恢复上次清单（{}，共 {} 行）— 点「刷新清单」可重新扫描".format(
                        cache.get("ts", "?"), len(cache["rows"])))
                self.log("[恢复] 上次清单已恢复，共 {} 行（{}）".format(
                    len(cache["rows"]), cache.get("ts", "?")))
            except Exception as exc:
                self.log("[恢复] 清单缓存读取失败：{}".format(exc))

    def on_close(self):
        """关窗口前保存状态与清单。"""
        try:
            self._sync_check_state()
            self._save_state(now=True)
            rows = []
            for iid in self.tree.get_children():
                p = self._row_path(iid)
                if p is None:
                    continue
                v = list(self.tree.item(iid, "values"))
                small = ""
                ph = self._thumbs.get(iid)
                if ph is not None:
                    small = getattr(ph, "_b64", "") or ""
                rows.append((str(p), v, small, ""))
            if rows:
                self._save_cache(rows)
        except Exception:
            pass  # allow-swallow: 关窗时存清单失败，不影响音频文件本身
        try:
            if self.logfile:
                self.logfile.close()
        except Exception:
            pass  # allow-swallow: 关窗时关日志文件，失败无后果
        self.root.destroy()

    # ------------------------------------------------------------ 队列
    def _poll(self):
        """消费后台队列（外层包了异常保底，抛异常也继续轮询）。"""
        try:
            self._poll_once()
        except Exception as exc:
            try:
                self.txt.insert("end", "\n[轮询异常] {}\n".format(exc))
            except Exception:
                pass  # allow-swallow: 连日志都写不了时只能跳过
            self._poll_job = self.root.after(300, self._poll)

    def _poll_once(self):
        """消费后台队列。

        节流三条（2026-11-30）：
          ①日志攒成一批，一次 insert + 一次 see；
          ②进度只取最后一条；
          ③**缩略图单独限速**——它是重活（每条 tk.PhotoImage 10-20ms），
            一轮只做 20 个，剩下的塞回队列。不这么做的话，扫描完 3759 条
            thumb 会把主线程连续占满，点任何按钮都是"未响应"。
        """
        # 先处理「后台线程请求主线程做的事」（弹窗等）
        ui_q = getattr(self, "_ui_requests", None)
        if ui_q is not None:
            for _ in range(4):      # 一轮最多 4 个，避免单个窗口挡住其它更新
                try:
                    fn, box, done = ui_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    box["v"] = fn()
                except Exception as exc:
                    box["e"] = exc
                finally:
                    done.set()

        logs = []
        last_tick = None
        last_total = None
        thumbs = []
        n = 0
        more = False
        try:
            while n < 300:
                kind, payload = self.q.get_nowait()
                n += 1
                if kind == "log":
                    logs.append(payload)
                elif kind == "total":
                    last_total = payload
                elif kind == "tick":
                    last_tick = payload
                elif kind == "tablescan":
                    done, total, rows = payload
                    if rows is not None:
                        self._fill_table(rows)
                    else:
                        self.summary.configure(text="扫描中… {}/{}".format(done, total))
                elif kind == "thumb_done":
                    self._thumb_job = None
                    t = getattr(self, "_thumb_total", 0)
                    logs.append("[缩略图] {} 张已全部生成".format(t))
                elif kind == "save_state":
                    self._save_state()
                elif kind == "autoscan":
                    self._auto_refresh_table()
                elif kind == "lyric":
                    try:
                        self.ly_status.configure(text=payload)
                    except Exception:
                        pass  # allow-swallow: 歌词页签未初始化时忽略
                elif kind == "done":
                    # 把这轮到底做了什么列出来 —— 用户看到"待处理 2464 个"却只动了一张，
                    # 会觉得"怎么处理一个就完成了"（2026-11-30 实际发生）。
                    done_steps = getattr(self, "_last_steps", [])
                    if done_steps:
                        logs.append("本次执行的步骤：" + "、".join(done_steps))
                    self.status.configure(text=payload)
                    self.btn_run.configure(state="normal")
                    self.btn_preview.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    # 流水线结束，恢复补图
                    self._thumb_stop = False
                    self._thumb_job = None
                    try:
                        self._start_thumb_worker()
                    except Exception:
                        pass  # allow-swallow: 补图恢复失败不影响主流程
                    logs.append("\n==== {} ====".format(payload))
        except queue.Empty:
            pass
        else:
            more = True

        # ---- 提交日志 / 进度 ----
        if logs:
            self.txt.insert("end", "\n".join(logs) + "\n")
            self.txt.see("end")
            try:
                total_lines = int(self.txt.index("end-1c").split(".")[0])
                if total_lines > 6000:
                    self.txt.delete("1.0", "1500.0")
            except Exception:
                pass  # allow-swallow: 行数取不到时不影响日志继续写
        if last_total is not None:
            self.pbar.configure(maximum=max(1, last_total), value=0)
        if last_tick is not None:
            self.pbar.configure(value=last_tick)
            self.status.configure(
                text="进度 {}/{}".format(last_tick, int(self.pbar["maximum"])))

        # ---- 缩略图：从**独立队列**取，每轮最多 20 个 ----
        pending_thumb = False
        q_t = getattr(self, "q_thumb", None)
        if q_t is not None:
            quota = 10
            for _ in range(quota):
                try:
                    iid, b64 = q_t.get_nowait()
                except queue.Empty:
                    break
                try:
                    ph = tk.PhotoImage(data=b64)
                    ph._b64 = b64
                    self.tree.item(iid, image=ph)
                    self._thumbs[iid] = ph
                    done_n = getattr(self, "_thumb_done", 0)
                    total_n = getattr(self, "_thumb_total", 0)
                    if total_n and done_n % 20 < 4:
                        self.status.configure(
                            text="缩略图 {}/{}…（清单已可用）".format(done_n, total_n))
                except Exception:
                    pass  # allow-swallow: 单行缩略图失败不影响其它行
            if not q_t.empty():
                pending_thumb = True

        # ---- 调度 ----
        if pending_thumb:
            self._poll_job = self.root.after(60, self._poll)
        else:
            self._poll_job = self.root.after(1 if more else 120, self._poll)




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
