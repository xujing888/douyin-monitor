"""抖音采集系统 - Windows 桌面版启动器（PyInstaller 入口）。

2026-10-06 架构变更：默认 GUI 模式不再启动本地 HTTP 服务（不监听任何端口），
GUI 通过 api_bridge 进程内直调业务函数；--server-only 保留 uvicorn 供 QA/调试。

用法：
    抖音采集系统.exe                正常 GUI 模式（默认，无端口监听）
    抖音采集系统.exe --server-only  仅启动后台 API 服务（控制台日志，QA 用）
    抖音采集系统.exe --screenshot <dir>  GUI 验证模式：自动切五个页面截图后退出
"""
import argparse
import os
import socket
import sys
import threading
import time

# windowed 模式下 stdout/stderr 为 None，logging/print 会炸，先重定向到 exe 同目录日志
_logfile = None


def _redirect_stdio():
    global _logfile
    if sys.stdout is None or sys.stderr is None:
        try:
            base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
            _logfile = open(os.path.join(base, "launcher.log"), "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = _logfile
            if sys.stderr is None:
                sys.stderr = _logfile
        except Exception:
            pass


_redirect_stdio()

from app import config as _cfg  # noqa: E402

# import app.main 的副作用：db.ensure_schema() 建表/补列（幂等）、媒体目录创建；
# 调度器由 config.json scheduler_enabled 门控（交付包为 false 不会启动），无需 startup 事件
PORT_START = 7788


def pick_port(start=PORT_START, tries=50):
    """从 start 起探测第一个可用端口（--server-only 用）"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"端口 {start}-{start + tries - 1} 均被占用")


def wait_ready(port, timeout=90):
    """等 API 可响应（任意 HTTP 响应即认为 uvicorn 已就绪）"""
    import httpx
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/settings", timeout=2.0,
                          trust_env=False)  # 本地回环绝不能走系统代理
            if r.status_code < 600:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def start_server():
    import uvicorn
    from app.main import app
    port = pick_port()
    t = threading.Thread(
        target=uvicorn.run, args=(app,),
        kwargs={"host": "127.0.0.1", "port": port, "log_level": "info"},
        daemon=True, name="uvicorn-server",
    )
    t.start()
    return port


def main():
    ap = argparse.ArgumentParser(description="抖音采集系统桌面版")
    ap.add_argument("--server-only", action="store_true", help="只启动 API 服务，不启动 GUI（QA 用）")
    ap.add_argument("--screenshot", metavar="DIR", help="GUI 验证模式：自动截图五个页面到目录后退出")
    args = ap.parse_args()

    print(f"[launcher] MEDIA_DIR = {_cfg.MEDIA_DIR}", flush=True)
    print(f"[launcher] CONFIG    = {os.path.abspath(_cfg._CFG_PATH)}", flush=True)
    print(f"[launcher] DB        = {_cfg.DB.get('type')}", flush=True)

    if args.server_only:
        port = start_server()
        print(f"[launcher] API 服务已启动: http://127.0.0.1:{port}", flush=True)
        if not wait_ready(port):
            print("[launcher] 服务就绪探测超时，但仍保持运行", flush=True)
        else:
            print("[launcher] 服务就绪", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("[launcher] 退出", flush=True)
        return

    # GUI / screenshot 模式：进程内直调，不起 uvicorn、不监听端口
    from gui import App

    screenshot_dir = None
    if args.screenshot:
        screenshot_dir = os.path.abspath(args.screenshot)
        os.makedirs(screenshot_dir, exist_ok=True)

    app_win = App(screenshot_dir=screenshot_dir)
    app_win.mainloop()


if __name__ == "__main__":
    main()
