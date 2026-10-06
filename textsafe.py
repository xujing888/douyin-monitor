# -*- coding: utf-8 -*-
"""展示层文本清洗（Tk 无字体回退，缺字形字符会渲染成方框 □）。

界面字体是 Microsoft YaHei UI，字体表里没有字形的字符 —— emoji、变体选择符
（U+FE0E/FE0F）、数学粗体字母（𝐒𝐩𝐫𝐢𝐧𝐠）、他国文字（ᐛ 𓏽 ᜊ）等 —— 在 Tk 里
一律显示为方框。本模块按「字体 cmap 是否包含该码位」判定并剔除。

只作用于界面显示，不改动数据库 / 磁盘上的原始数据。
字体表解析失败时原样返回，绝不抛异常。
"""
import os
import re
import struct

_CMAP = None
_CACHE = {}
_WS = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")


def _parse_cmap(path):
    """从 ttf/ttc 里读出 cmap 覆盖的全部码位（优先 format 12，退回 format 4）"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"ttcf":                     # TrueType Collection
        num = struct.unpack(">I", data[8:12])[0]
        offsets = struct.unpack(">%dI" % num, data[12:12 + 4 * num])
        base = offsets[0]
    else:
        base = 0
    num_tables = struct.unpack(">H", data[base + 4:base + 6])[0]
    cmap_off = None
    for i in range(num_tables):
        rec = base + 12 + 16 * i
        if data[rec:rec + 4] == b"cmap":
            cmap_off = struct.unpack(">I", data[rec + 8:rec + 12])[0]
            break
    if cmap_off is None:
        return None
    n = struct.unpack(">H", data[cmap_off + 2:cmap_off + 4])[0]
    subs = {}
    for i in range(n):
        rec = cmap_off + 4 + 8 * i
        pid, eid = struct.unpack(">HH", data[rec:rec + 4])
        subs[(pid, eid)] = cmap_off + struct.unpack(">I", data[rec + 4:rec + 8])[0]
    cps = set()
    for key in ((3, 10), (0, 4), (0, 6), (3, 1), (0, 3)):
        p = subs.get(key)
        if p is None:
            continue
        fmt = struct.unpack(">H", data[p:p + 2])[0]
        if fmt == 12:
            ng = struct.unpack(">I", data[p + 12:p + 16])[0]
            for g in range(ng):
                r = p + 16 + 12 * g
                s, e = struct.unpack(">II", data[r:r + 8])
                cps.update(range(s, e + 1))
            break
        if fmt == 4:
            segx2 = struct.unpack(">H", data[p + 6:p + 8])[0]
            seg = segx2 // 2
            end = struct.unpack(">%dH" % seg, data[p + 14:p + 14 + segx2])
            st = p + 16 + segx2
            start = struct.unpack(">%dH" % seg, data[st:st + segx2])
            for s, e in zip(start, end):
                if s != 0xFFFF and s <= e:
                    cps.update(range(s, e + 1))
            break
    return cps or None


def _cmap():
    global _CMAP
    if _CMAP is not None:
        return _CMAP
    _CMAP = set()
    try:
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        for fn in ("msyh.ttc", "msyh.ttf", "simhei.ttf"):
            p = os.path.join(fonts, fn)
            if os.path.isfile(p):
                got = _parse_cmap(p)
                if got:
                    _CMAP = got
                    break
    except Exception:
        _CMAP = set()
    return _CMAP


def disp(s):
    """剔除当前界面字体无字形的字符（防止显示方框）。解析失败则原样返回。"""
    try:
        t = "" if s is None else str(s)
        if not t:
            return t
        cm = _cmap()
        if not cm:
            return t
        hit = _CACHE.get(t)
        if hit is not None:
            return hit
        out = "".join(ch for ch in t if ord(ch) < 0x80 or ord(ch) in cm)
        if not out.strip():
            out = t          # 全是无字形字符时保留原文，避免整行空白
        else:
            out = _WS.sub(" ", out).strip()   # 剔除后留下的双空格归一
        if len(_CACHE) < 8192:
            _CACHE[t] = out
        return out
    except Exception:
        return "" if s is None else str(s)
