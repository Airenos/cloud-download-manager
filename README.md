# 临时下载站

一个轻量的临时下载站：管理员粘贴 HTTP/HTTPS 下载链接，后端通过 aria2 下载到 `downloads/`，朋友打开页面下载文件。项目只使用 Python 3 标准库和 `aria2c`，不需要数据库，不使用 React/Vue/Vite/Next.js。

## 功能

- 首页 SSR 渲染：统计、文件列表、任务状态、添加任务表单。
- 下载目录页：预览、普通下载、一次性下载、复制链接。
- 图片和视频在线预览：使用浏览器原生 `<img>` / `<video>`，不转码。
- 文件默认保留 24 小时，到期清理。
- 一次性下载完整传输成功后删除文件；中断不会删除。
- aria2 RPC 只访问 `127.0.0.1:6800`，secret 不暴露在 HTML。
- 管理员上传本地文件，需要管理密码验证。

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
│   └── .gitkeep
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
2. 展开“添加下载任务”。
3. 输入 HTTP/HTTPS 链接、自定义文件名（可选）和管理密码。
4. 提交后等待 aria2 下载完成。
5. 打开 `/downloads/` 下载文件。

也可以展开“上传本地文件”，选择文件并输入管理密码直接上传。

真实下载地址：

- 普通下载：`/file/<filename>`
- 一次性下载：`/once/<filename>`
- 在线预览页：`/view/<filename>`
- 媒体内联读取：`/media/<filename>`

`/downloads/` 是服务端渲染页面，不会暴露 Python 默认目录列表。

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
```

说明：

- 系统剩余空间低于 `MIN_FREE_BYTES` 时拒绝新任务。
- `downloads/` 占用超过 `MAX_DOWNLOAD_DIR_BYTES` 时拒绝新任务。
- HTTP/HTTPS 远程文件如果能通过 `Content-Length` 预先得知大小，超过 `SINGLE_FILE_LIMIT_BYTES` 会拒绝；如果远端不提供大小，只能下载过程中依赖磁盘空间保护。
- `RETENTION_HOURS` 控制文件保留时间。

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

## aria2 配置

`aria2.conf.example` 只是模板，不包含真实 secret。`start.sh` 会：

1. 生成或复用 `data/aria2_rpc_secret.txt`。
2. 根据该 secret 写入运行时配置 `data/aria2.conf`。
3. 启动 aria2 RPC，仅监听 `127.0.0.1:6800`。

不要把 `data/aria2_rpc_secret.txt` 或 `data/aria2.conf` 提交到 Git。

## 安全边界

- 不暴露项目根目录。
- 上传接口需要管理密码验证，同名文件拒绝覆盖。
- 不访问 `downloads/` 之外的文件。
- 拒绝路径穿越和 URL 编码绕过。
- 预览路由同样只允许访问 `downloads/` 内的图片/视频文件。
- 拒绝 `file://`、`ftp://` 和本地路径。
- HTML 不显示管理密码或 aria2 RPC secret。
- 自定义文件名只允许中文、英文、数字、空格、点、下划线和短横线，长度 1~180。

## 测试

本机和 Linux 验收记录见 `TESTING.md`。
