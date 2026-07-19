# 测试记录

测试日期：2026-07-18

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

- 自定义文件名合法/非法校验，包括半角和全角括号。
- 首页时间按东八区显示，但不附加 `UTC+8` 文本。
- URL 编码路径穿越拒绝。
- `filemeta.json` 首次入库时间不覆盖。
- `filemeta.json` 下载次数持久化并渲染到文件行。
- `filemeta.json` 图片/视频预览次数持久化并渲染到预览页。
- 普通文件完整下载增加次数，HEAD 和中断下载不增加。
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
- 首页三张状态卡、文件区右上角管理入口、居中管理弹窗、文件/任务双栏、完整管理员字段、文件图标和更多菜单。
- 添加链接 AJAX 提示、剪贴板入口、批量链接校验与 JSON 响应。
- 添加链接后任务面板刷新、活动任务轮询，以及完成任务不再渲染 100% 进度条。
- 未指定文件名的链接任务通过 GID 在完成后保留所选期限。
- 续期无需密码并返回 JSON。
- 文件删除要求管理密码，成功后同时清理文件和元数据。
- 视频同时保留预览与普通下载，更多菜单保留分享、一次性下载和续期。
- 移动端文件优先顺序、文件区管理入口、响应式 CSS 和无障碍菜单钩子。
- 文件面板签名、自动刷新、复制链接、到期颜色和当前标签页状态保留。
- 管理后台密码登录、HttpOnly 会话、退出和未授权拒绝。
- 访问/登录 IP 统计、Linux 系统指标降级、元数据备份和保留状态。
- `/healthz` 返回服务、aria2 和磁盘状态且不包含密码或 secret。
- 管理密码连续失败达到阈值后返回 429 和 `Retry-After`，成功验证可清除失败状态。
- aria2 失败任务渲染中文错误原因和重试入口，重试继承来源、输出文件名和保留时间。
- 自动维护周期清理过期文件、按间隔创建元数据备份并更新后台状态。
- 管理后台聚合最近下载、上传、清理、备份和错误事件。
- 首页概览卡联动全部文件和 6 小时内即将过期筛选。

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

旧下载目录兼容重定向：

```bash
curl.exe -I http://127.0.0.1:18081/downloads/
```

结果：返回 `302`，`Location: /`。

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
curl -I http://127.0.0.1:8081/downloads/ | grep -i "location: /"
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
curl -s http://127.0.0.1:8081/ | grep "test-1mb.bin"
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

- `1440x900`：三张状态卡具有清晰的大号蓝色数值，文件列表与下载任务并排，两个管理入口位于“可用文件”标题右侧。
- `1024x768`：文件/任务双栏保持稳定，状态卡和文件操作不重叠。
- `390x844`：状态卡与工作区正常换行，文件区先出现，任务区随后出现，页面无横向溢出。

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
12. 续期成功后显示包含最新剩余时间的文本提示。
13. 添加链接后任务面板立即刷新；活动任务持续更新，完成后显示“已完成”且不保留 100% 进度条。
14. 完整普通下载后次数增加；HEAD、预览/Range 和中断下载不增加次数。
15. 图片和视频每次打开预览页只增加一次预览次数；媒体 Range 请求不重复累计。
16. 深色模式下文件类型筛选的选中项保持蓝底白字；链接输入框不可拖拽并使用内部滚动条。
17. 文件更多菜单删除操作需要二次确认和管理密码，成功后从当前分页移除。
18. 文件可按创建时间或过期时间正反排序，排序与搜索、类型筛选和分页联动。
19. 首页菜单和预览页的一次性下载均在跳转前提示不可恢复风险。
20. 下载任务自动轮询保留当前任务页；新增任务时才回到第一页。
21. 用户在任务区输入密码或操作控件时，自动轮询不会替换面板或清空输入。
22. 删除和一次性下载使用站内确认弹窗，不调用浏览器原生 `confirm` / `prompt`。
23. 删除文件成功后状态卡中的文件数量、目录占用、剩余空间和磁盘条立即更新。
