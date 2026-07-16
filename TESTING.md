# 测试记录

测试日期：2026-07-16

## 当前本机环境

- OS：Windows / PowerShell
- Python：3.12.5
- `aria2c`：未安装，真实 aria2 下载任务需在 Linux 目标环境验证
- `gh`：未安装，无法使用 GitHub CLI 发布流

## 已运行

### Python 语法检查

```bash
python -m py_compile download_server.py
```

正常开发环境期望结果：exit 0。当前 Codex Windows 沙箱禁止 Python 写入现有 `__pycache__`，因此本轮使用 `python -B` 禁止字节码缓存，并由测试导入完成语法验证。

### 标准库单元测试

```bash
python -B -m unittest tests.test_download_server -v
```

结果：全部通过。

覆盖：

- 自定义文件名合法/非法校验。
- URL 编码路径穿越拒绝。
- `filemeta.json` 首次入库时间不覆盖。
- 过期文件清理和日志。
- 一次性下载完整成功后删除。
- 一次性下载中断保留文件。
- `/once/<filename>` 拒绝 HEAD。
- `/once/<filename>` 拒绝 Range 且不删除文件。
- `/view/<filename>` 对图片渲染 `<img>`。
- `/view/<filename>` 对视频渲染 `<video controls>`。
- `/media/<filename>` 拒绝非媒体文件。
- `/media/<filename>` 支持 Range 并返回 `206 Partial Content`。
- 上传会话 ID 校验、原子 JSON 持久化和服务端路径派生。
- `/api/upload_init` 的密码、文件名、大小、重名和成功会话。
- 分片乱序 offset 直写、重复分片幂等、token 和精确长度校验。
- finish 缺片拒绝、最终 rename、元数据写入和 cancel 幂等清理。
- 上传临时占用、过期会话清理和传统 multipart 读取前 413 拒绝。
- 首页渲染 upload v2 初始化、token、重试、取消和服务端配置客户端。
- 首页 A1 图标栏、居中管理弹窗、完整管理员字段、文件图标和更多菜单。
- 添加链接 AJAX 提示、剪贴板入口、批量链接校验与 JSON 响应。
- 未指定文件名的链接任务通过 GID 在完成后保留所选期限。
- 续期无需密码并返回 JSON。
- 视频同时保留预览与普通下载，更多菜单保留分享、一次性下载和续期。
- 移动端文件优先顺序、底部管理工具栏、响应式 CSS 和无障碍菜单钩子。

### 本地 SSR/API 验证

启动命令：

```powershell
$env:HOST='127.0.0.1'
$env:PORT='18081'
$env:MIN_FREE_BYTES='0'
python download_server.py
```

结果：服务输出 `Serving on http://127.0.0.1:18081`。

首页：

```bash
curl.exe -s http://127.0.0.1:18081/
```

结果：HTML 包含 `临时下载站`，核心数据由 SSR 输出。

下载目录：

```bash
curl.exe -s http://127.0.0.1:18081/downloads/
```

结果：HTML 包含 `下载目录`，测试文件显示在表格中。

API：

```bash
curl.exe -s http://127.0.0.1:18081/api/stats
curl.exe -s http://127.0.0.1:18081/api/files
curl.exe -s http://127.0.0.1:18081/api/tasks
```

结果：`/api/stats` 和 `/api/files` 返回 JSON；本机未运行 aria2 时 `/api/tasks` 返回 `ok: false` 和连接拒绝错误，不影响首页 SSR。

普通下载：

```bash
curl.exe -I http://127.0.0.1:18081/file/probe.txt
```

结果：

```text
HTTP/1.0 200 OK
Content-Disposition: attachment; filename="probe.txt"; filename*=UTF-8''probe.txt
Content-Length: 15
```

在线预览补充：

```bash
curl -s http://127.0.0.1:8081/view/test.mp4 | grep "<video"
curl -I http://127.0.0.1:8081/media/test.mp4
curl -H "Range: bytes=0-3" -i http://127.0.0.1:8081/media/test.mp4
```

期望：预览页由 SSR 输出 `<video>`；`/media/` 返回 `Content-Disposition: inline`；Range 请求返回 `206 Partial Content`。

一次性下载：

```bash
curl.exe -s http://127.0.0.1:18081/once/probe.txt
```

结果：返回文件内容 `probe content`，随后 `downloads/probe.txt` 不存在。

安全路径：

```bash
curl.exe -i http://127.0.0.1:18081/../download_server.py
curl.exe -i http://127.0.0.1:18081/data/admin_password.txt
curl.exe -i http://127.0.0.1:18081/logs/access.log
```

结果：均返回 `HTTP/1.0 403 Forbidden`。

一次性下载防误删：

```bash
curl.exe -I http://127.0.0.1:18081/once/probe.txt
curl.exe -s http://127.0.0.1:18081/once/probe.txt -H "Range: bytes=0-1"
```

结果：HEAD 返回 `405 Method Not Allowed`，Range 返回 `416 Requested Range Not Satisfiable`，文件不会被删除。

## 需要在 Linux + aria2 环境运行

```bash
./start.sh
ss -tlnp | grep 8081
ss -tlnp | grep 6800
curl -s http://127.0.0.1:8081/ | grep "临时下载站"
curl -s http://127.0.0.1:8081/downloads/ | grep "下载目录"
curl -s http://127.0.0.1:8081/api/stats
curl -s http://127.0.0.1:8081/api/files
curl -s http://127.0.0.1:8081/api/tasks
```

添加 HTTP 小文件任务示例：

```bash
PASSWORD="$(cat data/admin_password.txt)"
curl -s -X POST http://127.0.0.1:8081/api/add-task \
  -d "url=https://speed.cloudflare.com/__down?bytes=1048576" \
  -d "filename=test-1mb.bin" \
  -d "password=${PASSWORD}"
```

确认返回 GID，下载完成后检查：

```bash
ls -lh downloads/
curl -s http://127.0.0.1:8081/downloads/ | grep "test-1mb.bin"
curl -I http://127.0.0.1:8081/file/test-1mb.bin
```

HTML 不暴露 secret：

```bash
curl -s http://127.0.0.1:8081/ | grep -i "secret"
```

期望：不出现 aria2 RPC secret 或管理密码。

## 上传 v2 部署验收

在 Linux 目标环境通过浏览器依次验证：

1. 上传约 10MiB 文件，确认进度、下载内容和文件名正确。
2. 上传约 200MiB 文件，确认 finish 阶段不再长时间合并。
3. 上传中点击取消，确认对应 `data/uploads/*.json` 和 `downloads/.upload-*.tmp` 被删除。
4. 使用错误密码初始化，确认返回 403 且不创建临时文件。
5. 上传与现有文件同名的文件，确认返回 409 且原文件未改变。
6. 中断上传并将会话放置超过 `UPLOAD_SESSION_TTL_SECONDS`，运行 `./cleanup.sh` 后确认状态和临时文件被清理。

检查诊断日志：

```bash
tail -f logs/upload.log
```

分片记录中的 `read_ms` 明显高于 `write_ms` 时，优先检查网络、反代和隧道；`write_ms` 较高时检查服务器磁盘。日志不得包含管理密码或 `upload_token`。

## A1 UI 视觉验收

使用以下视口检查首页：

- `1440x900`：左侧 64px 图标栏，右侧文件区；三个入口分别打开页面中央弹窗。
- `1024x768`：图标栏和文件区保持稳定，状态条和文件操作不重叠。
- `390x844`：文件区先出现，三个管理入口固定在底部且不遮挡内容。

每个视口覆盖：

1. 长中英文文件名不挤出操作按钮或产生横向页面滚动。
2. 站点无文件与搜索无结果显示不同空状态。
3. 视频行同时显示“预览”和“下载”。
4. 更多菜单包含二维码分享、一次性下载和续期；点击外部和按 `Escape` 均可关闭。
5. 上传中显示进度、分片、活跃 worker、重试次数和取消按钮。
6. aria2 不可用只在任务区显示错误，不影响文件和上传区域。
7. 浅色和深色主题下的文字、焦点、警示操作和磁盘状态清晰可辨。
8. 弹窗可通过关闭按钮、遮罩和 `Escape` 关闭；上传进行中必须先完成或取消。
9. 添加单个或多个链接后停留在首页并显示结果；批量模式禁用自定义文件名。
10. 上传或链接任务选择 7 天后，文件元数据标签为 `7d`，剩余时间接近 168 小时。
11. 图片文件显示“预览”；点击续期无需输入密码，并立即更新该行剩余时间。
