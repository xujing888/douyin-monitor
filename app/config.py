import json
import os
import sys


def _find_cfg_path():
    """config.json 查找顺序：
    - PyInstaller 打包环境（sys.frozen）：优先 exe 同目录的 config.json（用户可自行修改），
      不存在则回落打包时内置的 config.json（sys._MEIPASS 根目录）。
    - 源码运行：行为完全不变（仓库根目录 config.json）。
    """
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        p = os.path.join(exe_dir, "config.json")
        if os.path.isfile(p):
            return p
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, "config.json")
        return p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


_CFG_PATH = _find_cfg_path()
CFG_PATH = _CFG_PATH


def writable_cfg_path():
    """config.json 的可写落点：正常情况 = 实际加载路径；frozen 且实际加载的是
    _MEIPASS 内置兜底时 = exe 同目录（_MEIPASS 是临时解包目录不可持久，
    写到 exe 同目录后下次启动按优先级自然生效）。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.abspath(_CFG_PATH).startswith(os.path.abspath(meipass)):
            return os.path.join(os.path.dirname(sys.executable), "config.json")
    return _CFG_PATH


with open(_CFG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

DTK_BASE = CFG["dtk_base"].rstrip("/")
DTK_KEY = CFG["dtk_key"]
DB = dict(CFG["db"])
SYNC_INTERVAL_MIN = int(CFG.get("sync_interval_min", 30))
ARCHIVE_PAGES = int(CFG.get("archive_pages", 5))
DOWNLOAD_PAGES = int(CFG.get("download_pages", 5))


def _media_dir_default():
    """media 目录回落值：
    - frozen：固定 exe 同目录/media（_MEIPASS 是 onefile 临时解包目录，进程退出即删，
      落那里会导致下载文件丢失）；仅当显式配置 media_dir 时才可指向别处。
    - 非 frozen：跟随实际加载的 config.json 所在目录（源码/服务器版行为不变）。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "media")
    return os.path.join(os.path.dirname(_CFG_PATH), "media")


MEDIA_DIR = CFG.get("media_dir", _media_dir_default())
