"""抖音采集系统 - CustomTkinter 桌面界面 v2。

2026-10-06 全量重写（对齐 Web 版 static/index.html）：
- 架构：去掉本地 HTTP 服务，全部经 api_bridge 进程内直调业务函数；
- 页面：作者监控 / 作品与下载 / 文件管理 / 同步日志 / 参数设置；
- 作者卡片：IP 徽章、四指标格（含当日涨跌）、有变化徽章、趋势弹窗（Canvas 自绘折线）；
- 文件管理为新增页（作者汇总 -> 作品文件行、批量删除、打开目录）。
"""
import datetime
import os
import queue
import re
import threading
import time
import tkinter
import tkinter.filedialog as fd
import tkinter.messagebox as mb

import customtkinter as ctk
import httpx
from PIL import Image, ImageGrab, ImageTk

try:                      # 内嵌视频预览（无声音）；缺库时优雅降级
    import cv2
except Exception:         # pragma: no cover
    cv2 = None

from api_bridge import call
from app import config, db
from textsafe import disp

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ---- 配色 ----
C_BG = "#15181d"        # 窗口底
C_SIDEBAR = "#1b1f26"   # 侧边栏
C_PANEL = "#222730"     # 卡片/面板
C_PANEL2 = "#2a303b"    # 行/次级面板
C_PRIMARY = "#3b82f6"   # 主色
C_PRIMARY_HOVER = "#2f6fd6"
C_TEXT = "#e5e7eb"
C_MUTED = "#9ca3af"
C_UP = "#ef4444"        # 涨（红）
C_DOWN = "#22c55e"      # 跌（绿）
C_OK = "#22c55e"
C_ERR = "#ef4444"
C_WARN = "#f59e0b"
C_IP_FG = "#c4b5fd"     # IP 徽章字色（紫）
C_IP_BG = "#2b2342"
C_BTN = C_PANEL2
C_BTN_HOVER = "#343b48"

PAGES = ["作者监控", "作品与下载", "文件管理", "同步日志", "参数设置"]

FONT = "Microsoft YaHei UI"

TREND_RANGES = [("day", "日"), ("week", "周"), ("month", "月"), ("quarter", "季"), ("year", "年")]
TREND_RANGE_TEXT = {"day": "今日", "week": "近7天", "month": "近30天",
                    "quarter": "近90天", "year": "近1年"}
TREND_METRICS = [("follower_count", "粉丝", "#409eff"),
                 ("following_count", "关注", "#a78bfa"),
                 ("total_digg", "获赞", "#e6a23c"),
                 ("content_count", "作品数", "#67c23a")]

STEP_NAME = {"profiles": "作者资料", "posts": "作品抓取", "downloads": "文件回拉"}

# 预览支持的类型（与 Web 端 static/index.html 的 PV_IMG / PV_VID 一致）
PV_IMG = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"}
PV_VID = {"mp4", "webm", "mov", "m4v"}
COVER_DIR = os.path.join(os.path.dirname(os.path.abspath(config.MEDIA_DIR)), "_covers")


def fmt_n(v):
    if v is None:
        return "-"
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.1f}w"
    return str(v)


def fmt_ts(s, with_sec=False):
    """ISO 时间串 -> MM-DD HH:MM(:SS)"""
    if not s:
        return "-"
    s = str(s).replace("T", " ")
    if len(s) >= 16:
        return s[5:16] if not with_sec else s[5:19]
    return s


def human_b(b):
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        return "-"
    return f"{b / 1048576:.1f} MB"


class PreviewWindow(ctk.CTkToplevel):
    """软件内预览弹窗（对齐 Web 端 pvModal）

    - 图片：内嵌显示，随窗口缩放；
    - 视频：cv2 逐帧解码内嵌播放（无声音），放完自动进下一个文件；
    - ←/→ 键、鼠标滚轮、按钮跨作品连续切换；Esc 关闭。
    """

    def __init__(self, master, flat, idx=0, on_open_file=None):
        super().__init__(master)
        self.flat = list(flat or [])
        self.idx = max(0, min(int(idx), len(self.flat) - 1)) if self.flat else 0
        self.on_open_file = on_open_file
        self._cap = None
        self._playing = False
        self._stop = threading.Event()
        self._q = queue.Queue(maxsize=3)
        self._seek_to = None
        self._total = 0
        self._fps = 25.0
        self._cur_img = None
        self._photo = None
        self._wheel_ts = 0.0

        self.title("预览")
        self.geometry("1020x780")
        self.minsize(560, 420)
        self.configure(fg_color=C_BG)
        try:
            self.transient(master)
        except Exception:
            pass

        head = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0)
        head.pack(side="top", fill="x")
        self._lbl_title = ctk.CTkLabel(head, text="", text_color=C_TEXT,
                                       font=ctk.CTkFont(family=FONT, size=13, weight="bold"))
        self._lbl_title.pack(side="left", padx=14, pady=8)
        ctk.CTkButton(head, text="关闭", width=70, height=26, fg_color=C_BTN,
                      hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      font=ctk.CTkFont(family=FONT, size=11),
                      command=self.close).pack(side="right", padx=10, pady=8)
        ctk.CTkButton(head, text="打开文件", width=96, height=26, fg_color=C_BTN,
                      hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      font=ctk.CTkFont(family=FONT, size=11),
                      command=self._open_file).pack(side="right", pady=8)

        # 底部信息条（先 pack，保证 pack 顺序稳定）
        foot = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0)
        foot.pack(side="bottom", fill="x")
        ctk.CTkButton(foot, text="上一个", width=90, height=26, fg_color=C_BTN,
                      hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      font=ctk.CTkFont(family=FONT, size=11),
                      command=lambda: self.step(-1)).pack(side="left", padx=(14, 6), pady=8)
        ctk.CTkButton(foot, text="下一个", width=90, height=26, fg_color=C_BTN,
                      hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      font=ctk.CTkFont(family=FONT, size=11),
                      command=lambda: self.step(1)).pack(side="left", pady=8)
        self._lbl_cap = ctk.CTkLabel(foot, text="", text_color=C_MUTED,
                                     font=ctk.CTkFont(family=FONT, size=11))
        self._lbl_cap.pack(side="right", padx=14)

        # 媒体区（原生 tk 控件，逐帧刷新性能好）
        self._body = tkinter.Frame(self, bg="#0b0d10")
        self._media = tkinter.Label(self._body, bg="#0b0d10", fg="#cfd8e3",
                                    font=(FONT, 12), justify="center", text="")
        self._media.pack(fill="both", expand=True)
        self._body.pack(side="top", fill="both", expand=True)

        # 视频控制条（仅视频时 pack，插到媒体区之前）
        self._ctl = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0)
        self._btn_play = ctk.CTkButton(self._ctl, text="暂停", width=84, height=26,
                                       fg_color=C_BTN, hover_color=C_BTN_HOVER,
                                       text_color=C_TEXT,
                                       font=ctk.CTkFont(family=FONT, size=11),
                                       command=self.toggle_play)
        self._btn_play.pack(side="left", padx=(14, 8), pady=6)
        self._lbl_time = ctk.CTkLabel(self._ctl, text="00:00 / 00:00", text_color=C_MUTED,
                                      font=ctk.CTkFont(family=FONT, size=11))
        self._lbl_time.pack(side="right", padx=14)
        self._slider = ctk.CTkSlider(self._ctl, from_=0, to=100, height=16,
                                     fg_color=C_PANEL2, progress_color=C_PRIMARY,
                                     button_color=C_PRIMARY, button_hover_color=C_PRIMARY_HOVER,
                                     command=self._on_seek)
        self._slider.pack(side="left", fill="x", expand=True, padx=6)

        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<Right>", lambda e: self.step(1))
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<space>", lambda e: self.toggle_play())
        self.bind("<MouseWheel>", self._on_wheel)
        self._body.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._render()

    # ---------- 渲染 ----------
    def _entry(self):
        return self.flat[self.idx] if self.flat else None

    @staticmethod
    def _ext(name):
        n = str(name or "")
        return n.rsplit(".", 1)[-1].lower() if "." in n else ""

    def _render(self):
        self._stop_playback()
        e = self._entry()
        if e is None:
            self._media.configure(image="", text="没有可预览的文件")
            self._lbl_cap.configure(text="")
            return
        w, f = e["work"], e["file"]
        name = disp(f.get("name")) or "-"
        ext = self._ext(name)
        self._lbl_title.configure(
            text=f"{disp(w.get('nickname')) or '未知作者'} · 作品 {w.get('content_id') or '-'}")
        self._lbl_cap.configure(
            text=f"{name} · {human_b(f.get('size'))} · 第 {self.idx + 1} / {len(self.flat)} 条")
        if ext in PV_IMG:
            self._show_image(e["path"])
        elif ext in PV_VID:
            self._show_video(e["path"])
        else:
            self._cur_img = None
            self._media.configure(
                image="", text=f"该类型（.{ext or '?'}）不支持预览\n可点右上「打开文件」")

    def _show_image(self, path):
        try:
            img = Image.open(path)
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
        except Exception as ex:
            self._cur_img = None
            self._media.configure(image="", text=f"图片加载失败：{ex!r}")
            return
        self._draw_pil(img)

    def _draw_pil(self, img):
        self._cur_img = img
        try:
            bw = max(160, self._body.winfo_width())
            bh = max(160, self._body.winfo_height())
            iw, ih = img.size
            k = min(bw / iw, bh / ih)
            nw, nh = max(1, int(iw * k)), max(1, int(ih * k))
            rs = Image.LANCZOS if k < 1 else Image.BILINEAR
            self._photo = ImageTk.PhotoImage(img.resize((nw, nh), rs))
            self._media.configure(image=self._photo, text="")
        except Exception:
            pass

    def _on_resize(self, _evt=None):
        if self._cur_img is not None and self._cap is None:
            self._draw_pil(self._cur_img)

    # ---------- 视频 ----------
    def _show_video(self, path):
        if cv2 is None:
            self._cur_img = None
            self._media.configure(
                image="", text="未内置视频解码组件，无法内嵌播放\n可点右上「打开文件」")
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self._cur_img = None
            self._media.configure(image="", text="视频打不开")
            return
        self._cap = cap
        self._fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self._total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._slider.configure(from_=0, to=max(1, self._total))
        try:
            self._slider.set(0)
        except Exception:
            pass
        self._ctl.pack(side="bottom", fill="x", before=self._body)
        self._playing = True
        self._btn_play.configure(text="暂停")
        self._stop.clear()
        threading.Thread(target=self._decode_loop, daemon=True).start()
        self._pump()

    def _decode_loop(self):
        cap = self._cap
        interval = 1.0 / max(5.0, min(float(self._fps or 25.0), 60.0))
        while not self._stop.is_set():
            if not self._playing:
                time.sleep(0.05)
                continue
            if self._seek_to is not None:
                try:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(self._seek_to))
                except Exception:
                    pass
                self._seek_to = None
            ok, frame = cap.read()
            if not ok:
                self._put(("ended", None))
                return
            try:
                pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                bw = max(160, self._body.winfo_width())
                bh = max(160, self._body.winfo_height())
                fh_, fw_ = frame.shape[:2]
                k = min(bw / fw_, bh / fh_)
                if k < 1:
                    frame = cv2.resize(frame, (max(1, int(fw_ * k)), max(1, int(fh_ * k))),
                                       interpolation=cv2.INTER_AREA)
                img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            except Exception:
                continue
            self._put(("frame", (img, pos)))
            time.sleep(interval)

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
            except Exception:
                pass

    def _pump(self):
        if self._stop.is_set():
            return
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "frame":
                    img, pos = payload
                    self._draw_pil(img)
                    if self._total:
                        try:
                            self._slider.set(min(pos, self._total))
                            fps = self._fps or 25.0
                            self._lbl_time.configure(
                                text=f"{self._fmt(pos / fps)} / {self._fmt(self._total / fps)}")
                        except Exception:
                            pass
                elif kind == "ended":
                    self._playing = False
                    self.step(1)      # 与 Web 端一致：放完自动进下一个文件
                    return
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(33, self._pump)
        except Exception:
            pass

    @staticmethod
    def _fmt(sec):
        sec = int(max(0, sec))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    # ---------- 操作 ----------
    def toggle_play(self):
        if self._cap is None:
            return
        self._playing = not self._playing
        self._btn_play.configure(text="暂停" if self._playing else "播放")

    def _on_seek(self, val):
        if self._cap is not None:
            self._seek_to = int(float(val))

    def step(self, d):
        if not self.flat:
            return
        self.idx = (self.idx + int(d)) % len(self.flat)
        self._render()

    def _on_wheel(self, evt):
        now = time.time()
        if now - self._wheel_ts < 0.35:
            return
        self._wheel_ts = now
        self.step(1 if getattr(evt, "delta", 0) < 0 else -1)

    def _open_file(self):
        e = self._entry()
        if e and self.on_open_file:
            self.on_open_file(e["file"])

    def _stop_playback(self):
        self._stop.set()
        self._playing = False
        self._seek_to = None
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        try:
            while True:
                self._q.get_nowait()
        except Exception:
            pass
        try:
            self._ctl.pack_forget()
        except Exception:
            pass

    def close(self):
        self._stop_playback()
        try:
            self.destroy()
        except Exception:
            pass


class App(ctk.CTk):
    def __init__(self, screenshot_dir=None):
        super().__init__()
        self._shot_dir = screenshot_dir
        self._authors = []            # /api/authors 原始行
        self._author_filter_sec = None
        self._file_filter_sec = None  # 文件管理当前作者
        self._file_authors = []
        self._file_works = []
        self._file_sel = {}           # download_id -> BooleanVar
        self._dl_state = {}           # content_id -> {state, local, deleted}
        self._badge_labels = {}       # content_id -> 徽章 Label（状态原地刷新）
        self._dl_watch = set()        # 下载中待观察的 content_id
        self._dl_refresh_token = 0
        self._content_offset = 0
        self._selected = {}           # content_id -> BooleanVar
        self._log_groups = []
        self._detail_open = set()
        self._done = [False] * 5      # 各页数据加载完成标志（截图模式用）
        self._db_done = False
        self._closing = False
        self._evq = queue.Queue()  # 工作线程 -> 主线程事件队列（tkinter after 非线程安全，用泵投递）
        self._trend_win = None

        self.title("抖音采集系统")
        # geometry 会被 CTk 的 DPI scaling 缩放，乘回 scaling 保证真实渲染尺寸为 1280x860
        try:
            s = self._get_window_scaling()
        except Exception:
            s = 1.0
        self.geometry(f"{int(1280 * s)}x{int(860 * s)}")
        self.minsize(1100, 700)
        self.configure(fg_color=C_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_sidebar()
        self._content = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        self._content.pack(side="right", fill="both", expand=True)
        self._pages = [self._build_page_authors(), self._build_page_contents(),
                       self._build_page_files(), self._build_page_logs(),
                       self._build_page_settings()]
        for i, p in enumerate(self._pages):
            if i == 0:
                p.grid(row=0, column=0, sticky="nsew")
            else:
                p.grid_remove()
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

        self.after(80, self._pump)   # 启动主线程事件泵
        self._show_page(0)           # 触发首页数据加载
        self._ping_db()

        if self._shot_dir:
            self.attributes("-topmost", True)
            self.after(800, self._shot_flow_check)

    # ================= 后台请求基建 =================
    def _pump(self):
        """主线程事件泵：消费工作线程投递的回调（每 80ms）"""
        try:
            while True:
                cb, res, err = self._evq.get_nowait()
                if cb:
                    try:
                        cb(res, err)
                    except Exception:
                        import traceback
                        traceback.print_exc()  # windowed 模式落 launcher.log
        except queue.Empty:
            pass
        if not self._closing:
            self.after(80, self._pump)

    def _bg(self, fn, cb=None):
        """后台线程执行 fn()，经事件队列在主线程回调 cb(result, err)；fn 内不得碰控件"""
        def runner():
            try:
                res = fn()
                err = None
            except Exception as e:
                res, err = None, e
            self._evq.put((cb, res, err))
        threading.Thread(target=runner, daemon=True).start()

    def _toast(self, msg, ok=True):
        if getattr(self, "_toast_lbl", None):
            try:
                self._toast_lbl.destroy()
            except Exception:
                pass
        color = C_OK if ok else C_ERR
        lbl = ctk.CTkLabel(self, text=msg, fg_color=color, text_color="#ffffff",
                           corner_radius=8, font=ctk.CTkFont(family=FONT, size=13),
                           padx=14, pady=8)
        lbl.place(relx=0.5, rely=0.96, anchor="center")
        self._toast_lbl = lbl
        self.after(2600, lambda: (lbl.destroy() if lbl.winfo_exists() else None))

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._toast("已复制：" + text[:40] + ("…" if len(text) > 40 else ""))

    # ================= 侧边栏 =================
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=190, corner_radius=0, fg_color=C_SIDEBAR)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        ctk.CTkLabel(sb, text="抖音采集系统", font=ctk.CTkFont(family=FONT, size=18, weight="bold"),
                     text_color=C_TEXT).pack(pady=(22, 2))
        ctk.CTkLabel(sb, text="桌面版 · 进程内直调", font=ctk.CTkFont(family=FONT, size=11),
                     text_color=C_MUTED).pack(pady=(0, 18))

        self._nav_btns = []
        for i, name in enumerate(PAGES):
            b = ctk.CTkButton(sb, text=name, font=ctk.CTkFont(family=FONT, size=14),
                              height=40, corner_radius=8, anchor="w",
                              fg_color="transparent", text_color=C_MUTED,
                              hover_color=C_PANEL,
                              command=lambda i=i: self._show_page(i))
            b.pack(fill="x", padx=12, pady=3)
            self._nav_btns.append(b)

        status = ctk.CTkFrame(sb, fg_color="transparent")
        status.pack(side="bottom", fill="x", padx=14, pady=14)
        sched = config.CFG.get("scheduler_enabled", True)
        n = config.SYNC_INTERVAL_MIN
        sched_txt = f"开 · 每 {n} 分钟" if sched else "关"
        ctk.CTkLabel(status, text=f"自动同步 {sched_txt}",
                     font=ctk.CTkFont(family=FONT, size=12), text_color=C_MUTED,
                     anchor="w").pack(fill="x")
        self._lbl_db = ctk.CTkLabel(status, text="数据库 连接中…",
                                    font=ctk.CTkFont(family=FONT, size=12),
                                    text_color=C_MUTED, anchor="w")
        self._lbl_db.pack(fill="x")

    def _show_page(self, idx):
        for i, b in enumerate(self._nav_btns):
            b.configure(fg_color=C_PRIMARY if i == idx else "transparent",
                        text_color="#ffffff" if i == idx else C_MUTED)
        for p in self._pages:
            p.grid_remove()
        self._pages[idx].grid(row=0, column=0, sticky="nsew")
        self._cur_page = idx
        if idx == 0 and not self._authors:
            self.load_authors()
        elif idx == 1:
            self.load_contents(reset=True)
        elif idx == 2:
            self.load_files()
        elif idx == 3:
            self.load_logs()
        elif idx == 4:
            self.load_settings()

    # ================= 页1：作者监控 =================
    def _build_page_authors(self):
        pg = ctk.CTkFrame(self._content, fg_color="transparent")
        top = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=10)
        top.pack(fill="x", padx=18, pady=(16, 8))
        self._sum_authors = ctk.CTkLabel(top, text="监控作者 -",
                                         font=ctk.CTkFont(family=FONT, size=13),
                                         text_color=C_TEXT)
        self._sum_authors.pack(side="left", padx=16, pady=12)
        self._sum_today = ctk.CTkLabel(top, text="今日新增作品 - · 回拉文件 -",
                                       font=ctk.CTkFont(family=FONT, size=13),
                                       text_color=C_MUTED)
        self._sum_today.pack(side="left", padx=10)
        ctk.CTkButton(top, text="刷新", width=64, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self.load_authors).pack(side="right", padx=14)
        ctk.CTkButton(top, text="立即同步", width=88, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self._sync_now).pack(side="right")
        ctk.CTkButton(top, text="+ 添加作者", width=92, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=self._add_author_dialog).pack(side="right", padx=(0, 10))

        self._cards = ctk.CTkScrollableFrame(pg, fg_color=C_BG, corner_radius=0)
        self._cards.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        return pg

    def load_authors(self):
        self._done[0] = False
        self._render_cards(None, None)  # 显示加载中

        def work():
            authors = call("GET", "/api/authors")
            if "detail" in authors:
                raise RuntimeError(authors["detail"])
            try:
                logs = call("GET", "/api/logs", params={"limit": 300})
                logs = logs.get("data", []) if "detail" not in logs else []
            except Exception:
                logs = []
            return authors["data"], logs

        self._bg(work, self._on_authors)

    def _on_authors(self, res, err):
        self._done[0] = True
        if err:
            self._render_cards(None, err)
            return
        authors, logs = res
        self._authors = authors
        self._update_author_summary(logs)
        self._render_cards(authors, None)
        self._refresh_filter_menu()

    def _today_logs(self, logs):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        return [l for l in logs if str(l.get("created_at", "")).startswith(today)]

    def _today_stats(self, logs):
        """与 Web 版同口径：新增作品 = 今日 posts 最新 items - 最早 items；
        回拉文件 = 今日 downloads message「拉回本地 a/b」a 之和。返回 (x_add, y_dl)"""
        t = self._today_logs(logs)
        posts = [l for l in t if l.get("step") == "posts" and l.get("items") is not None]
        if len(posts) == 1:
            x_add = 0
        elif len(posts) >= 2:
            diff = int(posts[0]["items"]) - int(posts[-1]["items"])
            x_add = diff if diff >= 0 else "-"
        else:
            x_add = "-"
        dl = [l for l in t if l.get("step") == "downloads"]
        y_dl = "-"
        if dl:
            ms = [re.search(r"拉回本地\s*(\d+)\s*/\s*(\d+)", str(l.get("message") or "")) for l in dl]
            y_dl = sum(int(m.group(1)) for m in ms) if all(ms) else "-"
        return x_add, y_dl

    def _update_author_summary(self, logs):
        self._sum_authors.configure(text=f"监控 {len(self._authors)} 位作者")
        x_add, y_dl = self._today_stats(logs)
        self._sum_today.configure(text=f"今日新增作品 {x_add} · 今日回拉文件 {y_dl}")

    def _sync_now(self):
        self._toast("全量同步已启动，约 30 秒后完成", ok=True)
        self._bg(lambda: call("POST", "/api/sync/run"),
                 lambda res, err: None)

    def _delta_txt(self, d):
        """(文本, 颜色)"""
        if d is None or d == 0:
            return "-", C_MUTED
        if d > 0:
            return f"▲{fmt_n(d)}", C_UP
        return f"▼{fmt_n(-d)}", C_DOWN

    def _render_cards(self, authors, err):
        for w in self._cards.winfo_children():
            w.destroy()
        if err:
            ctk.CTkLabel(self._cards, text=f"加载失败：{err}", text_color=C_ERR,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=40)
            return
        if authors is None:
            ctk.CTkLabel(self._cards, text="加载中…", text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=40)
            return
        if not authors:
            ctk.CTkLabel(self._cards, text="暂无监控作者，点击右上角「+ 添加作者」",
                         text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=40)
            return
        COLS = 3
        for c in range(COLS):
            self._cards.grid_columnconfigure(c, weight=1, uniform="card")
        for i, a in enumerate(authors):
            card = ctk.CTkFrame(self._cards, fg_color=C_PANEL, corner_radius=12)
            card.grid(row=i // COLS, column=i % COLS, padx=8, pady=8, sticky="nsew")
            self._fill_card(card, a)

    def _fill_card(self, card, a):
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 0))
        nick = disp(a.get("nickname") or a["sec_uid"])[:14]
        ctk.CTkLabel(head, text=nick,
                     font=ctk.CTkFont(family=FONT, size=15, weight="bold"),
                     text_color=C_TEXT, anchor="w").pack(side="left")
        sw = ctk.CTkSwitch(head, text="", width=40,
                           progress_color=C_PRIMARY,
                           command=lambda a=a: self._toggle_author(a))
        (sw.select if a.get("enabled") else sw.deselect)()
        sw.pack(side="right")

        # 第二行：@抖音号 + IP 徽章 + 「有变化」徽章
        uid_row = ctk.CTkFrame(card, fg_color="transparent")
        uid_row.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkLabel(uid_row, text="@" + disp(a.get("unique_id") or "-")[:16],
                     font=ctk.CTkFont(family=FONT, size=11),
                     text_color=C_MUTED).pack(side="left")
        ip_raw = re.sub(r"^IP属地[：:]\s*", "", str(a.get("ip_location") or ""))
        if ip_raw:
            ctk.CTkLabel(uid_row, text=f"IP {ip_raw[:8]}", font=ctk.CTkFont(family=FONT, size=10),
                         text_color=C_IP_FG, fg_color=C_IP_BG, corner_radius=8,
                         padx=6, pady=1).pack(side="left", padx=(6, 0))
        else:
            ctk.CTkLabel(uid_row, text="30天无互动", font=ctk.CTkFont(family=FONT, size=10),
                         text_color="#6b7280", fg_color=C_PANEL2, corner_radius=8,
                         padx=6, pady=1).pack(side="left", padx=(6, 0))
        if a.get("change_flag"):
            ctk.CTkButton(uid_row, text="有变化", width=54, height=18,
                          font=ctk.CTkFont(family=FONT, size=10, weight="bold"),
                          fg_color="#5b2029", hover_color="#7a2531", text_color="#f87171",
                          corner_radius=8,
                          command=lambda a=a: self._open_trend(a, range="day", ack=True)
                          ).pack(side="right")

        # 四指标格：关注 / 粉丝 / 获赞 / 作品（数值 + 当日涨跌）
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=10, pady=(8, 2))
        cells = [("关注", a.get("last_following_count"), a.get("delta_following")),
                 ("粉丝", a.get("last_follower_count"), a.get("delta_follower")),
                 ("获赞", a.get("last_total_digg"), a.get("delta_digg")),
                 ("作品", a.get("last_content_count"), a.get("delta_content"))]
        for j, (lbl, val, dl_) in enumerate(cells):
            cell = ctk.CTkFrame(stats, fg_color=C_PANEL2, corner_radius=8)
            cell.grid(row=0, column=j, padx=3, sticky="nsew")
            stats.grid_columnconfigure(j, weight=1)
            dt_, dc_ = self._delta_txt(dl_)
            ctk.CTkLabel(cell, text=fmt_n(val),
                         font=ctk.CTkFont(family=FONT, size=14, weight="bold"),
                         text_color=C_TEXT).pack(pady=(6, 0))
            lf = ctk.CTkFrame(cell, fg_color="transparent")
            lf.pack(pady=(0, 5))
            ctk.CTkLabel(lf, text=lbl, font=ctk.CTkFont(family=FONT, size=10),
                         text_color=C_MUTED).pack(side="left")
            ctk.CTkLabel(lf, text=dt_, font=ctk.CTkFont(family=FONT, size=10),
                         text_color=dc_).pack(side="left", padx=2)

        sig = ctk.CTkLabel(card, text=disp(a.get("signature") or "-")[:26],
                           font=ctk.CTkFont(family=FONT, size=11), text_color=C_MUTED,
                           anchor="w")
        sig.pack(fill="x", padx=14)

        sec = ctk.CTkLabel(card, text=f"{a['sec_uid'][:12]}… 复制",
                           font=ctk.CTkFont(family="Consolas", size=10),
                           text_color=C_MUTED, cursor="hand2")
        sec.pack(anchor="w", padx=14)
        sec.bind("<Button-1>", lambda e, a=a: self._copy(a["sec_uid"]))

        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.pack(fill="x", padx=10, pady=(6, 12))
        btns = [("刷新", self._refresh_author), ("趋势", lambda a=a: self._open_trend(a)),
                ("文件", self._goto_files), ("设置", self._author_settings_dialog)]
        for name, cb in btns:
            ctk.CTkButton(foot, text=name, width=48, height=26,
                          font=ctk.CTkFont(family=FONT, size=11),
                          fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                          command=lambda a=a, cb=cb: cb(a)).pack(side="left", padx=2)
        ctk.CTkButton(foot, text="停用" if a.get("enabled") else "启用", width=48, height=26,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=lambda a=a: self._toggle_author(a)).pack(side="left", padx=2)
        ctk.CTkButton(foot, text="删除", width=48, height=26,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color="#3f1d24", hover_color="#5b2029", text_color=C_ERR,
                      command=lambda a=a: self._delete_author(a)).pack(side="right", padx=2)

    def _toggle_author(self, a):
        new_val = not a.get("enabled")

        def work():
            r = call("PATCH", f"/api/authors/{a['sec_uid']}/enabled",
                     json={"enabled": new_val})
            if "detail" in r:
                raise RuntimeError(r["detail"])
            return r

        def cb(res, err):
            self._toast("已启用" if new_val else "已停用", ok=not err)
            if not err:
                self.load_authors()
        self._bg(work, cb)

    def _refresh_author(self, a):
        self._toast(f"正在刷新 {disp(a.get('nickname'))[:10]} …", ok=True)

        def work():
            r = call("POST", f"/api/authors/{a['sec_uid']}/refresh")
            if "detail" in r:
                raise RuntimeError(r["detail"])
            return r

        def cb(res, err):
            if err:
                self._toast(f"刷新失败：{err}", ok=False)
            else:
                d = (res or {}).get("data") or {}
                self._toast(f"已刷新：{disp(d.get('nickname'))[:10]} "
                            f"粉丝 {fmt_n(d.get('last_follower_count'))}", ok=True)
                self.load_authors()
        self._bg(work, cb)

    def _delete_author(self, a):
        nick = a.get("nickname") or a["sec_uid"]
        if not mb.askyesno("确认删除",
                           f"确定删除作者「{nick}」？\n其全部作品记录、下载记录、趋势快照与本地文件将一并删除，不可恢复。"):
            return

        def work():
            r = call("DELETE", f"/api/authors/{a['sec_uid']}")
            if "detail" in r:
                raise RuntimeError(r["detail"])
            return r

        def cb(res, err):
            self._toast(res.get("message", "已删除") if res and "detail" not in res
                        else (f"删除失败：{err}"), ok=not err)
            if not err:
                self.load_authors()
        self._bg(work, cb)

    def _add_author_dialog(self):
        d = ctk.CTkInputDialog(text="粘贴作者主页链接或 sec_uid（MS4w 开头）：",
                               title="添加作者")
        val = d.get_input()
        if not val or not val.strip():
            return

        def work():
            r = call("POST", "/api/authors", json={"sec_uid": val.strip()})
            if "detail" in r:
                raise RuntimeError(r["detail"])
            return r

        def cb(res, err):
            self._toast(res.get("message", "已加入") if res and "detail" not in res
                        else f"添加失败：{err}", ok=not err)
            if not err:
                self.load_authors()
        self._bg(work, cb)

    def _author_settings_dialog(self, a):
        win = ctk.CTkToplevel(self)
        win.title("作者独立设置")
        win.geometry("380x240")
        win.configure(fg_color=C_PANEL)
        win.grab_set()
        win.transient(self)
        ctk.CTkLabel(win, text=disp(a.get("nickname") or a["sec_uid"])[:16],
                     font=ctk.CTkFont(family=FONT, size=15, weight="bold"),
                     text_color=C_TEXT).pack(pady=(18, 4))
        row1 = ctk.CTkFrame(win, fg_color="transparent")
        row1.pack(fill="x", padx=24, pady=8)
        ctk.CTkLabel(row1, text="资料自动刷新间隔（分钟）",
                     font=ctk.CTkFont(family=FONT, size=12),
                     text_color=C_MUTED).pack(anchor="w")
        e_iv = ctk.CTkEntry(row1, width=140, fg_color=C_PANEL2)
        e_iv.insert(0, str(a.get("refresh_interval_min") or 360))
        e_iv.pack(anchor="w", pady=4)
        sw = ctk.CTkSwitch(win, text="新作品自动下载", font=ctk.CTkFont(family=FONT, size=12),
                           text_color=C_TEXT, progress_color=C_PRIMARY)
        (sw.select if a.get("auto_download") else sw.deselect)()
        sw.pack(anchor="w", padx=24, pady=6)

        def save():
            try:
                iv = max(10, min(int(e_iv.get().strip()), 10080))
            except ValueError:
                self._toast("间隔必须是数字", ok=False)
                return

            def work():
                r = call("PATCH", f"/api/authors/{a['sec_uid']}/settings",
                         json={"refresh_interval_min": iv,
                               "auto_download": bool(sw.get())})
                if "detail" in r:
                    raise RuntimeError(r["detail"])
                return r

            def cb(res, err):
                self._toast(res.get("message", "设置已保存") if res and "detail" not in res
                            else f"保存失败：{err}", ok=not err)
                win.destroy()
                if not err:
                    self.load_authors()
            self._bg(work, cb)

        ctk.CTkButton(win, text="保 存", width=110, height=32,
                      font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=save).pack(pady=12)

    # ---------- 趋势弹窗（Canvas 自绘折线） ----------
    def _open_trend(self, a, range="month", ack=False):
        sec = a["sec_uid"]
        nick = disp(a.get("nickname")) or sec
        if self._trend_win is not None and self._trend_win.winfo_exists():
            self._trend_win.destroy()
        win = ctk.CTkToplevel(self)
        self._trend_win = win
        win.title(f"{nick} · 趋势")
        win.geometry("760x540")
        win.configure(fg_color=C_PANEL)
        win.transient(self)
        if ack:
            win.grab_set()
            win.protocol("WM_DELETE_WINDOW", lambda: self._close_trend(win, sec))
        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(head, text=f"{nick[:14]} · 趋势",
                     font=ctk.CTkFont(family=FONT, size=15, weight="bold"),
                     text_color=C_TEXT).pack(side="left")
        self._lbl_trend_info = ctk.CTkLabel(head, text="加载中…",
                                            font=ctk.CTkFont(family=FONT, size=11),
                                            text_color=C_MUTED)
        self._lbl_trend_info.pack(side="right")

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(8, 0))
        rb_btns = {}
        mb_btns = {}
        state = {"range": range, "metric": 0}

        def mk_group(options, key, parent):
            btns = {}
            fr = ctk.CTkFrame(parent, fg_color="transparent")
            fr.pack(side="left", padx=(0, 18))
            for val, label in options:
                b = ctk.CTkButton(fr, text=label, width=44, height=24,
                                  font=ctk.CTkFont(family=FONT, size=11),
                                  fg_color=C_BTN, hover_color=C_BTN_HOVER,
                                  text_color=C_TEXT, corner_radius=7)
                b.pack(side="left", padx=2)
                btns[val] = b
            return btns

        def paint():
            for v, b in rb_btns.items():
                b.configure(fg_color=C_PRIMARY if v == state["range"] else C_BTN)
            for j, (k, _, col) in enumerate(TREND_METRICS):
                mb_btns[j].configure(fg_color=col if j == state["metric"] else C_BTN,
                                     text_color="#ffffff" if j == state["metric"] else C_TEXT)

        def on_range(v):
            state["range"] = v
            paint()
            load()

        def on_metric(j):
            state["metric"] = j
            paint()
            draw(state.get("rows") or [])

        rb_btns = mk_group([(k, l) for k, l in TREND_RANGES], "range", bar)
        for v, b in rb_btns.items():
            b.configure(command=lambda v=v: on_range(v))
        mb_btns = mk_group([(j, m[1]) for j, m in enumerate(TREND_METRICS)], "metric", bar)
        for j, b in mb_btns.items():
            b.configure(command=lambda j=j: on_metric(j))
        paint()

        canvas = tkinter.Canvas(win, width=700, height=380, bg=C_PANEL,
                                highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True, padx=14, pady=(6, 12))

        def load():
            r = state["range"]
            self._lbl_trend_info.configure(text="加载中…")

            def work():
                res = call("GET", f"/api/authors/{sec}/trend", params={"range": r})
                if "detail" in res:
                    raise RuntimeError(res["detail"])
                return res.get("data") or []

            def cb(rows, err):
                if not win.winfo_exists():
                    return
                if err:
                    self._lbl_trend_info.configure(text=f"加载失败：{err}", text_color=C_ERR)
                    return
                state["rows"] = rows
                self._lbl_trend_info.configure(
                    text=f"{len(rows)} 个数据点 · "
                         f"{'今日 0 点以来原始刷新点' if r == 'day' else '每日收盘'}",
                    text_color=C_MUTED)
                draw(rows)
            self._bg(work, cb)

        def draw(rows):
            if not canvas.winfo_exists():
                return
            canvas.delete("all")
            j = state["metric"]
            key, mname, mcol = TREND_METRICS[j]
            pts = [(str(r.get("taken_at") or ""), r.get(key))
                   for r in rows if r.get(key) is not None]
            if len(pts) < 2:
                canvas.create_text(350, 190, text="数据点不足，暂无法绘图",
                                   fill=C_MUTED, font=(FONT, 13))
                return
            W, H = 700, 380
            L, R, T, B = 78, 24, 18, 42
            pw, ph = W - L - R, H - T - B
            vals = [v for _, v in pts]
            lo, hi = min(vals), max(vals)
            if lo == hi:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad

            def vx(i):
                return L + pw * i / (len(pts) - 1)

            def vy(v):
                return T + ph * (1 - (v - lo) / (hi - lo))

            # 网格与 y 轴刻度
            for g in range(5):
                y = T + ph * g / 4
                v = hi - (hi - lo) * g / 4
                canvas.create_line(L, y, W - R, y, fill="#333a46")
                canvas.create_text(L - 6, y, text=fmt_n(round(v)), anchor="e",
                                   fill=C_MUTED, font=("Consolas", 9))
            # x 轴日期标签（最多 6 个）
            n_lab = min(6, len(pts))
            for i in range(n_lab):
                k = round(i * (len(pts) - 1) / max(1, n_lab - 1))
                x = vx(k)
                lab = pts[k][0][5:16].replace("T", " ") if state["range"] == "day" \
                    else pts[k][0][5:10]
                canvas.create_text(x, H - B + 14, text=lab, fill=C_MUTED,
                                   font=("Consolas", 9))
            # 折线 + 数据点
            coords = [(vx(i), vy(v)) for i, (_, v) in enumerate(pts)]
            canvas.create_line(*[c for p in coords for c in p], fill=mcol,
                               width=2, smooth=True)
            for x, y in coords:
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=mcol, outline="")
            first, last = vals[0], vals[-1]
            d_txt, d_col = self._delta_txt(last - first if last != first else None)
            canvas.create_text(W - R, T + 4, anchor="ne",
                               text=f"{mname}  {fmt_n(last)}  {d_txt}",
                               fill=d_col if d_txt != "-" else C_MUTED,
                               font=(FONT, 11, "bold"))

        load()

    def _close_trend(self, win, sec):
        """关闭有变化打开的趋势弹窗：熄灭变化标识并刷新卡片"""
        try:
            win.destroy()
        except Exception:
            pass

        def work():
            call("POST", f"/api/authors/{sec}/ack-change")

        self._bg(work, None)
        self.load_authors()

    # ================= 页2：作品与下载 =================
    def _build_page_contents(self):
        pg = ctk.CTkFrame(self._content, fg_color="transparent")
        top = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=10)
        top.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(top, text="作者筛选", font=ctk.CTkFont(family=FONT, size=13),
                     text_color=C_MUTED).pack(side="left", padx=(16, 6))
        self._filter_menu = ctk.CTkOptionMenu(top, width=160, values=["全部作者"],
                                              font=ctk.CTkFont(family=FONT, size=12),
                                              fg_color=C_PANEL2, button_color=C_PANEL2,
                                              button_hover_color=C_BTN_HOVER,
                                              text_color=C_TEXT,
                                              command=self._on_filter_change)
        self._filter_menu.pack(side="left")
        self._lbl_total = ctk.CTkLabel(top, text="", font=ctk.CTkFont(family=FONT, size=12),
                                       text_color=C_MUTED)
        self._lbl_total.pack(side="left", padx=12)
        self._sw_sel_all = ctk.CTkCheckBox(top, text="全选", font=ctk.CTkFont(family=FONT, size=12),
                                           text_color=C_MUTED, fg_color=C_PRIMARY,
                                           command=self._toggle_sel_all)
        self._sw_sel_all.pack(side="right", padx=14)
        ctk.CTkButton(top, text="批量下载选中", width=110, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=self._batch_download).pack(side="right", padx=0)
        ctk.CTkButton(top, text="刷新", width=64, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=lambda: self.load_contents(reset=True)).pack(side="right", padx=(0, 10))

        self._rows = ctk.CTkScrollableFrame(pg, fg_color=C_BG, corner_radius=0)
        self._rows.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        return pg

    def _refresh_filter_menu(self):
        vals = ["全部作者"] + [disp(a.get("nickname") or a["sec_uid"])[:14] for a in self._authors]
        cur = self._filter_menu.get()
        self._filter_menu.configure(values=vals)
        if cur in vals:
            self._filter_menu.set(cur)
        else:
            self._filter_menu.set("全部作者")
            self._author_filter_sec = None

    def _on_filter_change(self, nick):
        if nick == "全部作者":
            self._author_filter_sec = None
        else:
            for a in self._authors:
                if disp(a.get("nickname") or a["sec_uid"])[:14] == nick:
                    self._author_filter_sec = a["sec_uid"]
                    break
        self.load_contents(reset=True)

    def _toggle_sel_all(self):
        v = bool(self._sw_sel_all.get())
        for var in self._selected.values():
            var.set(v)

    def load_contents(self, reset=True):
        self._done[1] = False
        if reset:
            self._content_offset = 0
            for w in self._rows.winfo_children():
                w.destroy()
            ctk.CTkLabel(self._rows, text="加载中…", text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=24)

        def work():
            dl = call("GET", "/api/downloads", params={"limit": 500})
            state_map = {} if "detail" in dl else self._dl_map(dl)
            q = {"limit": 100, "offset": self._content_offset}
            if self._author_filter_sec:
                q["sec_uid"] = self._author_filter_sec
            data = call("GET", "/api/contents", params=q)
            if "detail" in data:
                raise RuntimeError(data["detail"])
            return state_map, data

        self._bg(work, self._on_contents)

    def _on_contents(self, res, err):
        self._done[1] = True
        if err:
            for w in self._rows.winfo_children():
                w.destroy()
            ctk.CTkLabel(self._rows, text=f"加载失败：{err}", text_color=C_ERR,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=30)
            return
        state_map, data = res
        self._dl_state = state_map
        rows = data["data"]
        if self._content_offset == 0:
            for w in self._rows.winfo_children():
                w.destroy()
            self._badge_labels = {}
        self._content_offset += len(rows)
        self._lbl_total.configure(text=f"共 {data.get('total', len(rows))} 条")
        if not rows and self._content_offset == len(rows):
            ctk.CTkLabel(self._rows, text="暂无作品记录", text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=30)
            return
        for r in rows:
            self._add_content_row(r)
        if data.get("total", 0) > self._content_offset:
            ctk.CTkButton(self._rows, text="加载更多", width=120, height=30,
                          font=ctk.CTkFont(family=FONT, size=12),
                          fg_color=C_PANEL, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                          command=lambda: self.load_contents(reset=False)).pack(pady=10)

    @staticmethod
    def _dl_entry(d):
        """一行 dm_downloads -> 徽章判定信息。与 Web 版同口径：以本地文件为准
        （Web 版作品页不显示下载状态，权威视图是文件管理页的磁盘实扫）。"""
        ld = d.get("local_dir")
        try:
            has_local = bool(ld) and os.path.isdir(ld) and bool(os.listdir(ld))
        except OSError:
            has_local = False
        return {"state": d.get("state"), "local": has_local,
                "deleted": int(d.get("local_deleted") or 0)}

    @classmethod
    def _dl_map(cls, dl):
        """content_id -> 判定信息；同一作品多条记录时本地有文件的优先"""
        out = {}
        for d in (dl.get("data") or []):
            cid = d.get("content_id")
            if not cid:
                continue
            e = cls._dl_entry(d)
            old_e = out.get(cid)
            if old_e is None or (e["local"] and not old_e["local"]):
                out[cid] = e
        return out

    def _state_badge(self, info):
        """徽章文案：只有本地真存在文件才算「已下载」"""
        if not info:
            return ("未下载", C_MUTED)
        if isinstance(info, str):            # 兼容只传 state 的旧调用
            info = {"state": info}
        if info.get("local"):
            return ("已下载", C_OK)
        st = info.get("state")
        if st == "queued":
            return ("排队中", C_PRIMARY)
        if st == "processing":
            return ("处理中", C_PRIMARY)
        if st == "error":
            return ("失败", C_ERR)
        if st == "done":
            # DTK 有下载记录但本地没文件：手动删过 -> 本地已删；否则等同步回拉
            return ("本地已删", C_WARN) if info.get("deleted") else ("待回拉", C_PRIMARY)
        return ("未下载", C_MUTED)

    def _add_content_row(self, r):
        row = ctk.CTkFrame(self._rows, fg_color=C_PANEL, corner_radius=8)
        row.pack(fill="x", padx=6, pady=3)
        var = self._selected.get(r["content_id"]) or ctk.BooleanVar(value=False)
        self._selected[r["content_id"]] = var
        ctk.CTkCheckBox(row, text="", width=26, variable=var,
                        fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER).pack(side="left", padx=(10, 2))
        title = disp(r.get("title") or r.get("description") or r.get("content_id") or "")[:38]
        ctk.CTkLabel(row, text=title, font=ctk.CTkFont(family=FONT, size=12),
                     text_color=C_TEXT, anchor="w").pack(side="left", padx=4)
        nick = disp(r.get("nickname"))[:10]
        ctk.CTkLabel(row, text=nick, font=ctk.CTkFont(family=FONT, size=11),
                     text_color=C_MUTED).pack(side="left", padx=6)
        ctk.CTkLabel(row, text=fmt_ts(r.get("published_at")),
                     font=ctk.CTkFont(family=FONT, size=11), text_color=C_MUTED).pack(side="right", padx=10)
        badge, color = self._state_badge(self._dl_state.get(r["content_id"]))
        bl = ctk.CTkLabel(row, text=badge, font=ctk.CTkFont(family=FONT, size=11),
                          text_color=color, fg_color=C_PANEL2, corner_radius=6)
        bl.pack(side="right", padx=8, pady=6)
        self._badge_labels[r["content_id"]] = bl
        ctk.CTkButton(row, text="下载", width=52, height=24,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=lambda r=r: self._download_one(r)).pack(side="right", padx=4)

    def _download_one(self, r):
        self._toast("已提交下载任务…", ok=True)

        def work():
            res = call("POST", f"/api/contents/{r['content_id']}/download")
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已提交") if res and "detail" not in res
                        else f"失败：{err}", ok=not err)
            if not err:
                self._dl_watch.add(r["content_id"])
                self._schedule_state_refresh()
        self._bg(work, cb)

    def _batch_download(self):
        ids = [cid for cid, v in self._selected.items() if v.get()]
        if not ids:
            self._toast("请先勾选作品", ok=False)
            return
        if not mb.askyesno("批量下载", f"已选择 {len(ids)} 个作品，确认提交下载？"):
            return

        def work():
            res = call("POST", "/api/contents/download-batch", json={"content_ids": ids})
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已提交") if res and "detail" not in res
                        else f"失败：{err}", ok=not err)
            if not err:
                self._dl_watch.update(ids)
                self._schedule_state_refresh()
        self._bg(work, cb)

    def _schedule_state_refresh(self):
        """下载提交后轮询刷新状态徽章（排队中/处理中 -> 已下载），全部落定或
        160 秒后停止；重复提交会重置计数。"""
        self._dl_refresh_token += 1
        token = self._dl_refresh_token
        ticks = {"n": 0}

        def tick():
            if token != self._dl_refresh_token:
                return
            ticks["n"] += 1
            active = {c for c in self._dl_watch
                      if not (self._dl_state.get(c) or {}).get("local")}
            if not active or ticks["n"] > 20:
                return

            def work():
                dl = call("GET", "/api/downloads", params={"limit": 500})
                if "detail" in dl:
                    raise RuntimeError(dl["detail"])
                return self._dl_map(dl)

            def apply(res, err):
                if err or token != self._dl_refresh_token:
                    return
                self._dl_state.update(res)
                self._redraw_state_badges()
                self.after(8000, tick)

            self._bg(work, apply)

        self.after(4000, tick)

    def _redraw_state_badges(self):
        for cid, lbl in list(self._badge_labels.items()):
            try:
                badge, color = self._state_badge(self._dl_state.get(cid))
                lbl.configure(text=badge, text_color=color)
            except Exception:
                self._badge_labels.pop(cid, None)

    # ================= 页3：文件管理 =================
    def _build_page_files(self):
        pg = ctk.CTkFrame(self._content, fg_color="transparent")
        # 左：作者汇总卡列表
        left = ctk.CTkFrame(pg, fg_color=C_SIDEBAR, corner_radius=10)
        left.pack(side="left", fill="y", padx=(18, 6), pady=(16, 10))
        left.configure(width=240)
        left.pack_propagate(False)
        self._fm_summary = ctk.CTkLabel(left, text="文件汇总 -",
                                        font=ctk.CTkFont(family=FONT, size=12),
                                        text_color=C_MUTED, wraplength=210)
        self._fm_summary.pack(fill="x", padx=12, pady=(12, 6))
        self._fm_author_list = ctk.CTkScrollableFrame(left, fg_color=C_SIDEBAR, corner_radius=0)
        self._fm_author_list.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        # 右：工具条 + 作品文件列表
        right = ctk.CTkFrame(pg, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(6, 18), pady=(16, 10))
        bar = ctk.CTkFrame(right, fg_color=C_PANEL, corner_radius=10)
        bar.pack(fill="x")
        self._fm_cur_label = ctk.CTkLabel(bar, text="全部作者",
                                          font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                          text_color=C_TEXT)
        self._fm_cur_label.pack(side="left", padx=14, pady=10)
        ctk.CTkButton(bar, text="打开目录", width=84, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self._fm_open_dir).pack(side="right", padx=14)
        ctk.CTkButton(bar, text="删除选中", width=84, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color="#3f1d24", hover_color="#5b2029", text_color=C_ERR,
                      command=self._fm_batch_delete).pack(side="right")
        ctk.CTkButton(bar, text="刷新", width=60, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self.load_files).pack(side="right")
        self._fm_works = ctk.CTkScrollableFrame(right, fg_color=C_BG, corner_radius=0)
        self._fm_works.pack(fill="both", expand=True, pady=(6, 0))
        return pg

    def load_files(self):
        self._done[2] = False
        self._fm_summary.configure(text="文件汇总 加载中…")

        def work():
            ra = call("GET", "/api/files/authors")
            if "detail" in ra:
                raise RuntimeError(ra["detail"])
            return ra["data"]

        self._bg(work, self._on_file_authors)

    def _on_file_authors(self, res, err):
        self._done[2] = False
        if err:
            self._fm_summary.configure(text=f"文件汇总加载失败：{err}")
            return
        self._file_authors = res
        total_files = sum(a.get("files") or 0 for a in res)
        total_b = sum(a.get("bytes") or 0 for a in res)
        self._fm_summary.configure(
            text=f"{len(res)} 位作者 · {total_files} 个文件 · {human_b(total_b)}")
        # 作者卡片（含「全部」）：用按钮实现，点击可靠
        for w in self._fm_author_list.winfo_children():
            w.destroy()
        cards = [("全部", None)] + [(disp(a.get("nickname") or a["sec_uid"])[:12],
                                     a.get("sec_uid")) for a in res]
        for name, sec in cards:
            info = ""
            if sec:
                row = next((x for x in res if x.get("sec_uid") == sec), None)
                if row:
                    info = f"{row.get('works') or 0} 作品 / {row.get('files') or 0} 文件 · {human_b(row.get('bytes'))}"
            active = (sec or None) == self._file_filter_sec
            ctk.CTkButton(
                self._fm_author_list, text=(name + "\n" + info) if info else name,
                anchor="w", height=52 if info else 34,
                font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                fg_color=C_PRIMARY if active else C_PANEL,
                hover_color=C_PRIMARY_HOVER if active else C_BTN_HOVER,
                text_color="#ffffff" if active else C_TEXT,
                corner_radius=8,
                command=lambda s=sec, n=name: self._fm_select_author(s, n),
            ).pack(fill="x", pady=3, padx=2)

        def work2():
            params = {"sec_uid": self._file_filter_sec} if self._file_filter_sec else None
            rb = call("GET", "/api/files/browse", params=params)
            if "detail" in rb:
                raise RuntimeError(rb["detail"])
            return rb["data"]

        self._bg(work2, self._on_file_works)

    def _fm_select_author(self, sec, name):
        self._file_filter_sec = sec
        self._fm_cur_label.configure(text=name)
        self._on_file_authors(self._file_authors, None)  # 重画选中态并刷新右侧

    def _on_file_works(self, res, err):
        self._done[2] = True
        for w in self._fm_works.winfo_children():
            w.destroy()
        if err:
            ctk.CTkLabel(self._fm_works, text=f"加载失败：{err}", text_color=C_ERR,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=30)
            return
        self._file_works = res
        self._file_sel = {}
        if not res:
            ctk.CTkLabel(self._fm_works,
                         text="暂无本地文件（下载完成后自动按作者保存到这里）",
                         text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=40)
            return
        for wk in res:
            self._add_file_work_row(wk)

    def _add_file_work_row(self, wk):
        box = ctk.CTkFrame(self._fm_works, fg_color=C_PANEL, corner_radius=8)
        box.pack(fill="x", padx=6, pady=4)
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x")
        # 左：封面缩略图（点封面 = 预览，对齐 Web 端 fm-covwrap）
        cov = tkinter.Label(inner, bg=C_PANEL2, fg=C_MUTED, text="封面",
                            font=(FONT, 16), cursor="hand2", bd=0, highlightthickness=0)
        cov.pack(side="left", padx=(10, 8), pady=10, anchor="n")
        cov.bind("<Button-1>", lambda e, wk=wk: self._preview_from_work(wk, 0))
        self._load_cover(wk, cov)
        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, pady=(8, 0))
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x")
        var = ctk.BooleanVar(value=False)
        self._file_sel[wk["id"]] = var
        ctk.CTkCheckBox(head, text="", width=26, variable=var,
                        fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(head, text=f"{disp(wk.get('nickname'))[:12] or '未知作者'} · 作品 {wk.get('content_id') or '-'}",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C_TEXT, anchor="w").pack(side="left", padx=4)
        size = sum(f.get("size") or 0 for f in wk.get("files") or [])
        ctk.CTkLabel(head, text=f"{len(wk.get('files') or [])} 个文件 · {human_b(size)} · "
                                f"{fmt_ts(wk.get('finished_at'))}",
                     font=ctk.CTkFont(family=FONT, size=11),
                     text_color=C_MUTED).pack(side="right", padx=10)
        files_fr = ctk.CTkFrame(right, fg_color=C_PANEL2, corner_radius=6)
        files_fr.pack(fill="x", padx=(0, 10), pady=(4, 0))
        for fi, f in enumerate(wk.get("files") or []):
            fr = ctk.CTkFrame(files_fr, fg_color="transparent")
            fr.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(fr, text=f"{disp(f.get('name')) or '-'}",
                         font=ctk.CTkFont(family=FONT, size=11),
                         text_color=C_TEXT, anchor="w").pack(side="left")
            ctk.CTkLabel(fr, text=f"{human_b(f.get('size'))}",
                         font=ctk.CTkFont(family=FONT, size=11),
                         text_color=C_MUTED).pack(side="right", padx=(6, 10))
            lb_open = ctk.CTkLabel(fr, text="打开", font=ctk.CTkFont(family=FONT, size=11),
                                   text_color="#60a5fa", cursor="hand2")
            lb_open.pack(side="right", padx=4)
            lb_open.bind("<Button-1>", lambda e, f=f: self._fm_open_file(f))
            lb_prev = ctk.CTkLabel(fr, text="预览", font=ctk.CTkFont(family=FONT, size=11),
                                   text_color="#60a5fa", cursor="hand2")
            lb_prev.pack(side="right", padx=4)
            lb_prev.bind("<Button-1>",
                         lambda e, wk=wk, fi=fi: self._preview_from_work(wk, fi))
        foot = ctk.CTkFrame(right, fg_color="transparent")
        foot.pack(fill="x", pady=(4, 8))
        ctk.CTkButton(foot, text="预览", width=88, height=24,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=lambda wk=wk: self._preview_from_work(wk, 0)).pack(side="left")
        ctk.CTkButton(foot, text="删除此作品文件", width=110, height=24,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color="#3f1d24", hover_color="#5b2029", text_color=C_ERR,
                      command=lambda wk=wk: self._fm_delete_one(wk)).pack(side="right")

    # ---- 预览（对齐 Web 端 pvModal：跨作品连续切换） ----
    def _fm_file_path(self, f):
        rel = str(f.get("url") or "").replace("/media/", "", 1)
        return os.path.join(config.MEDIA_DIR, rel.replace("/", os.sep))

    def _preview_from_work(self, wk, file_idx=0):
        works = self._file_works or [wk]
        flat, idx = [], 0
        for w in works:
            for fi, f in enumerate(w.get("files") or []):
                if w is wk and fi == file_idx:
                    idx = len(flat)
                flat.append({"work": w, "file": f, "path": self._fm_file_path(f)})
        if not flat:
            self._toast("该作品没有可预览的文件", ok=False)
            return
        old = getattr(self, "_pv_win", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._pv_win = PreviewWindow(self, flat, idx, on_open_file=self._fm_open_file)
        try:
            self._pv_win.focus()
        except Exception:
            pass

    def _load_cover(self, wk, label):
        """作品卡片封面缩略图：本地缓存优先，否则后台下载（失败保留占位）"""
        cid = str(wk.get("content_id") or "")
        url = str(wk.get("cover_url") or "")
        cache = os.path.join(COVER_DIR, f"{cid}.jpg") if cid else None
        is_vid = any(
            (f.get("name") or "").rsplit(".", 1)[-1].lower() in PV_VID
            for f in wk.get("files") or [])

        def show(path):
            try:
                img = Image.open(path).convert("RGB")
                iw, ih = img.size
                tw, th = 96, 120
                k = max(tw / iw, th / ih)
                img = img.resize((max(1, int(iw * k)), max(1, int(ih * k))), Image.LANCZOS)
                w2, h2 = img.size
                img = img.crop(((w2 - tw) // 2, (h2 - th) // 2,
                                (w2 - tw) // 2 + tw, (h2 - th) // 2 + th))
                ph = ImageTk.PhotoImage(img)
                if label.winfo_exists():
                    label.configure(image=ph, text="", width=tw, height=th)
                    label.image = ph          # 防 GC
            except Exception:
                pass

        if cache and os.path.isfile(cache):
            show(cache)
            return
        if not (url and cache):
            label.configure(text="视频" if is_vid else "图片", width=8, height=5)
            return
        try:
            os.makedirs(COVER_DIR, exist_ok=True)
        except OSError:
            return

        def work():
            r = httpx.get(url, timeout=6.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0",
                                   "Referer": "https://www.douyin.com/"})
            r.raise_for_status()
            with open(cache, "wb") as fh:
                fh.write(r.content)
            return cache

        def done(res, err):
            if not err and res:
                show(res)

        self._bg(work, done)

    def _fm_open_file(self, f):
        """本地文件直接打开（不走 API）：url 形如 /media/相对路径"""
        rel = str(f.get("url") or "").replace("/media/", "", 1)
        p = os.path.join(config.MEDIA_DIR, rel.replace("/", os.sep))
        try:
            if os.path.isfile(p):
                os.startfile(p)
            else:
                self._toast("文件不存在", ok=False)
        except Exception as e:
            self._toast(f"打开失败：{e!r}", ok=False)

    def _fm_open_dir(self):
        """打开本地目录（不走 API）：筛选作者时优先其作品目录，否则媒体目录根"""
        target = config.MEDIA_DIR
        if self._file_filter_sec and self._file_works:
            files = (self._file_works[0].get("files") or [])
            if files:
                rel = str(files[0].get("url") or "").replace("/media/", "", 1)
                p = os.path.join(config.MEDIA_DIR, rel.replace("/", os.sep))
                d = os.path.dirname(os.path.abspath(p))
                if os.path.isdir(d):
                    target = d
        try:
            os.makedirs(target, exist_ok=True)
            os.startfile(target)
        except Exception as e:
            self._toast(f"打开目录失败：{e!r}", ok=False)

    def _fm_delete_one(self, wk):
        cid = wk.get("content_id") or wk["id"]
        if not mb.askyesno("确认删除",
                           f"确认删除作品 {cid} 的本地文件？\n（删除后同步不会再自动拉回；DTK 原始记录保留）"):
            return

        def work():
            res = call("DELETE", f"/api/files/{wk['id']}")
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已删除") if res and "detail" not in res
                        else f"删除失败：{err}", ok=not err)
            if not err:
                self.load_files()
        self._bg(work, cb)

    def _fm_batch_delete(self):
        ids = [k for k, v in self._file_sel.items() if v.get()]
        if not ids:
            self._toast("请先勾选要删除的作品", ok=False)
            return
        if not mb.askyesno("批量删除",
                           f"确认删除选中的 {len(ids)} 个作品的本地文件？\n（删除后同步不会再自动拉回；DTK 原始记录保留）"):
            return

        def work():
            res = call("POST", "/api/files/delete-batch", json={"ids": ids})
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已删除") if res and "detail" not in res
                        else f"删除失败：{err}", ok=not err)
            if not err:
                self.load_files()
        self._bg(work, cb)

    def _goto_files(self, a):
        """作者卡片「文件」按钮：跳文件管理并筛出该作者"""
        self._file_filter_sec = a["sec_uid"]
        self._show_page(2)  # load_files -> 汇总卡 + 按当前筛选浏览
        self._fm_cur_label.configure(
            text=disp(a.get("nickname") or a["sec_uid"])[:12])

    # ================= 页4：同步日志 =================
    def _build_page_logs(self):
        pg = ctk.CTkFrame(self._content, fg_color="transparent")
        top = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=10)
        top.pack(fill="x", padx=18, pady=(16, 8))
        self._log_summary = ctk.CTkLabel(top, text="今日同步 -",
                                         font=ctk.CTkFont(family=FONT, size=13),
                                         text_color=C_TEXT)
        self._log_summary.pack(side="left", padx=16, pady=12)
        ctk.CTkButton(top, text="刷新", width=64, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=self.load_logs).pack(side="right", padx=14)
        ctk.CTkButton(top, text="清空日志", width=80, height=28,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_PANEL2, hover_color=C_BTN_HOVER, text_color=C_MUTED,
                      command=self.clear_logs).pack(side="right", padx=(0, 6))

        self._log_list = ctk.CTkScrollableFrame(pg, fg_color=C_BG, corner_radius=0)
        self._log_list.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        return pg

    def clear_logs(self):
        if not mb.askyesno(
                "清空日志",
                "确认清空全部同步日志？\n（仅清空同步日志记录，不影响作者、作品与已下载文件）"):
            return

        def work():
            res = call("DELETE", "/api/logs")
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已清空日志") if res and "detail" not in res
                        else f"清空失败：{err}", ok=not err)
            if not err:
                self.load_logs()
        self._bg(work, cb)

    def load_logs(self):
        self._done[3] = False
        for w in self._log_list.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._log_list, text="加载中…", text_color=C_MUTED,
                     font=ctk.CTkFont(family=FONT, size=13)).pack(pady=24)
        self._bg(lambda: call("GET", "/api/logs", params={"limit": 500}), self._on_logs)

    def _dl_msg(self, m):
        dm = re.search(r"拉回本地\s*(\d+)\s*/\s*(\d+)", str(m or ""))
        if not dm:
            return m or ""
        return "无新文件" if dm.group(1) == "0" else f"回拉 {int(dm.group(1))} 个（共 {int(dm.group(2))}）"

    def _on_logs(self, res, err):
        self._done[3] = True
        for w in self._log_list.winfo_children():
            w.destroy()
        if err:
            ctk.CTkLabel(self._log_list, text=f"加载失败：{err}", text_color=C_ERR,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=30)
            return
        logs = res.get("data", [])
        t = self._today_logs(logs)
        runs = sorted({l["run_id"] for l in t})
        # 汇总（与 Web 版同口径）
        if t:
            last_run = t[0]["run_id"]
            batch = [l for l in t if l["run_id"] == last_run]
            ok = all(l.get("status") == "ok" for l in batch)
            seg = f"最近一次 {fmt_ts(t[0]['created_at'], with_sec=True)} {'成功' if ok else '失败'}"
        else:
            seg = "今天还没有同步记录"
        x_add, y_dl = self._today_stats(logs)
        self._log_summary.configure(
            text=f"今日同步 {len(runs)} 次 · {seg} · 今日新增作品 {x_add} 个 · 回拉文件 {y_dl} 个")

        if not logs:
            ctk.CTkLabel(self._log_list, text="暂无同步日志", text_color=C_MUTED,
                         font=ctk.CTkFont(family=FONT, size=13)).pack(pady=30)
            return
        # 按 run_id 分组（保持 logs 的时间倒序）
        groups, seen = [], set()
        for l in logs:
            if l["run_id"] not in seen:
                seen.add(l["run_id"])
                groups.append((l["run_id"], [x for x in logs if x["run_id"] == l["run_id"]]))
        self._log_groups = groups
        self._detail_open = set()
        for run_id, rows in groups[:80]:
            self._add_log_group(run_id, rows)

    def _add_log_group(self, run_id, rows):
        ok_n = sum(1 for l in rows if l.get("status") == "ok")
        err_n = sum(1 for l in rows if l.get("status") == "error")
        if err_n == 0:
            mark, color = "成功", C_OK
        elif ok_n == 0:
            mark, color = "失败", C_ERR
        else:
            mark, color = "部分失败", C_WARN
        parts = []
        for l in sorted(rows, key=lambda x: x.get("id") or 0):
            step = STEP_NAME.get(l.get("step"), l.get("step") or "-")
            if l.get("status") != "ok":
                parts.append(f"{step}失败")
                continue
            if l.get("step") == "downloads":
                msg = self._dl_msg(l.get("message"))
                parts.append("文件回拉" + ("" if msg == "无新文件" else "：" + msg))
            else:
                parts.append(f"{step} {l.get('items') if l.get('items') is not None else '-'} 个")
        dur = sum(l.get("duration_ms") or 0 for l in rows) / 1000
        t0 = min(str(l.get("created_at") or "9") for l in rows)
        summary = " / ".join(parts) + f" / 共 {dur:.1f}s"

        box = ctk.CTkFrame(self._log_list, fg_color=C_PANEL, corner_radius=8)
        box.pack(fill="x", padx=6, pady=3)
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=f"{fmt_ts(t0, with_sec=True)}  {mark}",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=color).pack(side="left", padx=(12, 8), pady=8)
        ctk.CTkLabel(head, text=summary[:110], font=ctk.CTkFont(family=FONT, size=12),
                     text_color=C_TEXT, anchor="w").pack(side="left", fill="x", expand=True)
        toggle = ctk.CTkButton(head, text="明细", width=48, height=22,
                               font=ctk.CTkFont(family=FONT, size=11),
                               fg_color=C_PANEL2, hover_color=C_BTN_HOVER, text_color=C_MUTED,
                               command=lambda: self._toggle_detail(run_id, box))
        toggle.pack(side="right", padx=10)

    def _toggle_detail(self, run_id, box):
        if run_id in self._detail_open:
            self._detail_open.discard(run_id)
            for w in box.winfo_children()[1:]:
                w.destroy()
            return
        self._detail_open.add(run_id)
        rows = dict(self._log_groups)[run_id]
        detail = ctk.CTkFrame(box, fg_color=C_PANEL2, corner_radius=6)
        detail.pack(fill="x", padx=10, pady=(0, 10))
        for l in sorted(rows, key=lambda x: x.get("id") or 0):
            txt = (f"{fmt_ts(l.get('created_at'), with_sec=True)}  "
                   f"[{l.get('step')}] {l.get('status')}  items={l.get('items')}  "
                   f"{l.get('duration_ms', 0)}ms\n{disp(l.get('message'))}")
            ctk.CTkLabel(detail, text=txt, font=ctk.CTkFont(family=FONT, size=11),
                         text_color=C_MUTED, justify="left", anchor="w").pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(detail, text=f"run_id: {run_id}",
                     font=ctk.CTkFont(family="Consolas", size=10),
                     text_color="#6b7280", anchor="w").pack(fill="x", padx=10, pady=(0, 6))

    # ================= 页5：参数设置 =================
    def _build_page_settings(self):
        pg = ctk.CTkFrame(self._content, fg_color="transparent")
        box = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=12)
        box.pack(fill="x", padx=18, pady=(16, 8))
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=18)

        def row(label):
            fr = ctk.CTkFrame(inner, fg_color="transparent")
            fr.pack(fill="x", pady=6)
            ctk.CTkLabel(fr, text=label, width=180, font=ctk.CTkFont(family=FONT, size=13),
                         text_color=C_MUTED, anchor="w").pack(side="left")
            return fr

        r1 = row("API 连接地址")
        self._e_base = ctk.CTkEntry(r1, width=400, font=ctk.CTkFont(family=FONT, size=13),
                                    fg_color=C_PANEL2, border_color="#3a4150")
        self._e_base.pack(side="left")
        r2 = row("API Key")
        self._e_key = ctk.CTkEntry(r2, width=400, font=ctk.CTkFont(family=FONT, size=13),
                                   fg_color=C_PANEL2, border_color="#3a4150")
        self._e_key.pack(side="left")
        r3 = row("自动同步间隔（分钟）")
        self._e_interval = ctk.CTkEntry(r3, width=120, font=ctk.CTkFont(family=FONT, size=13),
                                        fg_color=C_PANEL2, border_color="#3a4150")
        self._e_interval.pack(side="left")
        ctk.CTkLabel(r3, text="（改完立即按新间隔运行）",
                     font=ctk.CTkFont(family=FONT, size=11), text_color="#6b7280").pack(side="left", padx=8)
        r4 = row("新作者默认刷新间隔（分钟）")
        self._e_refresh = ctk.CTkEntry(r4, width=120, font=ctk.CTkFont(family=FONT, size=13),
                                       fg_color=C_PANEL2, border_color="#3a4150")
        self._e_refresh.pack(side="left")
        r5 = row("新作者默认自动下载")
        self._sw_auto = ctk.CTkSwitch(r5, text="", progress_color=C_PRIMARY)
        self._sw_auto.pack(side="left")
        r6 = row("下载文件夹")
        self._e_media = ctk.CTkEntry(r6, width=400, font=ctk.CTkFont(family=FONT, size=13),
                                     fg_color=C_PANEL2, border_color="#3a4150")
        self._e_media.pack(side="left")
        ctk.CTkButton(r6, text="浏览…", width=64, height=26,
                      font=ctk.CTkFont(family=FONT, size=11),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self._browse_media).pack(side="left", padx=8)
        ctk.CTkLabel(r6, text="（留空 = 回落默认；保存后新下载即时落新目录）",
                     font=ctk.CTkFont(family=FONT, size=11), text_color="#6b7280").pack(side="left")

        act = ctk.CTkFrame(inner, fg_color="transparent")
        act.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(act, text="保 存", width=110, height=34,
                      font=ctk.CTkFont(family=FONT, size=14, weight="bold"),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=self._save_settings).pack(side="left")
        ctk.CTkLabel(act, text="保存后立即生效，重启后依然有效",
                     font=ctk.CTkFont(family=FONT, size=11), text_color="#6b7280").pack(side="left", padx=12)

        # ---- 数据库切换卡 ----
        dbbox = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=12)
        dbbox.pack(fill="x", padx=18, pady=8)
        dbinner = ctk.CTkFrame(dbbox, fg_color="transparent")
        dbinner.pack(fill="x", padx=24, pady=14)

        head = ctk.CTkFrame(dbinner, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text="数据库", width=180, font=ctk.CTkFont(family=FONT, size=13),
                     text_color=C_MUTED, anchor="w").pack(side="left")
        self._db_type_menu = ctk.CTkOptionMenu(
            head, width=150, values=["本地 SQLite", "远程 MySQL"],
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color=C_PANEL2, button_color=C_PANEL2, button_hover_color=C_BTN_HOVER,
            text_color=C_TEXT, command=self._on_db_type_change)
        self._db_type_menu.pack(side="left")
        ctk.CTkButton(head, text="测试连接", width=88, height=26,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_BTN, hover_color=C_BTN_HOVER, text_color=C_TEXT,
                      command=self._test_db_config).pack(side="right", padx=(0, 8))
        ctk.CTkButton(head, text="保存切换（重启生效）", width=150, height=26,
                      font=ctk.CTkFont(family=FONT, size=12),
                      fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      command=self._save_db_config).pack(side="right")

        # sqlite 分支字段
        self._db_sqlite_fr = ctk.CTkFrame(dbinner, fg_color="transparent")
        ctk.CTkLabel(self._db_sqlite_fr, text="库文件名", width=180,
                     font=ctk.CTkFont(family=FONT, size=13), text_color=C_MUTED,
                     anchor="w").pack(side="left")
        self._e_dbfile = ctk.CTkEntry(self._db_sqlite_fr, width=300,
                                      font=ctk.CTkFont(family=FONT, size=13),
                                      fg_color=C_PANEL2, border_color="#3a4150")
        self._e_dbfile.pack(side="left")
        ctk.CTkLabel(self._db_sqlite_fr, text="（相对路径 = config.json 同目录）",
                     font=ctk.CTkFont(family=FONT, size=11), text_color="#6b7280").pack(side="left", padx=8)
        # mysql 分支字段
        self._db_mysql_fr = ctk.CTkFrame(dbinner, fg_color="transparent")
        for label, key, w in [("主机", "host", 160), ("端口", "port", 70),
                              ("用户", "user", 110), ("密码", "password", 110),
                              ("库名", "database", 110)]:
            ctk.CTkLabel(self._db_mysql_fr, text=label,
                         font=ctk.CTkFont(family=FONT, size=12), text_color=C_MUTED).pack(side="left")
            e = ctk.CTkEntry(self._db_mysql_fr, width=w,
                             font=ctk.CTkFont(family=FONT, size=12),
                             fg_color=C_PANEL2, border_color="#3a4150",
                             show="*" if key == "password" else "")
            e.pack(side="left", padx=(2, 10))
            setattr(self, f"_e_db_{key}", e)
        cur_cfg = config.CFG.get("db") or {}
        is_sqlite = getattr(db, "BACKEND", "") == "sqlite"
        self._e_dbfile.insert(0, cur_cfg.get("file") or "douyin.db")
        self._e_db_host.insert(0, cur_cfg.get("host") or "")
        self._e_db_port.insert(0, str(cur_cfg.get("port") or 3306))
        self._e_db_user.insert(0, cur_cfg.get("user") or "")
        self._e_db_password.insert(0, "******" if cur_cfg.get("password") else "")
        self._e_db_database.insert(0, cur_cfg.get("database") or "")
        self._on_db_type_change("本地 SQLite" if is_sqlite else "远程 MySQL")

        # 只读信息行
        info = ctk.CTkFrame(pg, fg_color=C_PANEL, corner_radius=12)
        info.pack(fill="x", padx=18, pady=8)
        media = config.MEDIA_DIR
        if getattr(db, "BACKEND", "") == "sqlite":
            txt = (f"当前本地库  {getattr(db, 'SQLITE_FILE', '')}        "
                   f"媒体目录  {media}")
        else:
            dbcfg = config.DB
            txt = (f"当前数据库  {dbcfg.get('host')}:{dbcfg.get('port')}  /  {dbcfg.get('database')}        "
                   f"媒体目录  {media}")
        ctk.CTkLabel(info, text=txt, font=ctk.CTkFont(family=FONT, size=12),
                     text_color=C_MUTED, anchor="w").pack(fill="x", padx=18, pady=12)
        return pg

    def _browse_media(self):
        d = fd.askdirectory(parent=self, title="选择下载文件夹",
                            initialdir=self._e_media.get().strip() or config.MEDIA_DIR or ".")
        if d:
            self._e_media.delete(0, "end")
            self._e_media.insert(0, d.replace("/", "\\"))

    def _on_db_type_change(self, val):
        self._db_sqlite_fr.pack_forget()
        self._db_mysql_fr.pack_forget()
        (self._db_sqlite_fr if val == "本地 SQLite" else self._db_mysql_fr).pack(fill="x", pady=(8, 0))

    def _collect_db_body(self):
        t = "sqlite" if self._db_type_menu.get() == "本地 SQLite" else "mysql"
        body = {"type": t, "file": self._e_dbfile.get().strip()}
        if t == "mysql":
            body.update({
                "host": self._e_db_host.get().strip(),
                "port": self._e_db_port.get().strip() or 3306,
                "user": self._e_db_user.get().strip(),
                "password": self._e_db_password.get(),
                "database": self._e_db_database.get().strip(),
            })
        return body

    def _save_db_config(self):
        body = self._collect_db_body()

        def work():
            res = call("POST", "/api/db-config", json=body)
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已保存") if res and "detail" not in res
                        else f"保存失败：{err}", ok=not err)
        self._bg(work, cb)

    def _test_db_config(self):
        self._toast("正在测试连接…", ok=True)
        body = self._collect_db_body()

        def work():
            return call("POST", "/api/db-test", json=body)

        def cb(res, err):
            if err:
                self._toast(f"测试失败：{err}", ok=False)
                return
            ok = res.get("code") == 0
            self._toast(res.get("message", f"测试失败：{err}"), ok=ok)
        self._bg(work, cb)

    def load_settings(self):
        self._bg(lambda: call("GET", "/api/settings"), self._on_settings)

    def _on_settings(self, res, err):
        self._done[4] = True
        if err or "detail" in (res or {}):
            self._toast(f"读取设置失败：{err or res.get('detail')}", ok=False)
            return
        d = res.get("data", {})
        self._e_base.delete(0, "end"); self._e_base.insert(0, d.get("dtk_base") or "")
        self._e_key.delete(0, "end"); self._e_key.insert(0, d.get("dtk_key") or "")
        self._e_interval.delete(0, "end"); self._e_interval.insert(0, str(d.get("sync_interval_min") or 30))
        self._e_refresh.delete(0, "end"); self._e_refresh.insert(0, str(d.get("default_refresh_interval_min") or 360))
        self._e_media.delete(0, "end"); self._e_media.insert(0, d.get("media_dir") or "")
        (self._sw_auto.select if d.get("default_auto_download") else self._sw_auto.deselect)()

    def _save_settings(self):
        base = self._e_base.get().strip()
        key = self._e_key.get().strip()
        if base and not base.startswith(("http://", "https://")):
            self._toast("API 连接地址必须以 http:// 或 https:// 开头", ok=False)
            return
        try:
            interval = int(self._e_interval.get().strip())
            refresh = int(self._e_refresh.get().strip())
        except ValueError:
            self._toast("间隔必须是数字", ok=False)
            return
        body = {"sync_interval_min": interval, "default_refresh_interval_min": refresh,
                "default_auto_download": bool(self._sw_auto.get()),
                "dtk_base": base, "dtk_key": key,
                "media_dir": self._e_media.get().strip()}

        def work():
            res = call("PATCH", "/api/settings", json=body)
            if "detail" in res:
                raise RuntimeError(res["detail"])
            return res

        def cb(res, err):
            self._toast(res.get("message", "已保存") if res and "detail" not in res
                        else f"保存失败：{err}", ok=not err)
        self._bg(work, cb)

    # ================= DB 状态 / 截图 =================
    def _ping_db(self):
        if getattr(db, "BACKEND", "") == "sqlite":
            def work():
                conn = db.conn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) AS n FROM dm_authors")
                    return cur.fetchone()["n"]
                finally:
                    conn.close()

            self._bg(work, self._on_db_sqlite)
            return
        cfg = dict(config.DB)

        def work():
            conn = db.conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
            finally:
                conn.close()
            return cfg["host"]

        self._bg(work, self._on_db)

    def _on_db_sqlite(self, res, err):
        self._db_done = True
        if err:
            self._lbl_db.configure(text="本地库 打开失败", text_color=C_ERR)
        else:
            name = os.path.basename(getattr(db, "SQLITE_FILE", "") or "douyin.db")
            self._lbl_db.configure(text=f"本地库 已连接 ({name})", text_color=C_OK)

    def _on_db(self, res, err):
        self._db_done = True
        if res:
            self._lbl_db.configure(text=f"数据库 已连接（{res}）", text_color=C_OK)
        else:
            self._lbl_db.configure(text="数据库 连接失败", text_color=C_ERR)

    # ---- screenshot 验证模式 ----
    def _shot_flow_check(self):
        self._shot_t0 = getattr(self, "_shot_t0", None) or datetime.datetime.now()
        if (datetime.datetime.now() - self._shot_t0).total_seconds() > 30:
            self._shot_page(0)
        elif self._done[0] and self._db_done:
            self._shot_page(0)
        else:
            self.after(300, self._shot_flow_check)

    def _shot_page(self, idx):
        # 切页（_show_page 内部触发该页数据加载）
        self._show_page(idx)
        self.update_idletasks()
        self._shot_start = time.time()
        self._shot_wait(idx)

    def _shot_wait(self, idx):
        """等待当前页数据 + 侧边栏 DB 状态行就绪；DB 行上限 10s、页面数据上限 30s，超时照截并记日志"""
        elapsed = time.time() - self._shot_start
        if not self._db_done and elapsed >= 10:
            print("[gui] WARN: db status row not ready in 10s, screenshot anyway", flush=True)
            self._db_done = True
        if not (self._done[idx] and self._db_done) and elapsed < 30:
            self.after(300, lambda: self._shot_wait(idx))
            return
        if not self._done[idx]:
            print(f"[gui] WARN: page{idx + 1} data not ready in 30s, screenshot anyway", flush=True)
        self.after(1000, lambda: self._grab_and_next(idx))

    def _grab_and_next(self, idx, tries=0):
        try:
            x, y = self.winfo_rootx(), self.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            import io as _io
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
            # 桌面锁屏（壁纸照片 >400KB）或黑屏（<8KB）时重试，等解锁（最多 ~15×4s）
            if (len(data) > 400_000 or len(data) < 8_000) and tries < 15:
                print(f"[gui] shot{idx} looks locked/black ({len(data)}B), retry {tries + 1}",
                      flush=True)
                self.after(4000, lambda: self._grab_and_next(idx, tries + 1))
                return
            name = f"page{idx + 1}_{PAGES[idx]}.png"
            with open(os.path.join(self._shot_dir, name), "wb") as f:
                f.write(data)
            print(f"[gui] screenshot saved: {name} ({len(data)}B)", flush=True)
        except Exception as e:
            print(f"[gui] screenshot {idx} failed: {e!r}", flush=True)
        if idx == 2 and self._file_works and not getattr(self, "_shot_pv_done", False):
            self._shot_pv_done = True
            self._shot_preview()
            return
        if idx + 1 < len(PAGES):
            self.after(300, lambda: self._shot_page(idx + 1))
        else:
            self.after(500, self._shot_finish)

    def _shot_preview(self, stage=0, tries=0):
        """页3 之后额外截预览弹窗（有视频优先截视频，验证内嵌播放）"""
        if stage == 0:
            wk = None
            for w in self._file_works:
                if any((f.get("name") or "").rsplit(".", 1)[-1].lower() in PV_VID
                       for f in w.get("files") or []):
                    wk = w
                    break
            wk = wk or self._file_works[0]
            fi = 0
            for i, f in enumerate(wk.get("files") or []):
                if (f.get("name") or "").rsplit(".", 1)[-1].lower() in PV_VID:
                    fi = i
                    break
            # 截图模式主窗设了 -topmost，transient 弹窗会被压在下面截不到：
            # 截图期间临时取消主窗置顶并把弹窗顶到最前（仅影响验证流程）
            try:
                self.attributes("-topmost", False)
            except Exception:
                pass
            self._preview_from_work(wk, fi)
            try:
                pv = getattr(self, "_pv_win", None)
                if pv is not None and pv.winfo_exists():
                    pv.attributes("-topmost", True)
                    pv.lift()
                    pv.focus_force()
            except Exception:
                pass
            self.after(4000, lambda: self._shot_preview(1))
            return
        win = getattr(self, "_pv_win", None)
        try:
            if win is not None and win.winfo_exists():
                x, y = win.winfo_rootx(), win.winfo_rooty()
                w, h = win.winfo_width(), win.winfo_height()
                img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
                import io as _io
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
                if len(data) < 8_000 and tries < 8:
                    print(f"[gui] preview shot looks black ({len(data)}B), retry", flush=True)
                    self.after(4000, lambda: self._shot_preview(1, tries + 1))
                    return
                with open(os.path.join(self._shot_dir, "page3b_预览.png"), "wb") as fh:
                    fh.write(data)
                print(f"[gui] screenshot saved: page3b_预览.png ({len(data)}B)", flush=True)
                win.close()
        except Exception as e:
            print(f"[gui] preview shot failed: {e!r}", flush=True)
        try:
            self.attributes("-topmost", True)     # 恢复主窗置顶，供页4/页5 截图
        except Exception:
            pass
        self.after(400, lambda: self._shot_page(3))

    def _shot_finish(self):
        """五页截完：退出 mainloop 并强制结束进程（windowed 下非 daemon 线程会挂住解释器）"""
        print("[gui] screenshot flow done, exiting", flush=True)
        self._closing = True
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def _on_close(self):
        self._closing = True
        try:
            self.destroy()
        finally:
            os._exit(0)
