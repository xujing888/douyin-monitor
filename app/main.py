import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from . import sync as syncmod

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="douyin-monitor", docs_url=None, redoc_url=None)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _job():
    try:
        syncmod.run_sync()
    except Exception:
        logging.getLogger("douyin-monitor.sync").exception("sync job crashed")


def _ticker():
    """每 60 秒检查一次：到点的作者按各自设置的间隔自动刷新资料"""
    try:
        syncmod.refresh_due_authors()
    except Exception:
        logging.getLogger("douyin-monitor.sync").exception("ticker crashed")


scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
scheduler.add_job(
    _job, "interval",
    minutes=config.SYNC_INTERVAL_MIN,
    id="sync", max_instances=1, coalesce=True,
    next_run_time=datetime.now(),  # 启动即跑第一轮
)
scheduler.add_job(
    _ticker, "interval",
    seconds=60,
    id="refresh_ticker", max_instances=1, coalesce=True,
)
scheduler.start()


# ---------- 全局参数（dm_settings 键值表，缺省回落 config.json） ----------
DEFAULT_SETTINGS = {
    "sync_interval_min": config.SYNC_INTERVAL_MIN,   # 全局自动同步间隔
    "default_refresh_interval_min": 360,             # 新作者默认资料刷新间隔
    "default_auto_download": 0,                      # 新作者默认自动下载
}


def _load_settings(cur):
    cur.execute("SELECT skey, svalue FROM dm_settings")
    vals = {r["skey"]: r["svalue"] for r in cur.fetchall()}
    out = dict(DEFAULT_SETTINGS)
    for k in list(out):
        if k not in vals or str(vals[k]).strip() == "":
            continue
        v = vals[k]
        if k == "default_auto_download":
            out[k] = 1 if str(v).lower() in ("1", "true", "yes") else 0
        else:
            try:
                out[k] = max(10, min(int(v), 10080))
            except (TypeError, ValueError):
                pass
    return out


def _save_setting(cur, key, value):
    cur.execute(
        "INSERT INTO dm_settings (skey, svalue) VALUES (%s,%s) "
        "ON DUPLICATE KEY UPDATE svalue=VALUES(svalue)",
        (key, str(value)),
    )


@app.get("/api/authors")
def api_authors():
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM dm_authors "
            "ORDER BY enabled DESC, last_follower_count DESC, nickname"
        )
        rows = cur.fetchall()
        base_dt = datetime.now() - timedelta(hours=24)
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["verified"] = bool(r["verified"])
            cur.execute(
                "SELECT follower_count FROM dm_author_snapshots "
                "WHERE sec_uid=%s AND taken_at<=%s AND follower_count IS NOT NULL "
                "ORDER BY taken_at DESC LIMIT 1",
                (r["sec_uid"], base_dt),
            )
            old = cur.fetchone()
            if old and r["last_follower_count"] is not None:
                r["follower_delta_24h"] = r["last_follower_count"] - old["follower_count"]
            else:
                r["follower_delta_24h"] = None
        return {"code": 0, "data": rows}
    finally:
        cur.close()
        conn.close()


@app.post("/api/authors/{sec_uid}/ack-change")
def api_ack_change(sec_uid: str):
    """用户看过趋势抽屉后熄灭变化标识"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE dm_authors SET change_flag=0 WHERE sec_uid=%s", (sec_uid,))
        return {"code": 0}
    finally:
        cur.close()
        conn.close()


@app.get("/api/authors/{sec_uid}/trend")
def api_trend(sec_uid: str, days: int = Query(30, ge=1, le=365)):
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT taken_at, follower_count, following_count, total_digg, content_count "
            "FROM dm_author_snapshots WHERE sec_uid=%s AND taken_at>=%s "
            "ORDER BY taken_at",
            (sec_uid, datetime.now() - timedelta(days=days)),
        )
        rows = cur.fetchall()
        return {"code": 0, "data": rows}
    finally:
        cur.close()
        conn.close()


@app.get("/api/contents")
def api_contents(
    sec_uid: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conn = db.conn()
    cur = conn.cursor()
    try:
        # 只显示当前监控名单作者的作品（作者删除后遗留记录不再展示）
        join = "FROM dm_contents c JOIN dm_authors a ON a.sec_uid=c.sec_uid"
        where, params = "1=1", []
        if sec_uid:
            where = "c.sec_uid=%s"
            params.append(sec_uid)
        cur.execute(
            f"SELECT COUNT(*) AS total {join} WHERE {where}", params
        )
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT c.* {join} WHERE {where} "
            f"ORDER BY COALESCE(c.published_at, c.first_seen_at) DESC, c.content_id DESC "
            f"LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()
        return {"code": 0, "total": total, "data": rows}
    finally:
        cur.close()
        conn.close()


@app.get("/api/downloads")
def api_downloads(
    state: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = db.conn()
    cur = conn.cursor()
    try:
        where, params = "1=1", []
        if state:
            where = "state=%s"
            params.append(state)
        cur.execute(
            f"SELECT COUNT(*) AS total FROM dm_downloads WHERE {where}", params
        )
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT * FROM dm_downloads WHERE {where} "
            f"ORDER BY COALESCE(finished_at, started_at, synced_at) DESC "
            f"LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()
        return {"code": 0, "total": total, "data": rows}
    finally:
        cur.close()
        conn.close()


@app.get("/api/logs")
def api_logs(limit: int = Query(100, ge=1, le=500)):
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT * FROM dm_sync_log ORDER BY id DESC LIMIT %s", (limit,)
        )
        return {"code": 0, "data": cur.fetchall()}
    finally:
        cur.close()
        conn.close()


@app.get("/api/status")
def api_status():
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT run_id, MAX(created_at) AS last_at FROM dm_sync_log GROUP BY run_id "
            "ORDER BY last_at DESC LIMIT 1"
        )
        last_run = cur.fetchone()
        steps = []
        if last_run:
            cur.execute(
                "SELECT step, status, items, message, duration_ms, created_at "
                "FROM dm_sync_log WHERE run_id=%s ORDER BY id",
                (last_run["run_id"],),
            )
            steps = cur.fetchall()
        counts = {}
        for table, key in [("dm_authors", "authors"), ("dm_contents", "contents"),
                           ("dm_downloads", "downloads")]:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            counts[key] = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM dm_authors WHERE enabled=1")
        counts["authors_enabled"] = cur.fetchone()["n"]
        return {
            "code": 0,
            "data": {
                "last_run": last_run,
                "steps": steps,
                "counts": counts,
                "interval_min": config.SYNC_INTERVAL_MIN,
            },
        }
    finally:
        cur.close()
        conn.close()


@app.post("/api/sync/run")
def api_sync_run():
    t = threading.Thread(target=_job, daemon=True)
    t.start()
    return {"code": 0, "message": "sync started in background"}


class AddAuthorBody(BaseModel):
    sec_uid: str  # 支持 sec_uid 或作者主页链接（自动提取 MS4w...）


class ToggleBody(BaseModel):
    enabled: bool


class SettingsBody(BaseModel):
    refresh_interval_min: int | None = None
    auto_download: bool | None = None


class BatchDownloadBody(BaseModel):
    content_ids: list[str]


class BatchDeleteBody(BaseModel):
    ids: list[str]


class GlobalSettingsBody(BaseModel):
    sync_interval_min: int | None = None
    default_refresh_interval_min: int | None = None
    default_auto_download: bool | None = None


@app.get("/api/settings")
def api_settings():
    """全局参数（参数设置页）"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        return {"code": 0, "data": _load_settings(cur)}
    finally:
        cur.close()
        conn.close()


@app.patch("/api/settings")
def api_settings_patch(body: GlobalSettingsBody):
    """修改全局参数；同步间隔改完立即重排调度"""
    global DEFAULT_SETTINGS
    if body.sync_interval_min is None and body.default_refresh_interval_min is None \
            and body.default_auto_download is None:
        raise HTTPException(400, "没有要修改的参数")
    conn = db.conn()
    cur = conn.cursor()
    try:
        s = _load_settings(cur)
        if body.sync_interval_min is not None:
            n = max(10, min(int(body.sync_interval_min), 10080))
            _save_setting(cur, "sync_interval_min", n)
            if n != config.SYNC_INTERVAL_MIN:
                config.SYNC_INTERVAL_MIN = n
                try:
                    scheduler.reschedule_job("sync", trigger="interval", minutes=n)
                except Exception:
                    logging.getLogger("douyin-monitor").warning("reschedule sync job failed")
        if body.default_refresh_interval_min is not None:
            n = max(10, min(int(body.default_refresh_interval_min), 10080))
            _save_setting(cur, "default_refresh_interval_min", n)
        if body.default_auto_download is not None:
            _save_setting(cur, "default_auto_download", 1 if body.default_auto_download else 0)
        s = _load_settings(cur)
        DEFAULT_SETTINGS = s
    finally:
        cur.close()
        conn.close()
    return {"code": 0, "message": "参数已保存", "data": s}


def _extract_sec_uid(text: str) -> str:
    m = re.search(r"(MS4w[A-Za-z0-9_-]+)", text or "")
    if not m:
        raise HTTPException(400, "无法识别 sec_uid：请粘贴作者主页链接或 sec_uid（MS4w 开头）")
    return m.group(1)


@app.post("/api/authors")
def api_add_author(body: AddAuthorBody):
    """添加作者到本项目独立名单（不动 DTK 的 watchlist），后台自动拉资料和作品"""
    sec = _extract_sec_uid(body.sec_uid)
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT sec_uid FROM dm_authors WHERE sec_uid=%s", (sec,))
        if cur.fetchone():
            raise HTTPException(400, "该作者已在监控名单中")
        s = _load_settings(cur)
        cur.execute(
            "INSERT INTO dm_authors (sec_uid, enabled, refresh_interval_min, auto_download) "
            "VALUES (%s, 1, %s, %s)",
            (sec, s["default_refresh_interval_min"], s["default_auto_download"]),
        )
    finally:
        cur.close()
        conn.close()
    threading.Thread(target=_job, daemon=True).start()  # 后台拉资料+作品
    return {"code": 0, "message": "已加入监控名单，正在后台拉取资料和作品"}


@app.patch("/api/authors/{sec_uid}/enabled")
def api_toggle_author(sec_uid: str, body: ToggleBody):
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE dm_authors SET enabled=%s WHERE sec_uid=%s",
                    (1 if body.enabled else 0, sec_uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "作者不存在")
    finally:
        cur.close()
        conn.close()
    return {"code": 0, "message": "已" + ("启用" if body.enabled else "停用")}


@app.delete("/api/authors/{sec_uid}")
def api_del_author(sec_uid: str):
    """删除作者：同时删除名下全部作品记录、下载记录、趋势快照和本地文件（不可恢复）"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT sec_uid FROM dm_authors WHERE sec_uid=%s", (sec_uid,))
        if not cur.fetchone():
            raise HTTPException(404, "作者不存在")
        base = Path(config.MEDIA_DIR).resolve()
        removed = 0
        cur.execute("SELECT local_dir FROM dm_downloads "
                    "WHERE sec_uid=%s AND local_dir IS NOT NULL", (sec_uid,))
        for row in cur.fetchall():
            p = Path(row["local_dir"]).resolve()
            if p.is_relative_to(base):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        cur.execute("DELETE FROM dm_contents WHERE sec_uid=%s", (sec_uid,))
        contents = cur.rowcount
        cur.execute("DELETE FROM dm_downloads WHERE sec_uid=%s", (sec_uid,))
        downloads = cur.rowcount
        cur.execute("DELETE FROM dm_author_snapshots WHERE sec_uid=%s", (sec_uid,))
        cur.execute("DELETE FROM dm_authors WHERE sec_uid=%s", (sec_uid,))
        return {"code": 0,
                "message": f"已删除作者及其 {contents} 个作品记录、{downloads} 条下载记录、{removed} 组本地文件"}
    finally:
        cur.close()
        conn.close()


@app.post("/api/authors/{sec_uid}/refresh")
def api_refresh_author(sec_uid: str):
    try:
        row = syncmod.refresh_author(sec_uid)
    except Exception as e:
        raise HTTPException(502, f"刷新失败：{e}")
    if not row:
        raise HTTPException(404, "作者不存在")
    return {"code": 0, "message": "已刷新", "data": row}


@app.patch("/api/authors/{sec_uid}/settings")
def api_author_settings(sec_uid: str, body: SettingsBody):
    """作者独立设置：资料自动刷新间隔（分钟）、新作品自动下载开关"""
    sets, params = [], []
    if body.refresh_interval_min is not None:
        sets.append("refresh_interval_min=%s")
        params.append(max(10, min(int(body.refresh_interval_min), 10080)))
    if body.auto_download is not None:
        sets.append("auto_download=%s")
        params.append(1 if body.auto_download else 0)
    if not sets:
        raise HTTPException(400, "没有要修改的设置")
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE dm_authors SET {', '.join(sets)} WHERE sec_uid=%s",
                    params + [sec_uid])
        if cur.rowcount == 0:
            raise HTTPException(404, "作者不存在")
    finally:
        cur.close()
        conn.close()
    return {"code": 0, "message": "设置已保存"}


@app.post("/api/contents/download-batch")
def api_download_batch(body: BatchDownloadBody):
    """批量下载：后台逐个提交（自动查重/限速），完成后统一拉回本地"""
    ids = []
    for x in body.content_ids:
        s = str(x).strip()
        if s and s not in ids:
            ids.append(s)
    ids = ids[:100]
    if not ids:
        raise HTTPException(400, "未选择作品")
    threading.Thread(target=_batch_download_job, args=(ids,), daemon=True).start()
    return {"code": 0, "message": f"已提交 {len(ids)} 个下载任务，完成后自动保存到文件库"}


def _batch_download_job(ids):
    c = syncmod.DtkClient()
    dl_ids = []
    for cid in ids:
        try:
            data = c.download_create(cid)
            dl_id = (data or {}).get("id") if isinstance(data, dict) else None
            if dl_id:
                dl_ids.append(dl_id)
        except Exception as e:
            logging.getLogger("douyin-monitor.sync").warning("batch dl %s: %r", cid, e)
        time.sleep(0.5)
    if dl_ids:
        syncmod.wait_and_pull_many(dl_ids)


@app.post("/api/contents/{content_id}/download")
def api_download_content(content_id: str):
    c = syncmod.DtkClient()
    try:
        data = c.download_create(content_id)
    except Exception as e:
        raise HTTPException(502, f"下载任务提交失败：{e}")
    dl_id = None
    if isinstance(data, dict):
        dl_id = data.get("id") or data.get("download_id")
    if dl_id:
        threading.Thread(target=syncmod.wait_and_pull, args=(dl_id,), daemon=True).start()
        return {"code": 0, "message": "下载任务已提交，完成后自动保存到本项目文件库"}
    threading.Thread(target=_job, daemon=True).start()
    return {"code": 0, "message": "下载任务已提交"}


@app.get("/api/downloads/{download_id}")
def api_download_detail(download_id: str):
    c = syncmod.DtkClient()
    try:
        data = c.download_detail(download_id)
    except Exception as e:
        raise HTTPException(502, f"获取下载详情失败：{e}")
    files = (data or {}).get("files") or []
    return {"code": 0, "data": {"id": download_id, "files": files}}


@app.get("/api/downloads/{download_id}/files/{name}")
def api_download_file(download_id: str, name: str):
    """文件代理：从 DTK 存储取文件流给用户保存"""
    c = syncmod.DtkClient()
    req = c.http.build_request("GET", f"/api/v1/downloads/{download_id}/files/{name}")
    resp = c.http.send(req, stream=True)
    if resp.status_code != 200:
        resp.close()
        raise HTTPException(resp.status_code, "文件不存在或已被清理")
    media = resp.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(resp.iter_bytes(256 * 1024), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/files/authors")
def api_file_authors():
    """文件管理：按作者汇总已落本地的下载文件（昵称：监控名单优先，作品表兜底）"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT d.sec_uid, COUNT(*) AS works, SUM(d.bytes_total) AS bytes, "
            "SUM(d.file_count) AS files FROM dm_downloads d "
            "WHERE d.pulled_at IS NOT NULL AND d.local_dir IS NOT NULL "
            "GROUP BY d.sec_uid"
        )
        rows = list(cur.fetchall())
        for r in rows:
            cur.execute("SELECT nickname FROM dm_authors WHERE sec_uid=%s", (r["sec_uid"],))
            a = cur.fetchone()
            if not a or not a["nickname"]:
                cur.execute("SELECT nickname FROM dm_contents WHERE sec_uid=%s "
                            "AND nickname IS NOT NULL LIMIT 1", (r["sec_uid"],))
                a = cur.fetchone()
            r["nickname"] = (a or {}).get("nickname") or r["sec_uid"]
        rows.sort(key=lambda x: (x["nickname"]))
        return {"code": 0, "data": rows}
    finally:
        cur.close()
        conn.close()


@app.delete("/api/files/{download_id}")
def api_delete_files(download_id: str):
    """删除已下载到本地的作品文件（仅限 media 目录内），标记删除后同步不再拉回"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT local_dir FROM dm_downloads WHERE id=%s", (download_id,))
        row = cur.fetchone()
        if not row or not row["local_dir"]:
            raise HTTPException(404, "该作品没有本地文件")
        p = Path(row["local_dir"]).resolve()
        base = Path(config.MEDIA_DIR).resolve()
        if not p.is_relative_to(base):
            raise HTTPException(400, "非法路径")
        shutil.rmtree(p, ignore_errors=True)
        cur.execute("UPDATE dm_downloads SET local_dir=NULL, pulled_at=NULL, local_deleted=1 "
                    "WHERE id=%s", (download_id,))
        return {"code": 0, "message": "本地文件已删除，同步不会再拉回（DTK 原始记录保留）"}
    finally:
        cur.close()
        conn.close()


@app.post("/api/files/delete-batch")
def api_files_delete_batch(body: BatchDeleteBody):
    """批量删除选中作品的本地文件（标记 local_deleted，同步不再拉回）"""
    ids = []
    for x in body.ids:
        s = str(x).strip()
        if s and s not in ids:
            ids.append(s)
    ids = ids[:500]
    if not ids:
        raise HTTPException(400, "未选择作品")
    conn = db.conn()
    cur = conn.cursor()
    deleted = 0
    try:
        base = Path(config.MEDIA_DIR).resolve()
        for did in ids:
            cur.execute("SELECT local_dir FROM dm_downloads WHERE id=%s", (did,))
            row = cur.fetchone()
            if not row:
                continue
            if row["local_dir"]:
                p = Path(row["local_dir"]).resolve()
                if p.is_relative_to(base):
                    shutil.rmtree(p, ignore_errors=True)
            cur.execute("UPDATE dm_downloads SET local_dir=NULL, pulled_at=NULL, local_deleted=1 "
                        "WHERE id=%s", (did,))
            deleted += 1
        return {"code": 0, "message": f"已删除 {deleted} 个作品的本地文件，同步不会再拉回"}
    finally:
        cur.close()
        conn.close()


@app.get("/api/files/browse")
def api_file_browse(sec_uid: str | None = None):
    """文件管理：浏览作者目录下的作品文件夹与文件（/media/ 直链）"""
    conn = db.conn()
    cur = conn.cursor()
    try:
        where, params = "d.pulled_at IS NOT NULL AND d.local_dir IS NOT NULL", []
        if sec_uid:
            where += " AND d.sec_uid=%s"
            params.append(sec_uid)
        cur.execute(
            f"SELECT d.id, d.content_id, d.local_dir, d.finished_at, d.sec_uid, "
            f"COALESCE(a.nickname, c.nickname) AS nickname, "
            f"COALESCE(c.cover_url, '') AS cover_url "
            f"FROM dm_downloads d LEFT JOIN dm_authors a ON a.sec_uid=d.sec_uid "
            f"LEFT JOIN dm_contents c ON c.content_id=d.content_id "
            f"WHERE {where} ORDER BY finished_at DESC",
            params,
        )
        base = Path(config.MEDIA_DIR).resolve()
        works = []
        for r in cur.fetchall():
            d = Path(r["local_dir"])
            files = []
            if d.is_dir():
                for p in sorted(d.iterdir()):
                    if p.is_file():
                        try:
                            rel = p.resolve().relative_to(base).as_posix()
                        except ValueError:
                            continue
                        files.append({"name": p.name, "size": p.stat().st_size,
                                      "url": "/media/" + rel})
            if files:
                works.append({"id": r["id"], "content_id": r["content_id"],
                              "sec_uid": r["sec_uid"],
                              "nickname": r.get("nickname") or "（未知作者）",
                              "cover_url": r.get("cover_url") or "",
                              "finished_at": r["finished_at"],
                              "files": files})
        return {"code": 0, "data": works}
    finally:
        cur.close()
        conn.close()


MEDIA_PATH = Path(config.MEDIA_DIR)
MEDIA_PATH.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_PATH)), name="media")

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
