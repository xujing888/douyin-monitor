import json
import os

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
with open(_CFG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

DTK_BASE = CFG["dtk_base"].rstrip("/")
DTK_KEY = CFG["dtk_key"]
DB = dict(CFG["db"])
SYNC_INTERVAL_MIN = int(CFG.get("sync_interval_min", 30))
ARCHIVE_PAGES = int(CFG.get("archive_pages", 5))
DOWNLOAD_PAGES = int(CFG.get("download_pages", 5))
MEDIA_DIR = CFG.get("media_dir", os.path.join(os.path.dirname(_CFG_PATH), "media"))
