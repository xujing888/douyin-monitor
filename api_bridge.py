"""进程内 API 分发器：桌面版 GUI 不再起本地 HTTP 服务，
直接调用 app.main 的业务函数（签名与 FastAPI 路由一致）。

行为与 HTTP 版对齐：
- 成功：原样返回业务函数返回值（{"code":0,...} 等）
- HTTPException：{"detail": str(e.detail)}
- 其他异常：{"detail": repr(e)}
- 无匹配路由：{"detail": "no route: ..."}

用法：
    from api_bridge import call
    call("GET", "/api/authors")
    call("PATCH", "/api/settings", json={"dtk_base": "..."})
    call("GET", "/api/contents", params={"limit": 100, "offset": 0})
"""
import re

from fastapi import HTTPException

from app import main as webmain


def _h(fn, *args, **kw):
    """统一异常包装：与 FastAPI 异常响应形态一致"""
    try:
        return fn(*args, **kw)
    except HTTPException as e:
        return {"detail": str(e.detail)}
    except Exception as e:
        return {"detail": repr(e)}


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------- GET ----------------

def _g_authors(m, params, body):
    return _h(webmain.api_authors)


def _g_trend(m, params, body):
    p = params or {}
    return _h(webmain.api_trend, m["sec_uid"],
              days=_int(p.get("days"), 30), range=p.get("range") or "")


def _g_contents(m, params, body):
    p = params or {}
    sec = p.get("sec_uid") or None
    return _h(webmain.api_contents, sec_uid=sec,
              limit=_int(p.get("limit"), 50), offset=_int(p.get("offset"), 0))


def _g_downloads(m, params, body):
    p = params or {}
    return _h(webmain.api_downloads, state=p.get("state") or None,
              limit=_int(p.get("limit"), 100), offset=_int(p.get("offset"), 0))


def _g_download_detail(m, params, body):
    return _h(webmain.api_download_detail, m["download_id"])


def _g_logs(m, params, body):
    return _h(webmain.api_logs, limit=_int((params or {}).get("limit"), 100))


def _g_status(m, params, body):
    return _h(webmain.api_status)


def _g_settings(m, params, body):
    return _h(webmain.api_settings)


def _g_db_config(m, params, body):
    return _h(webmain.api_db_config_get)


def _g_file_authors(m, params, body):
    return _h(webmain.api_file_authors)


def _g_file_browse(m, params, body):
    return _h(webmain.api_file_browse, sec_uid=(params or {}).get("sec_uid") or None)


# ---------------- POST ----------------

def _p_authors(m, params, body):
    return _h(webmain.api_add_author, webmain.AddAuthorBody(**(body or {})))


def _p_ack_change(m, params, body):
    return _h(webmain.api_ack_change, m["sec_uid"])


def _p_sync_run(m, params, body):
    return _h(webmain.api_sync_run)


def _p_settings(m, params, body):
    return _h(webmain.api_settings_patch, webmain.GlobalSettingsBody(**(body or {})))


def _p_db_config(m, params, body):
    return _h(webmain.api_db_config_set, webmain.DbConfigBody(**(body or {})))


def _p_db_test(m, params, body):
    return _h(webmain.api_db_test, webmain.DbConfigBody(**(body or {})))


def _p_refresh_author(m, params, body):
    return _h(webmain.api_refresh_author, m["sec_uid"])


def _p_download_batch(m, params, body):
    return _h(webmain.api_download_batch, webmain.BatchDownloadBody(**(body or {})))


def _p_download_one(m, params, body):
    return _h(webmain.api_download_content, m["content_id"])


def _p_files_delete_batch(m, params, body):
    return _h(webmain.api_files_delete_batch, webmain.BatchDeleteBody(**(body or {})))


# ---------------- PATCH / DELETE ----------------

def _pa_toggle(m, params, body):
    return _h(webmain.api_toggle_author, m["sec_uid"], webmain.ToggleBody(**(body or {})))


def _pa_author_settings(m, params, body):
    return _h(webmain.api_author_settings, m["sec_uid"], webmain.SettingsBody(**(body or {})))


def _d_author(m, params, body):
    return _h(webmain.api_del_author, m["sec_uid"])


def _d_files(m, params, body):
    return _h(webmain.api_delete_files, m["download_id"])


def _d_logs(m, params, body):
    return _h(webmain.api_logs_clear)


_SEC = r"(?P<sec_uid>[^/]+)"
_CID = r"(?P<content_id>[^/]+)"
_DID = r"(?P<download_id>[^/]+)"

_ROUTES = [
    ("GET",    rf"^/api/authors$",                                  _g_authors),
    ("GET",    rf"^/api/authors/{_SEC}/trend$",                     _g_trend),
    ("POST",   rf"^/api/authors/{_SEC}/ack-change$",                _p_ack_change),
    ("POST",   rf"^/api/authors/{_SEC}/refresh$",                   _p_refresh_author),
    ("PATCH",  rf"^/api/authors/{_SEC}/enabled$",                   _pa_toggle),
    ("PATCH",  rf"^/api/authors/{_SEC}/settings$",                  _pa_author_settings),
    ("DELETE", rf"^/api/authors/{_SEC}$",                           _d_author),
    ("POST",   rf"^/api/authors$",                                  _p_authors),
    ("GET",    rf"^/api/contents$",                                 _g_contents),
    ("POST",   rf"^/api/contents/download-batch$",                  _p_download_batch),
    ("POST",   rf"^/api/contents/{_CID}/download$",                 _p_download_one),
    ("GET",    rf"^/api/downloads$",                                _g_downloads),
    ("GET",    rf"^/api/downloads/{_DID}$",                         _g_download_detail),
    ("GET",    rf"^/api/logs$",                                     _g_logs),
    ("DELETE", rf"^/api/logs$",                                     _d_logs),
    ("GET",    rf"^/api/status$",                                   _g_status),
    ("POST",   rf"^/api/sync/run$",                                 _p_sync_run),
    ("GET",    rf"^/api/settings$",                                 _g_settings),
    ("PATCH",  rf"^/api/settings$",                                 _p_settings),
    ("GET",    rf"^/api/db-config$",                                _g_db_config),
    ("POST",   rf"^/api/db-config$",                                _p_db_config),
    ("POST",   rf"^/api/db-test$",                                  _p_db_test),
    ("GET",    rf"^/api/files/authors$",                            _g_file_authors),
    ("GET",    rf"^/api/files/browse$",                             _g_file_browse),
    ("DELETE", rf"^/api/files/{_DID}$",                             _d_files),
    ("POST",   rf"^/api/files/delete-batch$",                       _p_files_delete_batch),
]

_COMPILED = [(m, re.compile(p), fn) for m, p, fn in _ROUTES]


def call(method, path, params=None, json=None):
    """等价于 HTTP 版请求：call("GET", "/api/authors?limit=5") 也支持查询串内联"""
    from urllib.parse import parse_qsl, urlsplit
    parts = urlsplit(path)
    if parts.query:
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        params = {**(params or {}), **q}
        path = parts.path
    for mth, rx, fn in _COMPILED:
        if mth != method:
            continue
        m = rx.match(path)
        if not m:
            continue
        return fn(m.groupdict(), params, json)
    return {"detail": f"no route: {method} {path}"}
