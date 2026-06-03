# 测试记录

测试日期：2026-06-03

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

结果：exit 0。

### 标准库单元测试

```bash
python -m unittest tests.test_download_server -v
```

结果：

```text
Ran 8 tests in 0.683s
OK
```

覆盖：

- 自定义文件名合法/非法校验。
- URL 编码路径穿越拒绝。
- `filemeta.json` 首次入库时间不覆盖。
- 过期文件清理和日志。
- 一次性下载完整成功后删除。
- 一次性下载中断保留文件。
- `/once/<filename>` 拒绝 HEAD。
- `/once/<filename>` 拒绝 Range 且不删除文件。

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
