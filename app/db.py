"""数据库连接适配层：双后端（sqlite 桌面本地库 / mysql 服务器版）。

调用方用法与原 pymysql 版完全一致：
    conn = db.conn()
    cur = conn.cursor()
    cur.execute("... %s ...", params)   # %s 占位符；datetime 参数自动转字符串
    rows = cur.fetchall()               # 行为纯 dict（r["col"] 取值，可原地改键）
                                        # fetchone 无行时返回 None

config.json db 段：
    {"type": "sqlite", "file": "douyin.db"}             桌面版：本地 SQLite
    {"type": "mysql", "host": ..., "port": ..., ...}    服务器版：MySQL
type 缺省时向后兼容：有 host 字段即按 mysql（现网 config.json 无需改动）。

SQLite 文件相对路径基准 = 实际加载的 config.json 所在目录
（frozen 且 config 落在 _MEIPASS 内置兜底时改用 exe 同目录，保证可持久）。
SQLite 模式首次启动自动执行 ensure_schema() 建全套表与索引（幂等）。

MySQL 方言由 _to_sqlite() 做文本级机械转译，仅覆盖本项目实际用到的写法：
    INSERT IGNORE INTO          -> INSERT OR IGNORE INTO
    NOW()                       -> datetime('now','localtime')（本地时区，格式同 Python adapter）
    GREATEST(a,b)               -> max(a,b)（SQLite 多参标量 max；忽略 NULL 的差异不影响本项目：参数先经 COALESCE）
    <col> + INTERVAL <n> MINUTE -> datetime(<col>, '+' || <n> || ' minutes')
    ON DUPLICATE KEY UPDATE ... -> ON CONFLICT(<冲突键>) DO UPDATE SET ...（冲突键见 _CONFLICT_KEYS）
    VALUES(col)（仅 upsert 尾段）-> excluded.col
"""
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime

from . import config

log = logging.getLogger("douyin-monitor.db")

# ---------------- 后端判定 ----------------
_type = str(config.DB.get("type") or "").strip().lower()
if not _type:
    _type = "mysql" if config.DB.get("host") else "sqlite"
if _type not in ("sqlite", "mysql"):
    raise RuntimeError(f"config.json db.type 非法: {_type!r}（只支持 sqlite/mysql）")
BACKEND = _type


def _sqlite_base_dir():
    """SQLite 相对路径的基准目录 = 实际加载的 config.json 所在目录；
    frozen 且该文件其实是 _MEIPASS 内置兜底时，回落 exe 同目录（onefile
    临时解包目录进程退出即删，落那里数据库会丢）。"""
    p = os.path.abspath(config._CFG_PATH)
    if getattr(sys, "frozen", False):
        meipass = os.path.abspath(getattr(sys, "_MEIPASS", "") or "")
        if meipass and p.startswith(meipass):
            return os.path.dirname(sys.executable)
    return os.path.dirname(p)


def sqlite_path_for(file):
    """把 config 里的 sqlite file 字段解析为绝对路径（相对路径按基准目录展开）"""
    f = str(file or "douyin.db").strip() or "douyin.db"
    if os.path.isabs(f):
        return f
    return os.path.join(_sqlite_base_dir(), f)


SQLITE_FILE = sqlite_path_for(config.DB.get("file")) if BACKEND == "sqlite" else None

# datetime 参数统一转 "YYYY-MM-DD HH:MM:SS"（建表 DDL 中 datetime 列均为 TEXT，格式一致可比较）
sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))

# ---------------- MySQL -> SQLite 方言转译 ----------------

# upsert 冲突键（= MySQL ON DUPLICATE KEY UPDATE 的判定键，与 SQLite DDL 的主键/唯一键一致）
_CONFLICT_KEYS = {
    "dm_settings": "skey",
    "dm_authors": "sec_uid",
    "dm_author_snapshots": "sec_uid, taken_at",
    "dm_contents": "platform, content_id",
    "dm_downloads": "id",
}

_RE_INSERT_IGNORE = re.compile(r"\bINSERT\s+IGNORE\s+INTO\b", re.I)
_RE_INTERVAL_MIN = re.compile(r"\b(\w+)\s*\+\s*INTERVAL\s+(\w+)\s+MINUTE\b", re.I)
_RE_NOW = re.compile(r"\bNOW\(\)")
_RE_GREATEST = re.compile(r"\bGREATEST\(")
_RE_PLACEHOLDER = re.compile(r"%s")
_RE_INSERT_INTO = re.compile(r"\bINSERT\s+INTO\s+`?(\w+)`?", re.I)
_RE_ON_DUP = re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.I)
_RE_VALUES_COL = re.compile(r"\bVALUES\((\w+)\)")


def _to_sqlite(sql):
    """把本项目用到的 MySQL 方言机械转译为 SQLite 方言（详见模块 docstring）。"""
    sql = _RE_INSERT_IGNORE.sub("INSERT OR IGNORE INTO", sql)
    sql = _RE_INTERVAL_MIN.sub(r"datetime(\1, '+' || \2 || ' minutes')", sql)
    sql = _RE_NOW.sub("datetime('now','localtime')", sql)
    sql = _RE_GREATEST.sub("max(", sql)
    sql = _RE_PLACEHOLDER.sub("?", sql)
    m = _RE_ON_DUP.search(sql)
    if m:
        mt = _RE_INSERT_INTO.search(sql)
        key = _CONFLICT_KEYS.get(mt.group(1)) if mt else None
        if not key:
            raise RuntimeError(
                f"sqlite 方言转译失败：upsert 冲突键未登记, table={mt.group(1) if mt else '?'}")
        tail = _RE_VALUES_COL.sub(r"excluded.\1", sql[m.end():])
        sql = sql[:m.start()] + f"ON CONFLICT({key}) DO UPDATE SET " + tail.strip()
    return sql

# ---------------- 连接与游标包装 ----------------


def _open_sqlite(path):
    raw = sqlite3.connect(path, timeout=30, isolation_level=None)  # isolation_level=None == autocommit
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=30000")
    return raw


class _SQLiteCursor:
    def __init__(self, raw_cur):
        self._cur = raw_cur

    def execute(self, sql, params=()):
        if params is None:
            params = ()
        self._cur.execute(_to_sqlite(sql), tuple(params))

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else dict(r)

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    def close(self):
        self._cur.close()


class _SQLiteConn:
    def __init__(self, path):
        self._raw = _open_sqlite(path)

    def cursor(self):
        return _SQLiteCursor(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def conn():
    """返回与后端匹配的连接（调用方用法不变）。每次调用新建连接，用完需 close()。"""
    if BACKEND == "sqlite":
        d = os.path.dirname(SQLITE_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        return _SQLiteConn(SQLITE_FILE)
    import pymysql
    return pymysql.connect(
        host=config.DB["host"],
        port=int(config.DB["port"]),
        user=config.DB["user"],
        password=config.DB["password"],
        database=config.DB["database"],
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )

# ---------------- SQLite 建表（与 1.15 MySQL DDL 等价转译） ----------------

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS dm_authors (
  sec_uid TEXT PRIMARY KEY,
  nickname TEXT,
  unique_id TEXT,
  signature TEXT,
  avatar_url TEXT,
  web_url TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  watch_kind TEXT,
  ip_location TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_follower_count INTEGER,
  last_following_count INTEGER,
  last_total_digg INTEGER,
  last_content_count INTEGER,
  first_seen_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  is_private INTEGER NOT NULL DEFAULT 0,
  refresh_interval_min INTEGER NOT NULL DEFAULT 360,
  last_refreshed_at TEXT,
  auto_download INTEGER NOT NULL DEFAULT 1,
  change_flag INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dm_author_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sec_uid TEXT NOT NULL,
  follower_count INTEGER,
  following_count INTEGER,
  total_digg INTEGER,
  content_count INTEGER,
  taken_at TEXT NOT NULL,
  UNIQUE (sec_uid, taken_at)
);
CREATE INDEX IF NOT EXISTS idx_snap_taken ON dm_author_snapshots(taken_at);

CREATE TABLE IF NOT EXISTS dm_contents (
  platform TEXT NOT NULL DEFAULT 'douyin',
  content_id TEXT NOT NULL,
  sec_uid TEXT,
  nickname TEXT,
  kind TEXT,
  title TEXT,
  description TEXT,
  web_url TEXT,
  cover_url TEXT,
  music_title TEXT,
  tags TEXT,
  availability TEXT,
  published_at TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  digg_count INTEGER,
  comment_count INTEGER,
  share_count INTEGER,
  collect_count INTEGER,
  play_count INTEGER,
  is_deleted INTEGER NOT NULL DEFAULT 0,
  is_private INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (platform, content_id)
);
CREATE INDEX IF NOT EXISTS idx_contents_sec ON dm_contents(sec_uid);
CREATE INDEX IF NOT EXISTS idx_contents_published ON dm_contents(published_at);

CREATE TABLE IF NOT EXISTS dm_downloads (
  id TEXT PRIMARY KEY,
  platform TEXT DEFAULT 'douyin',
  content_id TEXT,
  sec_uid TEXT,
  state TEXT,
  bytes_total INTEGER,
  file_count INTEGER,
  directory TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  synced_at TEXT DEFAULT (datetime('now','localtime')),
  local_dir TEXT,
  pulled_at TEXT,
  local_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_downloads_content ON dm_downloads(content_id);
CREATE INDEX IF NOT EXISTS idx_downloads_state ON dm_downloads(state);

CREATE TABLE IF NOT EXISTS dm_settings (
  skey TEXT PRIMARY KEY,
  svalue TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  items INTEGER DEFAULT 0,
  message TEXT,
  duration_ms INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_synclog_run ON dm_sync_log(run_id);
CREATE INDEX IF NOT EXISTS idx_synclog_created ON dm_sync_log(created_at);
"""


def _ensure_ip_column_sqlite(raw):
    """存量库迁移：dm_authors 无 ip_location 列则补（幂等；2026-10-06 新增，
    sync.py 已落库该字段，旧库不迁移会报 no such column）。"""
    cols = {r["name"] for r in raw.execute("PRAGMA table_info(dm_authors)").fetchall()}
    if "ip_location" not in cols:
        raw.execute("ALTER TABLE dm_authors ADD COLUMN ip_location TEXT")
        log.info("sqlite migrated: dm_authors + ip_location")


def _ensure_ip_column_mysql():
    """MySQL 存量库同列检查（幂等）：无 ip_location 则 ALTER TABLE 补上。"""
    try:
        c = conn()
        try:
            with c.cursor() as cur:
                cur.execute("SHOW COLUMNS FROM dm_authors LIKE 'ip_location'")
                if not cur.fetchone():
                    cur.execute("ALTER TABLE dm_authors ADD COLUMN ip_location TEXT")
                    log.info("mysql migrated: dm_authors + ip_location")
        finally:
            c.close()
    except Exception as e:
        log.warning("mysql ip_location column check failed: %r", e)


def ensure_schema():
    """SQLite 模式建全套表与索引 + 存量库补列（幂等）；
    MySQL 模式仅做 dm_authors.ip_location 补列检查（表结构服务器端已建）。"""
    if BACKEND != "sqlite":
        _ensure_ip_column_mysql()
        return
    d = os.path.dirname(SQLITE_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    raw = _open_sqlite(SQLITE_FILE)
    try:
        raw.executescript(_DDL_SQLITE)
        _ensure_ip_column_sqlite(raw)
        log.info("sqlite schema ready: %s", SQLITE_FILE)
    finally:
        raw.close()


def probe_sqlite(path):
    """验证指定 SQLite 文件可创建/可写并补建表结构（db-test 用，不影响当前连接）。
    返回 (ok, dm_authors 行数 或 异常文本)。"""
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        raw = _open_sqlite(path)
        try:
            raw.executescript(_DDL_SQLITE)
            _ensure_ip_column_sqlite(raw)
            n = raw.execute("SELECT COUNT(*) AS n FROM dm_authors").fetchone()["n"]
            return True, n
        finally:
            raw.close()
    except Exception as e:
        return False, repr(e)


def probe_mysql(host, port, user, password, database):
    """验证 MySQL 连接（db-test 用）。返回 (ok, 描述 或 异常文本)。"""
    try:
        import pymysql
        c = pymysql.connect(host=host, port=int(port), user=user, password=password,
                            database=database, charset="utf8mb4", connect_timeout=5,
                            cursorclass=pymysql.cursors.DictCursor)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM dm_authors")
                n = cur.fetchone()["n"]
        finally:
            c.close()
        return True, n
    except Exception as e:
        return False, repr(e)


ensure_schema()
