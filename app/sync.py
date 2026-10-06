"""同步服务：本项目独立名单 -> 调 DTK 数据接口 -> 落库 1.15 douyin_monitor。
作者名单完全由本项目 dm_authors 管理，不读写 DTK 的 watchlist；
内容直接调 DTK 的 user/posts 数据接口按名单自抓；下载走 DTK 下载 API，
下载完成的文件自动拉回本项目 media 目录按作者归类。"""
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime

import httpx

from . import config, db

log = logging.getLogger("douyin-monitor.sync")


def _dt(s):
    """ISO 字符串(可能带时区) -> 本地 naive datetime；失败返回 None"""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.replace(tzinfo=None, microsecond=0)
    except Exception:
        return None


class DtkClient:
    """DTK 数据接口客户端（只把它当 API/调试台用）"""

    def __init__(self):
        self.http = httpx.Client(
            base_url=config.DTK_BASE,
            headers={"X-API-Key": config.DTK_KEY},
            timeout=30,
        )

    def _get(self, path, params=None, retries=8):
        """GET + 429 限流自动等待重试（DTK 限 120 次/分钟）"""
        for _ in range(retries):
            r = self.http.get(path, params=params)
            if r.status_code == 429:
                time.sleep(self._retry_after(r))
                continue
            r.raise_for_status()
            return r
        raise TimeoutError(f"rate limited too long: {path}")

    def _task(self, path, params):
        """提交异步任务并轮询到完成，返回 data 部分"""
        r = self._get(path, params=params)
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"task submit failed: {j}")
        tid = (j.get("data") or {}).get("task_id")
        if not tid:
            raise RuntimeError(f"no task_id: {j}")
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            t = self._get(f"/api/v1/tasks/{tid}").json()
            d = t.get("data") or {}
            st = d.get("state")
            if st == "done":
                return d.get("data") or {}
            if st in ("failed", "error", "cancelled"):
                raise RuntimeError(f"task {st}: {d.get('error')}")
        raise TimeoutError(f"task {tid} timeout")

    def user_profile(self, sec_uid):
        """作者资料（异步任务，include_raw 带原始字段：secret=私密标志等）"""
        return self._task("/api/v1/douyin/user",
                          {"sec_user_id": sec_uid, "include_raw": True})

    def user_posts(self, sec_uid, count=20):
        """作者作品列表第一页（异步任务）"""
        d = self._task("/api/v1/douyin/user/posts",
                       {"sec_user_id": sec_uid, "count": count})
        return {"items": d.get("items") or [],
                "has_more": d.get("has_more"), "cursor": d.get("cursor")}

    def downloads_pages(self, max_pages):
        """下载记录分页（若接口支持 cursor 则翻页，否则单页）"""
        cursor = None
        for _ in range(max_pages):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            j = self._get("/api/v1/downloads", params=params).json()
            if not j.get("success"):
                raise RuntimeError(f"downloads failed: {j}")
            data = j.get("data") or {}
            if isinstance(data, list):
                yield data
                break
            yield data.get("items") or []
            if not data.get("has_more") or not data.get("cursor"):
                break
            cursor = data["cursor"]

    # ---------- 下载中心（触发 / 详情 / 文件流） ----------

    def download_create(self, content_id):
        """触发作品下载（skip_existing=True，已下载过会跳过）；自动处理限流"""
        for attempt in range(6):
            r = self.http.post("/api/v1/downloads", json={
                "platform": "douyin", "content_id": str(content_id), "skip_existing": True,
            })
            if r.status_code == 429:
                time.sleep(self._retry_after(r))
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("success"):
                return j.get("data")
            err = (j.get("error") or {})
            if err.get("code") == "RATE_LIMITED":
                time.sleep(self._retry_after_from_err(err))
                continue
            raise RuntimeError(f"download_create failed: {j}")
        raise TimeoutError("download_create rate-limited too long")

    def download_detail(self, download_id):
        """单个下载记录详情（含 files 明细）；自动处理限流"""
        for attempt in range(8):
            j = self.http.get(f"/api/v1/downloads/{download_id}").json()
            if j.get("success"):
                return j.get("data")
            err = (j.get("error") or {})
            if err.get("code") == "RATE_LIMITED":
                time.sleep(self._retry_after_from_err(err))
                continue
            raise RuntimeError(f"download_detail failed: {j}")
        raise TimeoutError("download_detail rate-limited too long")

    @staticmethod
    def _retry_after(resp):
        try:
            err = (resp.json().get("error") or {})
            return min(float(err.get("retry_after") or 10), 30) + 1
        except Exception:
            return 11

    @staticmethod
    def _retry_after_from_err(err):
        try:
            return min(float(err.get("retry_after") or 10), 30) + 1
        except Exception:
            return 11


def _log_step(cur, run_id, step, status, items, msg="", ms=0):
    cur.execute(
        "INSERT INTO dm_sync_log (run_id, step, status, items, message, duration_ms) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (run_id, step, status, int(items), str(msg)[:1000], int(ms)),
    )


def _own_authors(cur):
    cur.execute("SELECT sec_uid, nickname, auto_download FROM dm_authors "
                "WHERE enabled=1 ORDER BY sec_uid")
    return list(cur.fetchall())


def _refresh_one_profile(cur, c, sec):
    """刷新单个作者资料：调 DTK user 任务 -> 快照落库 + 作者主表更新"""
    p = c.user_profile(sec)
    st = p.get("stats") or {}
    now_min = datetime.now().replace(second=0, microsecond=0)
    # 与上一份快照对比，关注/粉丝/获赞/作品数任一变化 -> 点亮 change_flag（等用户看过趋势后才熄灭）
    cur.execute(
        "SELECT follower_count, following_count, total_digg, content_count "
        "FROM dm_author_snapshots WHERE sec_uid=%s AND taken_at<%s ORDER BY taken_at DESC LIMIT 1",
        (sec, now_min),
    )
    prev = cur.fetchone()
    changed = 0
    if prev:
        for k in ("follower_count", "following_count", "total_digg", "content_count"):
            if (prev[k] or 0) != (st.get(k) or 0):
                changed = 1
                break
    cur.execute(
        "INSERT INTO dm_author_snapshots "
        "(sec_uid, follower_count, following_count, total_digg, content_count, taken_at) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE follower_count=VALUES(follower_count), "
        "following_count=VALUES(following_count), total_digg=VALUES(total_digg), "
        "content_count=VALUES(content_count)",
        (sec, st.get("follower_count"), st.get("following_count"),
         st.get("total_digg"), st.get("content_count"), now_min),
    )
    avatar = (p.get("avatar") or {}).get("url")
    raw = p.get("raw") or {}
    is_private = 1 if raw.get("secret") else 0
    cur.execute(
        "UPDATE dm_authors SET nickname=%s, unique_id=%s, signature=%s, avatar_url=%s, "
        "web_url=%s, verified=%s, is_private=%s, last_follower_count=%s, last_following_count=%s, "
        "last_total_digg=%s, last_content_count=%s, "
        "change_flag=GREATEST(COALESCE(change_flag,0), %s), last_refreshed_at=NOW() WHERE sec_uid=%s",
        (p.get("nickname"), p.get("unique_id"), p.get("signature"), avatar,
         p.get("web_url"), 1 if p.get("verified") else 0, is_private,
         st.get("follower_count"), st.get("following_count"),
         st.get("total_digg"), st.get("content_count"), changed, sec),
    )
    return p


_CONTENT_UPSERT = (
    "INSERT INTO dm_contents "
    "(platform, content_id, sec_uid, nickname, kind, title, description, web_url, "
    "cover_url, music_title, tags, availability, published_at, first_seen_at, last_seen_at, "
    "digg_count, comment_count, share_count, collect_count, play_count, is_deleted, is_private) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), kind=VALUES(kind), "
    "title=VALUES(title), description=VALUES(description), web_url=VALUES(web_url), "
    "cover_url=VALUES(cover_url), music_title=VALUES(music_title), tags=VALUES(tags), "
    "availability=VALUES(availability), published_at=VALUES(published_at), "
    "last_seen_at=VALUES(last_seen_at), digg_count=VALUES(digg_count), "
    "comment_count=VALUES(comment_count), share_count=VALUES(share_count), "
    "collect_count=VALUES(collect_count), play_count=VALUES(play_count), "
    "is_deleted=VALUES(is_deleted), is_private=VALUES(is_private)"
)


def _upsert_posts(cur, sec_uid, nickname, items):
    """把 user/posts 返回的作品落库，返回条数"""
    now = datetime.now()
    n = 0
    for it in items:
        media = it.get("media") or {}
        covers = media.get("covers") or []
        cover = (covers[0] or {}).get("url") if covers else None
        mu = it.get("music") or {}
        st = it.get("stats") or {}
        tags = it.get("tags") or []
        au = it.get("author") or {}
        if it.get("is_deleted"):
            avail = "deleted"
        elif it.get("is_private"):
            avail = "private"
        else:
            avail = "live"
        cur.execute(_CONTENT_UPSERT, (
            it.get("platform") or "douyin", str(it.get("content_id")),
            au.get("uid") or sec_uid, au.get("nickname") or nickname,
            it.get("kind"), (it.get("title") or None), (it.get("description") or None),
            it.get("web_url"), cover, mu.get("title"),
            json.dumps(tags, ensure_ascii=False) if tags else None,
            avail, _dt(it.get("created_at")), None, now,
            st.get("digg_count"), st.get("comment_count"), st.get("share_count"),
            st.get("collect_count"), st.get("play_count"),
            1 if it.get("is_deleted") else 0, 1 if it.get("is_private") else 0,
        ))
        n += 1
    return n


def _sync_profiles(cur, c, run_id):
    """按本项目自己的名单刷新全部启用作者的资料"""
    t0 = time.time()
    ok = err = 0
    try:
        secs = [r["sec_uid"] for r in _own_authors(cur)]
    except Exception as e:
        _log_step(cur, run_id, "profiles", "error", 0, repr(e), (time.time() - t0) * 1000)
        return
    for sec in secs:
        try:
            _refresh_one_profile(cur, c, sec)
            ok += 1
            time.sleep(0.5)
        except Exception as e:
            err += 1
            log.warning("profile %s failed: %r", sec, e)
    status = "ok" if err == 0 else ("ok" if ok > 0 else "error")
    _log_step(cur, run_id, "profiles", status, ok,
              f"errors={err}" if err else "", (time.time() - t0) * 1000)


def _maybe_autodownload(cur, c, sec_uid, items):
    """新作品自动下载：先查是否已有下载记录（已下载/进行中都跳过），没有才触发"""
    n = 0
    for it in items:
        if it.get("is_private"):
            continue
        cid = str(it.get("content_id") or "")
        if not cid:
            continue
        cur.execute("SELECT id FROM dm_downloads WHERE content_id=%s LIMIT 1", (cid,))
        if cur.fetchone():
            continue  # 已经下载过或正在下载
        try:
            data = c.download_create(cid)
            dl_id = (data or {}).get("id") if isinstance(data, dict) else None
            if dl_id:
                cur.execute(
                    "INSERT IGNORE INTO dm_downloads (id, platform, content_id, sec_uid, state) "
                    "VALUES (%s,'douyin',%s,%s,'queued')", (dl_id, cid, sec_uid))
            n += 1
            time.sleep(0.5)
        except Exception as e:
            log.warning("autodl %s failed: %r", cid, e)
    return n


def _sync_posts(cur, c, run_id):
    """按本项目自己的名单抓每个启用作者的最新作品；开了自动下载的顺带查重触发"""
    t0 = time.time()
    n = err = auto = 0
    authors = _own_authors(cur)
    for a in authors:
        try:
            data = c.user_posts(a["sec_uid"], count=20)
            n += _upsert_posts(cur, a["sec_uid"], a.get("nickname"), data["items"])
            if a.get("auto_download"):
                auto += _maybe_autodownload(cur, c, a["sec_uid"], data["items"])
            time.sleep(0.5)
        except Exception as e:
            err += 1
            log.warning("posts %s failed: %r", a["sec_uid"], e)
    status = "ok" if err == 0 else ("ok" if n > 0 else "error")
    msg = f"errors={err}" if err else ""
    if auto:
        msg = (msg + " " if msg else "") + f"自动下载={auto}"
    _log_step(cur, run_id, "posts", status, n, msg, (time.time() - t0) * 1000)


def _author_dirname(cur, sec_uid):
    """作者在本项目 media 目录里的文件夹名（昵称净化，退回 sec_uid）"""
    cur.execute("SELECT nickname FROM dm_authors WHERE sec_uid=%s", (sec_uid,))
    r = cur.fetchone()
    nick = (r or {}).get("nickname") or sec_uid or "unknown"
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(nick)).strip(".")[:50]
    return name or "unknown"


def pull_download_local(cur, c, download_id):
    """把一个已完成的 DTK 下载拉回本项目 media 目录（按作者/作品归档）"""
    detail = c.download_detail(download_id)
    if not detail or detail.get("state") != "done":
        return False
    sec = detail.get("author_uid") or "unknown"
    content_id = str(detail.get("content_id") or download_id)
    dest = os.path.join(config.MEDIA_DIR, _author_dirname(cur, sec), content_id)
    os.makedirs(dest, exist_ok=True)
    saved = 0
    for f in detail.get("files") or []:
        if f.get("state") != "done" or not f.get("name"):
            continue
        path = os.path.join(dest, f["name"])
        if os.path.exists(path) and os.path.getsize(path) == (f.get("bytes") or -1):
            saved += 1
            continue
        for attempt in range(6):
            resp = c.http.get(f"/api/v1/downloads/{download_id}/files/{f['name']}")
            if resp.status_code == 429:
                time.sleep(DtkClient._retry_after(resp))
                continue
            resp.raise_for_status()
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(resp.content)
            os.replace(tmp, path)
            saved += 1
            break
        time.sleep(0.2)  # 轻微限速，避免触发 DTK 120/min 限流
    cur.execute("UPDATE dm_downloads SET local_dir=%s, pulled_at=NOW() WHERE id=%s",
                (dest, download_id))
    log.info("pulled %s -> %s (%d files)", download_id, dest, saved)
    return True


def wait_and_pull(download_id, timeout=600):
    """后台等待 DTK 下载完成，然后拉回本地（供手动触发下载用）"""
    wait_and_pull_many([download_id], timeout=timeout)


def wait_and_pull_many(download_ids, timeout=1800):
    """批量后台等待 DTK 下载完成，然后逐个拉回本地（顺长度安全，自动避开限流）"""
    c = DtkClient()
    pending = list(download_ids)
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for dl_id in list(pending):
            try:
                d = c.download_detail(dl_id)
                state = (d or {}).get("state")
                if state == "done":
                    conn = db.conn()
                    cur = conn.cursor()
                    try:
                        pull_download_local(cur, c, dl_id)
                    finally:
                        cur.close()
                        conn.close()
                    pending.remove(dl_id)
                elif state in ("failed", "error", "cancelled"):
                    log.warning("download %s ended as %s", dl_id, state)
                    pending.remove(dl_id)
            except Exception as e:
                log.warning("wait_and_pull %s: %r", dl_id, e)
            time.sleep(0.3)
        if pending:
            time.sleep(5)


def _sync_downloads(cur, c, run_id):
    t0 = time.time()
    n = 0
    try:
        for items in c.downloads_pages(config.DOWNLOAD_PAGES):
            for it in items:
                cur.execute(
                    "INSERT INTO dm_downloads "
                    "(id, platform, content_id, sec_uid, state, bytes_total, file_count, "
                    "directory, error, started_at, finished_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE state=VALUES(state), bytes_total=VALUES(bytes_total), "
                    "file_count=VALUES(file_count), error=VALUES(error), "
                    "started_at=VALUES(started_at), finished_at=VALUES(finished_at), "
                    "synced_at=NOW()",
                    (it.get("id"), it.get("platform") or "douyin",
                     str(it.get("content_id")) if it.get("content_id") else None,
                     it.get("author_uid"), it.get("state"), it.get("bytes_total"),
                     it.get("file_count"), it.get("directory"),
                     (str(it.get("error"))[:1000] if it.get("error") else None),
                     _dt(it.get("started_at")), _dt(it.get("finished_at"))),
                )
                n += 1
        # 已完成但还没落到本项目 media 目录的，拉回来（按作者归类）；手动删除过的不回拉
        cur.execute(
            "SELECT id FROM dm_downloads WHERE state='done' AND pulled_at IS NULL "
            "AND local_deleted=0"
        )
        pend = [r["id"] for r in cur.fetchall()]
        pulled = 0
        for dl_id in pend:
            try:
                if pull_download_local(cur, c, dl_id):
                    pulled += 1
            except Exception as e:
                log.warning("pull %s failed: %r", dl_id, e)
        msg = f"拉回本地 {pulled}/{len(pend)}"
        _log_step(cur, run_id, "downloads", "ok", n, msg, (time.time() - t0) * 1000)
    except Exception as e:
        _log_step(cur, run_id, "downloads", "error", n, repr(e), (time.time() - t0) * 1000)


def refresh_author(sec_uid):
    """单独刷新一个作者：资料 + 最新作品（同步等待），返回最新作者行"""
    c = DtkClient()
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT nickname, auto_download FROM dm_authors WHERE sec_uid=%s", (sec_uid,))
        row = cur.fetchone()
        nick = row["nickname"] if row else None
        auto = bool(row and row["auto_download"])
        _refresh_one_profile(cur, c, sec_uid)
        data = c.user_posts(sec_uid, count=20)
        _upsert_posts(cur, sec_uid, nick, data["items"])
        if auto:
            _maybe_autodownload(cur, c, sec_uid, data["items"])
        cur.execute("SELECT * FROM dm_authors WHERE sec_uid=%s", (sec_uid,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def refresh_due_authors():
    """到点刷新：按每个作者自己的 refresh_interval_min 轮询（调度器每 60 秒调一次）"""
    due = []
    c = DtkClient()
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT sec_uid, nickname, refresh_interval_min FROM dm_authors "
            "WHERE enabled=1 AND (last_refreshed_at IS NULL OR "
            "last_refreshed_at + INTERVAL refresh_interval_min MINUTE <= NOW())"
        )
        due = list(cur.fetchall())
        for a in due:
            try:
                _refresh_one_profile(cur, c, a["sec_uid"])
                log.info("auto-refreshed %s (%s, every %s min)",
                         a["sec_uid"][:16], a["nickname"], a["refresh_interval_min"])
            except Exception as e:
                log.warning("auto-refresh %s failed: %r", a["sec_uid"], e)
            time.sleep(1)
    finally:
        cur.close()
        conn.close()
    return len(due)


def run_sync():
    """跑一轮完整同步：本项目名单作品（含自动下载查重） -> 下载记录与文件回拉"""
    run_id = uuid.uuid4().hex[:12]
    conn = db.conn()
    cur = conn.cursor()
    try:
        c = DtkClient()
        try:
            _sync_posts(cur, c, run_id)
        except Exception as e:
            log.error("posts step failed: %r", e)
        try:
            _sync_downloads(cur, c, run_id)
        except Exception as e:
            log.error("downloads step failed: %r", e)
    finally:
        cur.close()
        conn.close()
    log.info("sync run %s finished", run_id)
    return run_id
