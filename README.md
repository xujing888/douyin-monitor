# 抖音采集系统（douyin-user-monitor）

基于 [DTK（Douyin_TikTok_Download_API）](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 的抖音作者监控与作品归档系统：定时同步监控作者的粉丝/关注/获赞/作品指标与作品清单到 MySQL，新作品自动下载到本地媒体库，Web 面板统一管理作者、文件、参数与趋势。

## 技术栈
- 后端：Python 3.11 + FastAPI + uvicorn（systemd 服务 `douyin-monitor.service`）+ PyMySQL
- 前端：原生 HTML/JS 单页 + ECharts（暗色主题、手机适配）
- 依赖：DTK 栈（dtk-api :7777 / worker / downloader :9100 / browser-rpc :9000 / redis / postgres，位于 /home/docker/dtk，`./dtkctl` 管理）

## 数据来源与致谢

本项目的抖音数据采集能力**全部来自开源项目 DTK**。本项目不自行实现抖音接口与签名算法（X-Bogus / a_bogus 等），只通过 DTK 暴露的 REST API 读取已解析好的数据。

- **项目**：Douyin_TikTok_Download_API（简称 DTK）
- **作者**：[@Evil0ctal](https://github.com/Evil0ctal)
- **仓库地址**：<https://github.com/Evil0ctal/Douyin_TikTok_Download_API>
- **开源许可**：**Apache License 2.0**。按该许可要求，任何分发 DTK 的场景都需保留其 `LICENSE` 与 `NOTICE` 文件（含版权声明），并说明对 DTK 的修改。
- **接口文档**：DTK 实例自身提供 `/docs`（Swagger UI）、`/swagger`、`/redoc`。

本项目实际调用的 DTK 接口：

| DTK 接口 | 用途 |
| --- | --- |
| `GET /api/v1/douyin/user` | 作者资料（昵称 / sec_uid / 粉丝·关注·获赞 / IP 属地等），异步返回 `202` + `task_id` |
| `GET /api/v1/douyin/user/posts` | 作者作品列表，异步同上 |
| `POST /api/v1/downloads` | 提交作品下载任务 |
| `GET /api/v1/downloads` | 账号级下载列表（回拉本地媒体库） |
| `GET /api/v1/downloads/{id}` | 单条下载详情 |

> 本项目**未修改、未二次分发** DTK 源码，仅以黑盒方式调用其 HTTP 接口；部署时使用官方 Docker 镜像自建实例。DTK 是本项目唯一的外部数据来源，在此向作者及社区致谢。
>
> 采集所得数据的使用请自行遵守抖音平台条款与适用法律。DTK 上游项目对使用者有明确提醒：数据来自有自身条款的平台，请遵守平台条款、尊重内容作者，不要用于骚扰或再分发他人作品。

## 目录结构
```
app/main.py        # FastAPI 入口 + 全部 API
app/sync.py        # 同步调度：资料刷新、作品同步、下载回拉
app/config.py      # config.json 读取（参数设置页可热改回写）
app/db.py          # MySQL 连接（中心库 douyin_monitor）
static/index.html  # Web 面板单页
static/echarts.min.js
config.json        # 运行配置（含 DTK 连接与密钥，勿外传）
media/             # 媒体库（运行时生成，按 {作者}/{作品ID}/ 归档，不入库 git）
```

## 功能模块
- **作者监控**：卡片布局（头像 + 关注/粉丝/获赞/作品四格指标 + 签名 + 状态标签）。指标相对**上一份快照**变化时，指标名后显示增减箭头（增加=红▲、减少=绿▼，与「有变化」徽章同口径）；「有变化」呼吸徽章点击滑出 30 天趋势抽屉（四条曲线）。支持启停、独立设置（资料刷新间隔/自动下载）、单作者刷新、级联删除（清作品/下载记录/趋势快照/本地媒体，不可恢复）。
- **作品动态**：JOIN dm_authors 仅展示当前监控名单作者，按作者筛选。
- **文件管理**：卡片布局 + 作者列 + 跨作品连续预览（左右箭头/键盘/滚轮循环）；删除置 `local_deleted=1` 防同步回拉；支持批量删除。
- **同步日志 / 参数设置**：同步间隔、新作者默认参数、DTK API 连接均可热改（立即生效并回写 config.json + 持久化 dm_settings，重启不丢）。

## 数据库
MySQL 库 `douyin_monitor`：`dm_authors`（作者主表，含 last_* 指标与 change_flag）、`dm_contents`、`dm_downloads`（含 local_deleted）、`dm_settings`（键值配置）、`dm_author_snapshots`（指标快照，趋势与变化对比依据）。

## API 概览
| 接口 | 方法 | 说明 |
| --- | --- | --- |
| /api/authors | GET | 作者列表（含 follower_delta_24h 与 delta_following/follower/digg/content 四指标增减值） |
| /api/authors | POST | 添加作者（主页链接或 sec_uid） |
| /api/authors/{sec}/enabled | PATCH | 启停 |
| /api/authors/{sec} | DELETE | 级联删除 |
| /api/authors/{sec}/refresh | POST | 立即刷新资料 |
| /api/authors/{sec}/ack-change | POST | 确认变化（熄灭「有变化」徽章） |
| /api/authors/{sec}/trend | GET | 近 N 天指标趋势 |
| /api/contents | GET | 作品动态 |
| /api/files/browse | GET | 文件管理列表 |
| /api/files/delete-batch | POST | 批量删除文件 |
| /api/settings | GET / PATCH | 参数读写（热生效） |
| /api/status | GET | 运行状态与计数 |

## 桌面版·本地库模式
桌面 exe（`_exe_build/dist_test/`）默认使用**本地 SQLite**，拷到任何 Windows 机器双击即用、数据全部本地存，不依赖中心 MySQL：
- `config.json` 的 `db` 段：`{"type": "sqlite", "file": "douyin.db"}` —— 库文件相对路径基于 config.json 同目录（exe 同目录）；首次启动自动建全套表与索引。
- `db.type` 改回 `"mysql"`（含 host/port/user/password/database）即切回服务器版，与现网部署完全兼容（缺省 type 时有 host 字段即按 mysql）。
- 参数设置页新增：**下载文件夹**自定义（media_dir 持久化到 dm_settings + config.json，新下载即时落新目录）、**数据库切换**（本地 SQLite / 远程 MySQL，支持测试连接，写 config.json 重启后生效）。
- MySQL 方言（`ON DUPLICATE KEY UPDATE` / `NOW()` / `INSERT IGNORE` / `INTERVAL` / `GREATEST` 等）由 `app/db.py` 适配层自动转译，业务 SQL 双后端通用。
- 打包：`_exe_build/build_exe.sh`（PyInstaller onefile + windowed，内置 fallback config 为 sqlite 模板）。

## 部署与运维
```bash
sudo systemctl status/restart douyin-monitor   # Web 服务
journalctl -u douyin-monitor -f                # 日志
cd /home/docker/dtk && ./dtkctl status         # DTK 栈（restart 策略保持 unless-stopped）
```
- 改代码惯例：改动前同目录时间戳备份（`main_YYYYMMDD-HHmm.py` / `index_YYYYMMDD-HHmm.html`，已被 .gitignore 忽略），验证通过后提交本仓库。
- 静态页改动无需重启服务；改 app/ 下 Python 需 `systemctl restart douyin-monitor`。

## 近期变更
- **2026-10-06**：README 增加「数据来源与致谢」章节，明确采集能力来自开源项目 DTK（Douyin_TikTok_Download_API，Apache-2.0），列出实际调用的接口与许可义务。
- **2026-10-01**：作者监控四指标增减箭头（增加红▲/减少绿▼，与「有变化」徽章同口径）；箭头定位于指标名后；粉丝 24h delta 为 0 时不再显示灰色 0 占位。（245570d → 09f44bf → 576e991）
- **2026-09-27**：文件管理作者列 + 跨作品预览、参数设置页、删除防回拉、级联删除、手机适配等（详见内网 Wiki「更新记录」）。
