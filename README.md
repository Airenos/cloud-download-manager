# 临时下载站

一个轻量的临时下载站：管理员粘贴 HTTP/HTTPS 下载链接，后端通过 aria2 下载到 `downloads/`，朋友打开页面下载文件。项目只使用 Python 3 标准库和 `aria2c`，不需要数据库，不使用 React/Vue/Vite/Next.js。

## 功能

- 首页 SSR 渲染：顶部显示三张状态概览卡，桌面端文件列表与下载任务并排，移动端按文件、任务顺序纵向排列。
- 文件操作：图片、视频和文本可预览，所有文件可普通下载；更多菜单提供二维码分享、一次性下载、续期和带管理密码确认的删除。高风险操作使用统一的站内确认弹窗。
- 图片、视频、文本文件在线预览（txt/md/json/log/conf/yaml/xml/csv/py/sh 等）。
- 自定义保留时间：1小时 / 12小时 / 24小时（默认）/ 3天 / 7天。
- 一次性下载需确认，完整传输成功后删除文件；中断不会删除。
- 文件类型筛选：全部 / 图片 / 视频 / 文本 / 文档 / 压缩包 / 其他。
- 文件列表支持按名称、创建时间和过期时间排序，并与搜索、类型筛选和分页联动。
- 深色模式：跟随系统设置，也可通过页头按钮手动切换。
- 二维码分享：纯前端 JS 生成，不依赖外部库或 CDN。
- aria2 RPC 只访问 `127.0.0.1:6800`，secret 不暴露在 HTML。
- 添加链接支持从剪贴板粘贴和批量提交（每行一个、每次最多 20 个），结果直接在首页提示。
- 添加链接后任务面板立即刷新，下载进行中每 3 秒更新；完成任务显示明确的“已完成”状态。
- 管理员分片上传本地文件：服务端上传会话、失败重试、取消、进度显示和过期清理。
- 文件完整下载成功后累计下载次数；HEAD、预览/Range 和中断传输不计数。
- 删除文件后首页会立即更新文件数量、目录占用、剩余空间和磁盘状态条。
- 图片和视频预览页显示预览次数；每次打开预览页计一次，视频 Range 请求不重复计数。
- 自定义文件名支持中文、英文、数字、空格、点、下划线、短横线，以及半角/全角括号。
- 文件任务完成和本地上传后自动刷新文件面板；搜索、类型、排序和分页状态在当前标签页保留。
- 文件菜单可直接复制下载链接，剩余 6 小时/1 小时内分别使用警告/危险颜色。
- 轻量管理后台：访问 IP、后台登录 IP、Backup 状态、存储、内存、负载、进程和 aria2 监控。
- `/healthz` 提供不含密码和 secret 的部署健康状态。
- 失败任务显示 aria2 错误原因，并可在任务面板输入管理密码后原地重试；重试会继承原任务的保留时间。
- 管理密码入口按 IP 限制连续失败次数，默认 10 分钟内失败 5 次后暂停 10 分钟。
- 服务内置轻量维护线程：默认每小时清理过期文件和上传会话，每天自动备份元数据。
- 管理后台显示最近下载、上传、清理、备份和服务事件；首页概览卡可联动全部文件和即将过期筛选。

仅用于合法资源临时中转，请勿下载或传播侵权内容。

## 文件结构

```text
.
├── README.md
├── TESTING.md
├── requirements.txt
├── download_server.py
├── aria2.conf.example
├── start.sh
├── stop.sh
├── cleanup.sh
├── downloads/
│   └── .gitkeep
├── logs/
│   └── .gitkeep
├── data/
│   ├── .gitkeep
│   ├── uploads/            # 运行时上传会话 JSON
│   └── backups/            # 后台创建的元数据 ZIP，最多保留 10 份
└── tests/
    └── test_download_server.py
```

运行时文件会被 `.gitignore` 忽略，包括下载文件、日志、管理密码、aria2 RPC secret、`data/filemeta.json`、`data/aria2.conf`、`*.aria2` 和 session 文件。

## 安装 aria2

Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y aria2 python3
```

Alpine:

```bash
sudo apk add aria2 python3
```

CentOS/RHEL:

```bash
sudo yum install -y aria2 python3
```

## 启动与停止

```bash
chmod +x start.sh stop.sh cleanup.sh
./start.sh
```

默认访问地址：

```text
http://127.0.0.1:8081/
```

如果部署在反代或平台 preview 后面，请把外部 HTTP 流量转发到 `8081`。

停止：

```bash
./stop.sh
```

查看管理密码：

```bash
cat data/admin_password.txt
```

管理密码用于防止他人滥用添加下载任务；普通下载不需要密码。

## 使用

1. 打开首页。
2. 点击“可用文件”标题右侧的“添加链接”图标，在页面中央弹窗中操作。
3. 输入 HTTP/HTTPS 链接，或点击粘贴按钮读取剪贴板；批量添加时每行填写一个链接。
4. 单个链接可以设置自定义文件名；批量链接使用服务端解析的默认文件名。
5. 提交后首页会显示添加结果，不会跳转到单独的成功页面。
6. 在文件工作区预览或下载文件。视频行同时保留“预览”和“下载”。

二维码分享、一次性下载和无需密码的一键续期位于每个文件的“更多”菜单。也可以点击“可用文件”标题右侧的“上传文件”图标，在中央弹窗中选择文件并输入管理密码直接上传。浏览器先向服务端创建上传会话，再按服务端返回的分片大小和并发数上传；每个分片直接写入同一个临时文件的目标 offset，完成阶段只校验并 rename，不再把所有 `.partN` 文件重新合并一遍。

链接任务未指定自定义文件名时，服务端会暂存 GID 与保留时间的关联；aria2 完成下载并确定实际文件名后，再把保留时间写入文件元数据。

文件的首次入库时间、保留期限、`download_count` 和 `preview_count` 保存在 `data/filemeta.json`。普通下载只有在响应体完整传输后才增加下载计数；HEAD、在线预览、Range 请求和中断传输不会增加下载计数。图片和视频每次打开预览页增加一次预览计数，播放器后续的 Range 请求不重复增加。一次性下载完整传输后会先计数再删除文件，因此对应元数据也随文件一起清理。

网络波动时单个分片最多重试两次；上传过程中可以取消。浏览器关闭或断网后，未完成会话会在 TTL 到期后由清理流程删除。这个实现减少了 finish 等待和磁盘写放大，但不能突破公网、反代或隧道的真实带宽。

禁用 JavaScript 时仍可使用传统 multipart 上传，但默认只允许不超过 50MiB 的请求。对于数百 MiB 或更大的文件，优先使用浏览器分片上传；服务器本机已有文件时，最快也最稳定的方式仍是使用 `scp`/`rsync` 直接写入 `downloads/`。

真实下载地址：

- 普通下载：`/file/<filename>`
- 一次性下载：`/once/<filename>`
- 在线预览页：`/view/<filename>`
- 媒体内联读取：`/media/<filename>`

旧地址 `/downloads/` 会重定向到首页，书签和历史链接仍可继续使用。

## 管理后台

访问 `/admin/`，使用 `data/admin_password.txt` 中的管理密码登录。会话默认保留 12 小时，使用 HttpOnly、SameSite=Strict Cookie；服务重启后需要重新登录。

后台统计口径：

- 活跃人数：最近 15 分钟访问首页或预览页的独立 IP。
- 访问人数：最近 24 小时独立 IP，记录保存在 `logs/visitor.log`。
- 最近登录 IP：成功登录管理后台的 IP，记录保存在 `logs/admin-login.log`。
- 系统监控：Linux 从 `/proc` 读取内存和当前 Python 进程 RSS；不支持的平台显示“不可用”。
- Backup：只备份 `filemeta.json` 和 `task_retention.json`，不复制下载文件、密码或 aria2 secret，最多保留 10 份。
- 最近事件：读取现有日志末尾，不创建数据库或常驻统计数据。
- 自动维护：服务启动时先运行一次，随后按配置间隔清理；元数据备份达到设定间隔后自动创建。

健康检查地址为 `/healthz`。aria2 不可用或磁盘低于安全阈值时返回 `status: degraded`，HTTP 服务仍返回 200，便于区分“服务存活”和“依赖降级”。

## 在线预览

支持图片：

```text
.jpg .jpeg .png .gif .webp .bmp
```

支持视频：

```text
.mp4 .webm .ogg .ogv .mov .m4v .mkv .avi
```

说明：

- 预览只复用本地文件，不调用外部 CDN，不引入播放器库。
- `/media/<filename>` 使用 `Content-Disposition: inline`，并支持 Range，方便视频拖动进度。
- 浏览器能否播放视频取决于封装和编码；例如 H.264/AAC 的 MP4 通常可播，部分 MKV、HEVC 或特殊音频轨可能只能下载。
- SVG 暂不作为在线图片预览类型，避免把用户文件作为同源可执行内容直接内联。

## 配置

通过环境变量调整：

```bash
HOST=0.0.0.0 PORT=8081 ./start.sh
```

可用变量：

```text
HOST=0.0.0.0
PORT=8081
RETENTION_HOURS=24
MIN_FREE_BYTES=2147483648
MAX_DOWNLOAD_DIR_BYTES=12884901888
SINGLE_FILE_LIMIT_BYTES=4294967296
ARIA2_RPC_TIMEOUT=3
UPLOAD_CHUNK_BYTES=5242880
UPLOAD_CONCURRENCY=3
UPLOAD_SESSION_TTL_SECONDS=21600
UPLOAD_FALLBACK_MAX_BYTES=52428800
ADMIN_SESSION_TTL_SECONDS=43200
ADMIN_LOGIN_MAX_FAILURES=5
ADMIN_LOGIN_WINDOW_SECONDS=600
ADMIN_LOGIN_BLOCK_SECONDS=600
MAINTENANCE_INTERVAL_SECONDS=3600
AUTO_BACKUP_INTERVAL_SECONDS=86400
```

说明：

- 系统剩余空间低于 `MIN_FREE_BYTES` 时拒绝新任务。
- `downloads/` 占用超过 `MAX_DOWNLOAD_DIR_BYTES` 时拒绝新任务。
- HTTP/HTTPS 远程文件如果能通过 `Content-Length` 预先得知大小，超过 `SINGLE_FILE_LIMIT_BYTES` 会拒绝；如果远端不提供大小，只能下载过程中依赖磁盘空间保护。
- `RETENTION_HOURS` 控制文件保留时间。
- `UPLOAD_CHUNK_BYTES` 是浏览器分片大小，默认 5MiB。
- `UPLOAD_CONCURRENCY` 是浏览器并发 worker 数，默认 3；隧道不稳定时可降为 1 或 2。
- `UPLOAD_SESSION_TTL_SECONDS` 是未完成上传的保留时间，默认 6 小时。
- `UPLOAD_FALLBACK_MAX_BYTES` 是传统 multipart 上传上限，默认 50MiB。
- `ADMIN_SESSION_TTL_SECONDS` 是管理后台会话期限，默认 12 小时。
- `ADMIN_LOGIN_MAX_FAILURES`、`ADMIN_LOGIN_WINDOW_SECONDS` 和 `ADMIN_LOGIN_BLOCK_SECONDS` 控制管理密码失败限流。
- `MAINTENANCE_INTERVAL_SECONDS` 控制自动清理周期，默认 1 小时；`AUTO_BACKUP_INTERVAL_SECONDS` 控制元数据自动备份周期，默认 1 天。

## 清理

手动清理过期文件：

```bash
./cleanup.sh
```

每小时清理一次的 crontab 示例：

```cron
0 * * * * cd /path/to/cloud-download-manager && ./cleanup.sh
```

清理日志写入：

```text
logs/cleanup.log
```

一次性下载日志写入：

```text
logs/once-download.log
```

上传诊断日志写入：

```text
logs/upload.log
```

访问与后台登录日志：

```text
logs/visitor.log
logs/admin-login.log
logs/backup.log
```

典型分片记录包含 `read_ms`、`write_ms` 和 `total_ms`：

- `read_ms` 高：瓶颈通常在浏览器、反向代理或隧道到 Python 的网络读取。
- `write_ms` 高：瓶颈通常在服务器磁盘写入。
- finish 的 `rename_ms` 应很低；若 finish 仍慢，应检查文件系统或元数据写入错误日志。

## aria2 配置

`aria2.conf.example` 只是模板，不包含真实 secret。`start.sh` 会：

1. 生成或复用 `data/aria2_rpc_secret.txt`。
2. 根据该 secret 写入运行时配置 `data/aria2.conf`。
3. 启动 aria2 RPC，仅监听 `127.0.0.1:6800`。

不要把 `data/aria2_rpc_secret.txt` 或 `data/aria2.conf` 提交到 Git。

## 安全边界

- 不暴露项目根目录。
- 上传初始化需要管理密码验证；后续分片使用服务端生成的短期 ID/token，同名文件拒绝覆盖。
- 上传 ID 只用于派生 `data/uploads/` 和 `downloads/.upload-*.tmp` 下的路径，客户端不能提交任意文件路径。
- 不访问 `downloads/` 之外的文件。
- 拒绝路径穿越和 URL 编码绕过。
- 预览路由同样只允许访问 `downloads/` 内的图片/视频文件。
- 拒绝 `file://`、`ftp://` 和本地路径。
- HTML 不显示管理密码或 aria2 RPC secret。
- 自定义文件名只允许中文、英文、数字、空格、点、下划线和短横线，长度 1~180。

## 测试

本机和 Linux 验收记录见 `TESTING.md`。
