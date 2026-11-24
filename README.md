# musiclib

面向 DAP（如 xDuoo X20）与本地音乐库的批量整理工具。**0.1 alpha**，功能可用但未经大规模验证，遇到问题请开 Issue。

- 图形界面（tkinter），也可命令行调用
- 所有会改动文件的动作**默认 dry-run**，必须显式 `--apply` 才执行
- 删除类操作用「覆盖校验 + 条数比对」，新集少于旧集一律保留

## 功能

| 子命令 / 按钮 | 作用 |
|---|---|
| `scan` | 扫描统计：格式、采样率、位深、声道、封面、大小 → `scan.md` / `scan.csv` |
| `rename` | 规范命名，去序号：普通 `音乐名 - 歌手 - 专辑`，古典 `标题 - 作曲家` |
| `flatten` | 目录扁平化：把子目录里的音频提到专辑第一层，可清理空目录 |
| `cover` | 封面内嵌：同目录图片 / 统一图片 / 向上找父目录图片 |
| `downmix` | 5.1 → 立体声（ITU-R BS.775 权重，−3 dB 防削波） |
| `cleanup` | 旧文件清理（需覆盖校验通过） |
| `lyrics` | 批量取歌词（源 [lrclib.net](https://lrclib.net)，免登录）写入 FLAC 的 `LYRICS` 标签，可另存 `.lrc` |`n| `verify` | 库体检：0Hz/0bit、无封面、空目录、ISO/DSD 残留 |
| `fixall.py` | 全库批量：跨格式改名（含清洗标签内曲号）+ 批量补封面 |

## 安装

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

`downmix` 需要 **ffmpeg**：放到 `bin/ffmpeg.exe`，或加进 PATH，或用 `--ffmpeg PATH` 指定。

## 使用

### 图形界面

```bash
python musiclib_gui.py
```

1. 选来源（整个文件夹 / 手动多选文件）
2. 勾要做的处理
3. 选输出位置（可复制到指定目录）
4. **先点「预览」看清单**，确认无误再点「开始执行」

### 命令行

```bash
python musiclib.py scan    --root /mnt/sd
python musiclib.py rename  --root /mnt/sd --apply
python musiclib.py flatten --root /mnt/sd --apply --prune-empty
python musiclib.py cover   --root /mnt/sd --image cover.jpg --apply
python musiclib.py downmix --root /mnt/sd --apply --delete-source
python musiclib.py cleanup --root /mnt/sd --old-rate 44100 --apply --force
python musiclib.py verify  --root /mnt/sd`npython musiclib.py lyrics  --root /mnt/sd --apply --save-lrc
```

全库批量（推荐用这个，覆盖 mp3/dsf/wav）：

```bash
python fixall.py report --root /mnt/sd
python fixall.py rename --root /mnt/sd --apply
python fixall.py cover  --root /mnt/sd --apply
```

## 安全设计

- `scan` / `verify` / `report` 只读
- 其余子命令默认 dry-run，`--apply` 才落盘
- `cleanup` 需 `--apply` + `--force`，且按**声道分别计数**：新目录立体声数 ≥ 旧目录才删
- 改名冲突自动加 ` (2)`，目标已存在则跳过并记日志
- 每步可 `--log x.jsonl` 留痕

## 打包 exe

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name musiclib `
  --hidden-import mutagen --hidden-import mutagen.flac `
  --hidden-import PIL --hidden-import PIL.Image musiclib_gui.py
```

把 `musiclib.exe` 与 `bin\ffmpeg.exe` 放同一目录即可分发。

## 已知问题（0.1）

- 只对 **FLAC** 做封面内嵌与降混；mp3/dsf 仅改名与统计
- 古典命名只能到「标题 - 作曲家」：opus / 曲式 / 调号 / 曲号 标签里通常不存在，工具不编造
- 大库在 U 盘上跑封面很慢（整文件重写，约 7 个/分钟），属物理限制
- 设备中途掉线时，早期版本会把「目录不可读」误记为「无图」，已在 0.1 修正为记失败

'## 歌词

`lyrics` 用 lrclib.net 的公开 API（无需登录）。匹配依据：标题 + 歌手 + 专辑，并按**时长差 ≤10 秒**过滤，避免配错歌。命中的同步歌词写入 FLAC 的 `LYRICS` 标签（DAP 可直接显示滚动歌词），纯文本写 `UNSYNCEDLYRICS`；`--save-lrc` 额外存同名 `.lrc`。

官方建议每秒不超过 1 次请求，工具内置 1 秒间隔。

## 许可'

MIT，见 [LICENSE](LICENSE)。
