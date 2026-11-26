# -*- coding: utf-8 -*-
"""多源歌词抓取 —— 统一入口 fetch_lyrics()。

数据源（按命中率与稳定度排序）：
  1. LRCLIB        https://lrclib.net/api/search      开源、无鉴权、带时间轴
  2. 网易云音乐     music.163.com/api                  中文曲库最全
  3. QQ 音乐        c.y.qq.com                         中文补充
  4. Lyrics.ovh    api.lyrics.ovh                      英文补充

设计要点：
· 每个源都要**匹配校验**——标题/艺术家对不上就不用，宁可没结果也不要贴错歌词；
· 统一返回 LRC 或纯文本 + 来源名，调用方不用关心是哪个源；
· 每源独立超时，一个挂不影响其它；
· 全程用标准库 urllib，不给打包增加依赖。
"""
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 12


# ---------------------------------------------------------------- 文本归一化
_BRACKETS = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
_JUNK = re.compile(r"[\s\-_·・.,，。、!！?？:：;；'\"“”‘’/\\|]+")


def _norm(s):
    """归一化：去括号补充说明、去标点空格、转小写、全角转半角。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _BRACKETS.sub("", s)
    s = _JUNK.sub("", s)
    return s.lower()


def _sim(a, b):
    """0~1 的相似度（归一化后比较，含包含关系）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    # 字符集合重合度，够用且便宜
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def _looks_like_lrc(text):
    return bool(re.search(r"^\[\d{1,2}:\d{2}", (text or "").strip()[:200], re.M))


# ---------------------------------------------------------------- 各数据源
def _src_lrclib(title, artist, album, duration):
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "musiclib/0.1 (https://github.com/minterinz/musiclib)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    best, best_score = None, 0.0
    for c in (data or [])[:15]:
        lyric = c.get("syncedLyrics") or c.get("plainLyrics") or ""
        if not lyric.strip():
            continue
        sc = _sim(title, c.get("trackName")) * 0.6 + _sim(artist, c.get("artistName")) * 0.4
        if duration:
            try:
                d = abs(float(c.get("duration") or 0) - float(duration))
                if d < 3:
                    sc += 0.1
                elif d > 15:
                    sc -= 0.2
            except Exception:
                pass
        if sc > best_score:
            best, best_score = lyric, sc
    if best and best_score >= 0.5:
        return best, "LRCLIB", best_score
    return None


def _src_netease(title, artist, duration):
    q = urllib.parse.quote("{} {}".format(title, artist))
    url = ("https://music.163.com/api/search/get?s={}&type=1&limit=8".format(q))
    req = urllib.request.Request(url, headers={
        "Referer": "https://music.163.com", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    songs = ((j or {}).get("result") or {}).get("songs") or []
    pick, pick_score = None, 0.0
    for s in songs:
        names = " ".join(a.get("name", "") for a in (s.get("artists") or []))
        sc = _sim(title, s.get("name")) * 0.65 + _sim(artist, names) * 0.35
        if sc > pick_score:
            pick, pick_score = s, sc
    if not pick or pick_score < 0.5:
        return None
    sid = pick.get("id")
    url2 = ("https://music.163.com/api/song/lyric?id={}&lv=1&kv=1&tv=-1".format(sid))
    req2 = urllib.request.Request(url2, headers={
        "Referer": "https://music.163.com", "User-Agent": UA})
    with urllib.request.urlopen(req2, timeout=TIMEOUT) as r:
        j2 = json.loads(r.read().decode("utf-8", "replace"))
    lyric = ((j2 or {}).get("lrc") or {}).get("lyric") or ""
    if not lyric.strip():
        return None
    return lyric, "网易云", pick_score


def _src_qq(title, artist):
    q = urllib.parse.quote("{} {}".format(title, artist))
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
           "?w={}&format=json&n=8&p=1".format(q))
    req = urllib.request.Request(url, headers={
        "Referer": "https://y.qq.com", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    songs = (((j or {}).get("data") or {}).get("song") or {}).get("list") or []
    pick, pick_score = None, 0.0
    for s in songs:
        names = " ".join(a.get("name", "") for a in (s.get("singer") or []))
        sc = _sim(title, s.get("songname")) * 0.65 + _sim(artist, names) * 0.35
        if sc > pick_score:
            pick, pick_score = s, sc
    if not pick or pick_score < 0.5:
        return None
    mid = pick.get("songmid")
    if not mid:
        return None
    url2 = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
            "?songmid={}&format=json&nobase64=1&g_tk=5381".format(mid))
    req2 = urllib.request.Request(url2, headers={
        "Referer": "https://y.qq.com", "User-Agent": UA})
    with urllib.request.urlopen(req2, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8", "replace")
    raw = re.sub(r"^MusicJsonCallback\(|\)$", "", raw.strip())
    j2 = json.loads(raw)
    lyric = (j2 or {}).get("lyric") or ""
    # QQ 返回的是 HTML 实体
    import html
    lyric = html.unescape(lyric)
    if not lyric.strip():
        return None
    return lyric, "QQ音乐", pick_score


def _src_lyrics_ovh(title, artist):
    url = "https://api.lyrics.ovh/v1/{}/{}".format(
        urllib.parse.quote(artist), urllib.parse.quote(title))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    lyric = (j or {}).get("lyrics") or ""
    return (lyric, "Lyrics.ovh", 0.6) if lyric.strip() else None


# ---------------------------------------------------------------- 统一入口
SOURCES = [
    ("LRCLIB", lambda t, a, al, d: _src_lrclib(t, a, al, d)),
    ("网易云", lambda t, a, al, d: _src_netease(t, a, d)),
    ("QQ音乐", lambda t, a, al, d: _src_qq(t, a)),
    ("Lyrics.ovh", lambda t, a, al, d: _src_lyrics_ovh(t, a)),
]


def fetch_lyrics(title, artist, album="", duration=None, prefer_lrc=True, log=None):
    """按顺序问各个源，返回 (歌词文本, 来源名, 是否带时间轴)；全失败返回 (None, None, False)。

    prefer_lrc=True 时，若某源只给纯文本会先记住，继续找带时间轴的；
    找完仍没有就退回纯文本。
    """
    if not (title or "").strip():
        return None, None, False
    plain_fallback = None
    for name, fn in SOURCES:
        try:
            hit = fn(title, artist or "", album or "", duration)
        except Exception as exc:
            if log:
                log("    [歌词] {} 查询失败：{}".format(name, str(exc)[:60]))
            continue
        if not hit:
            if log:
                log("    [歌词] {} 没找到".format(name))
            continue
        text, src, score = hit
        has_lrc = _looks_like_lrc(text)
        if has_lrc or not prefer_lrc:
            if log:
                log("    [歌词] {} 命中（{:.0%}{}）".format(
                    src, score, "，带时间轴" if has_lrc else ""))
            return text, src, has_lrc
        if plain_fallback is None:
            plain_fallback = (text, src, False)
        if log:
            log("    [歌词] {} 只有纯文本，继续找带时间轴的".format(src))
    if plain_fallback:
        if log:
            log("    [歌词] 退回纯文本（{}）".format(plain_fallback[1]))
        return plain_fallback
    return None, None, False


def fetch_lyrics_lrc(title, artist, album="", duration=None, log=None):
    """只要带时间轴的 LRC；没有就返回 (None, None)。"""
    text, src, has = fetch_lyrics(title, artist, album, duration, prefer_lrc=True, log=log)
    return (text, src) if has else (None, None)


if __name__ == "__main__":
    for t, a in [("晴天", "周杰伦"), ("Yellow", "Coldplay"), ("后来的我们", "五月天"),
                 ("Hotel California", "Eagles")]:
        print("=" * 70)
        print("查询:", t, "/", a)
        txt, src, has = fetch_lyrics(t, a, log=print)
        if txt:
            print("  来源:", src, "| 时间轴:", has, "| 长度:", len(txt))
            print("  前 3 行:", " / ".join(txt.strip().splitlines()[:3])[:120])
        else:
            print("  没找到")
        time.sleep(1)
