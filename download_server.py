#!/usr/bin/env python3
"""A tiny temporary download site backed by aria2 RPC."""

from __future__ import annotations

import contextlib
import email.utils
import hmac
import html
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent)).resolve()
DOWNLOADS_DIR = (APP_ROOT / "downloads").resolve()
LOGS_DIR = (APP_ROOT / "logs").resolve()
DATA_DIR = (APP_ROOT / "data").resolve()
META_PATH = DATA_DIR / "filemeta.json"
ADMIN_PASSWORD_PATH = DATA_DIR / "admin_password.txt"
ARIA2_SECRET_PATH = DATA_DIR / "aria2_rpc_secret.txt"
ARIA2_RPC_URL = os.environ.get("ARIA2_RPC_URL", "http://127.0.0.1:6800/jsonrpc")
ARIA2_RPC_TIMEOUT = float(os.environ.get("ARIA2_RPC_TIMEOUT", "3"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
RETENTION_SECONDS = int(RETENTION_HOURS * 3600)
MIN_FREE_BYTES = int(os.environ.get("MIN_FREE_BYTES", str(2 * 1024**3)))
MAX_DOWNLOAD_DIR_BYTES = int(os.environ.get("MAX_DOWNLOAD_DIR_BYTES", str(12 * 1024**3)))
SINGLE_FILE_LIMIT_BYTES = int(os.environ.get("SINGLE_FILE_LIMIT_BYTES", str(4 * 1024**3)))
TIMEZONE_CN = timezone(timedelta(hours=8))

ALLOWED_URL_PREFIXES = ("http://", "https://", "magnet:?")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v", ".mkv", ".avi"}
BANNED_PREFIXES = (
    "/.git",
    "/logs",
    "/data",
    "/download_server.py",
    "/start.sh",
    "/stop.sh",
    "/cleanup.sh",
    "/aria2.conf",
    "/aria2.session",
)


def ensure_directories() -> None:
    for directory in (DOWNLOADS_DIR, LOGS_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def chmod_600(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def read_or_create_secret(path: Path, length: int = 24) -> str:
    ensure_directories()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            chmod_600(path)
            return value
    value = secrets.token_urlsafe(length)
    path.write_text(value + "\n", encoding="utf-8")
    chmod_600(path)
    return value


def get_admin_password() -> str:
    return read_or_create_secret(ADMIN_PASSWORD_PATH, 18)


def get_aria2_secret() -> str:
    return read_or_create_secret(ARIA2_SECRET_PATH, 24)


def format_size(size: int | float | None) -> str:
    if not size:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_speed(speed: int | float | None) -> str:
    return f"{format_size(speed)}/s"


def now_ts() -> float:
    return time.time()


def format_time(ts: int | float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(TIMEZONE_CN).strftime(
        "%Y-%m-%d %H:%M:%S UTC+8"
    )


def format_remaining(expires_at: float) -> str:
    remaining = int(expires_at - now_ts())
    if remaining <= 0:
        return "已过期"
    hours, rem = divmod(remaining, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def get_downloads_usage() -> int:
    total = 0
    if not DOWNLOADS_DIR.exists():
        return 0
    for path in DOWNLOADS_DIR.iterdir():
        if path.is_file() and not path.name.startswith(".") and path.suffix != ".aria2":
            total += path.stat().st_size
    return total


def get_disk_stats() -> dict[str, int | str]:
    ensure_directories()
    usage = shutil.disk_usage(DOWNLOADS_DIR)
    used = get_downloads_usage()
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "downloads_used": used,
        "total_human": format_size(usage.total),
        "used_human": format_size(usage.used),
        "free_human": format_size(usage.free),
        "downloads_used_human": format_size(used),
    }


def load_meta() -> dict[str, dict[str, float]]:
    ensure_directories()
    if not META_PATH.exists():
        return {}
    try:
        with META_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for name, item in data.items():
        if isinstance(name, str) and isinstance(item, dict):
            try:
                result[name] = {"created_at": float(item["created_at"])}
            except (KeyError, TypeError, ValueError):
                continue
    return result


def save_meta(meta: dict[str, dict[str, float]]) -> None:
    ensure_directories()
    tmp_path = META_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(META_PATH)


def is_visible_download(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith(".") or path.suffix == ".aria2":
        return False
    if (DOWNLOADS_DIR / f"{path.name}.aria2").exists():
        return False
    return True


def scan_files() -> list[dict[str, object]]:
    ensure_directories()
    meta = load_meta()
    changed = False
    current_files: set[str] = set()
    for path in DOWNLOADS_DIR.iterdir():
        if not is_visible_download(path):
            continue
        current_files.add(path.name)
        if path.name not in meta:
            meta[path.name] = {"created_at": now_ts()}
            changed = True
    for stale_name in list(meta.keys()):
        if stale_name not in current_files:
            meta.pop(stale_name, None)
            changed = True
    if changed:
        save_meta(meta)
    files = []
    for name in sorted(current_files, key=str.casefold):
        path = DOWNLOADS_DIR / name
        created_at = meta[name]["created_at"]
        expires_at = created_at + RETENTION_SECONDS
        files.append(
            {
                "name": name,
                "size": path.stat().st_size,
                "size_human": format_size(path.stat().st_size),
                "created_at": created_at,
                "created_at_text": format_time(created_at),
                "expires_at": expires_at,
                "expires_at_text": format_time(expires_at),
                "remaining_text": format_remaining(expires_at),
                "url_name": urllib.parse.quote(name),
            }
        )
    return files


def append_log(log_name: str, message: str) -> None:
    ensure_directories()
    line = f"{format_time(now_ts())} {message}\n"
    with (LOGS_DIR / log_name).open("a", encoding="utf-8") as f:
        f.write(line)


def cleanup_expired() -> list[str]:
    ensure_directories()
    meta = load_meta()
    cutoff = now_ts() - RETENTION_SECONDS
    removed: list[str] = []
    changed = False
    for name, item in list(meta.items()):
        path = DOWNLOADS_DIR / name
        if not path.exists():
            meta.pop(name, None)
            changed = True
            continue
        if item.get("created_at", now_ts()) <= cutoff:
            with contextlib.suppress(OSError):
                path.unlink()
            with contextlib.suppress(OSError):
                (DOWNLOADS_DIR / f"{name}.aria2").unlink()
            meta.pop(name, None)
            removed.append(name)
            changed = True
            append_log("cleanup.log", f"expired removed={name}")
    if changed:
        save_meta(meta)
    return removed


def validate_custom_filename(filename: str) -> str:
    name = (filename or "").strip()
    if not 1 <= len(name) <= 180:
        raise ValueError("文件名长度必须为 1~180 个字符")
    if "/" in name or "\\" in name:
        raise ValueError("文件名不能包含路径分隔符")
    if ".." in name or name in {".", ".."}:
        raise ValueError("文件名不能包含路径穿越片段")
    for char in name:
        code = ord(char)
        if code < 32 or code == 127:
            raise ValueError("文件名不能包含控制字符")
        if char in " ._-":
            continue
        if "0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z":
            continue
        if "\u4e00" <= char <= "\u9fff":
            continue
        raise ValueError("文件名只允许中文、英文、数字、空格、点、下划线和短横线")
    return name


def decode_path_segment(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_decoded = urllib.parse.unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    return decoded


def safe_download_path(filename: str) -> Path:
    name = decode_path_segment(filename)
    if not name or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("非法文件名")
    if ".." in name or name in {".", ".."}:
        raise ValueError("非法文件名")
    path = (DOWNLOADS_DIR / name).resolve()
    try:
        path.relative_to(DOWNLOADS_DIR)
    except ValueError as exc:
        raise ValueError("非法路径") from exc
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(name)
    return path


def check_admin_password(password: str) -> bool:
    return hmac.compare_digest(password or "", get_admin_password())


def validate_task_url(url: str) -> str:
    value = (url or "").strip()
    if not value.startswith(ALLOWED_URL_PREFIXES):
        raise ValueError("只允许 http://、https:// 或 magnet:? 链接")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in {"file", "ftp"}:
        raise ValueError("不允许 file:// 或 ftp:// 链接")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise ValueError("HTTP/HTTPS 链接必须包含主机名")
    return value


def get_remote_content_length(url: str) -> int | None:
    if not url.startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            value = response.headers.get("Content-Length")
    except Exception:
        return None
    try:
        return int(value) if value else None
    except ValueError:
        return None


def ensure_can_add_task(url: str) -> None:
    stats = get_disk_stats()
    free = int(stats["free"])
    downloads_used = int(stats["downloads_used"])
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"系统剩余空间低于 {format_size(MIN_FREE_BYTES)}，已拒绝新任务")
    if downloads_used >= MAX_DOWNLOAD_DIR_BYTES:
        raise RuntimeError(f"downloads 目录超过 {format_size(MAX_DOWNLOAD_DIR_BYTES)}，已拒绝新任务")
    remote_size = get_remote_content_length(url)
    if remote_size is not None and remote_size > SINGLE_FILE_LIMIT_BYTES:
        raise RuntimeError(f"远程文件超过单文件限制 {format_size(SINGLE_FILE_LIMIT_BYTES)}")


def aria2_rpc(method: str, params: list[object] | None = None) -> object:
    secret = get_aria2_secret()
    rpc_params = [f"token:{secret}"]
    if params:
        rpc_params.extend(params)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": rpc_params}
    ).encode("utf-8")
    request = urllib.request.Request(
        ARIA2_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=ARIA2_RPC_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    if "error" in data:
        error = data["error"]
        raise RuntimeError(error.get("message", "aria2 RPC 调用失败"))
    return data.get("result")


def add_aria2_task(url: str, filename: str | None = None) -> str:
    validated_url = validate_task_url(url)
    ensure_can_add_task(validated_url)
    options = {"dir": str(DOWNLOADS_DIR)}
    if filename:
        options["out"] = validate_custom_filename(filename)
    result = aria2_rpc("aria2.addUri", [[validated_url], options])
    if not isinstance(result, str):
        raise RuntimeError("aria2 未返回 GID")
    return result


def remove_aria2_task(gid: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{1,32}", gid or ""):
        raise ValueError("非法 GID")
    with contextlib.suppress(Exception):
        aria2_rpc("aria2.remove", [gid])
    with contextlib.suppress(Exception):
        aria2_rpc("aria2.removeDownloadResult", [gid])


def clear_stopped_tasks() -> int:
    tasks = aria2_rpc("aria2.tellStopped", [0, 10]) or []
    count = 0
    if isinstance(tasks, list):
        for task in tasks:
            gid = task.get("gid") if isinstance(task, dict) else None
            if gid:
                with contextlib.suppress(Exception):
                    aria2_rpc("aria2.removeDownloadResult", [gid])
                    count += 1
    return count


def task_name(task: dict[str, object]) -> str:
    bittorrent = task.get("bittorrent")
    if isinstance(bittorrent, dict):
        info = bittorrent.get("info")
        if isinstance(info, dict) and info.get("name"):
            return str(info["name"])
    files = task.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("path"):
                path = str(item["path"])
                if path:
                    return Path(path).name or path
    return str(task.get("gid", "未知任务"))


def normalize_task(task: dict[str, object]) -> dict[str, object]:
    name = task_name(task)
    status = str(task.get("status", "-"))
    total = int(task.get("totalLength") or 0)
    completed = int(task.get("completedLength") or 0)
    speed = int(task.get("downloadSpeed") or 0)
    progress = round((completed / total) * 100, 1) if total else 0.0
    hint = ""
    if name.startswith("[METADATA]"):
        hint = "正在获取磁力链接元数据。若 3-5 分钟无速度，可能是当前平台不支持 BT/DHT。"
    elif total == 0 and speed == 0:
        hint = "等待元数据"
    return {
        "gid": str(task.get("gid", "")),
        "name": name,
        "status": status,
        "progress": progress,
        "speed": speed,
        "speed_human": format_speed(speed),
        "completed": completed,
        "completed_human": format_size(completed),
        "total": total,
        "total_human": format_size(total),
        "hint": hint,
    }


def get_aria2_tasks() -> dict[str, object]:
    try:
        active = aria2_rpc("aria2.tellActive") or []
        waiting = aria2_rpc("aria2.tellWaiting", [0, 100]) or []
        stopped = aria2_rpc("aria2.tellStopped", [0, 10]) or []
    except Exception as exc:
        return {"ok": False, "error": str(exc), "active": [], "waiting": [], "stopped": [], "tasks": []}
    result = {
        "ok": True,
        "error": "",
        "active": [normalize_task(item) for item in active if isinstance(item, dict)],
        "waiting": [normalize_task(item) for item in waiting if isinstance(item, dict)],
        "stopped": [normalize_task(item) for item in stopped if isinstance(item, dict)],
    }
    result["tasks"] = result["active"] + result["waiting"] + result["stopped"]
    return result


def stream_once_file(path: Path, write_chunk, chunk_size: int = 1024 * 128) -> None:
    name = path.name
    completed = False
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                write_chunk(chunk)
        completed = True
    finally:
        if completed:
            with contextlib.suppress(OSError):
                path.unlink()
            meta = load_meta()
            if name in meta:
                meta.pop(name, None)
                save_meta(meta)
            append_log("once-download.log", f"completed removed={name}")
        else:
            append_log("once-download.log", f"interrupted kept={name}")


def json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def media_kind(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def copy_file_range(path: Path, output, start: int, length: int, chunk_size: int = 1024 * 128) -> None:
    remaining = length
    with path.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if file_size <= 0 or not range_header.startswith("bytes="):
        raise ValueError("invalid range")
    spec = range_header.removeprefix("bytes=").strip()
    if "," in spec or "-" not in spec:
        raise ValueError("invalid range")
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid range")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except ValueError as exc:
        raise ValueError("invalid range") from exc
    if start < 0 or end < start or start >= file_size:
        raise ValueError("invalid range")
    return start, min(end, file_size - 1)


def page(title: str, body: str) -> bytes:
    css = """
    :root { color-scheme: light; --primary: #6d5dfc; --primary-dark: #5144d8; --bg: #f6f7fb; --text: #1f2430; --muted: #6b7280; --line: #e5e7eb; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    main { width: min(1100px, calc(100% - 28px)); margin: 0 auto; padding: 28px 0 48px; }
    header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; margin-bottom: 18px; }
    h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }
    p { margin: 6px 0; color: var(--muted); line-height: 1.55; }
    a { color: var(--primary-dark); text-decoration: none; }
    .actions, .row-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .button, button, input[type=submit] { border: 0; border-radius: 8px; background: var(--primary); color: #fff; padding: 9px 13px; font-weight: 650; cursor: pointer; line-height: 1.2; }
    .button.secondary, button.secondary { background: #eef0ff; color: var(--primary-dark); }
    button.danger { background: #ef4444; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
    .card, section, details { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }
    .card strong { display: block; font-size: 22px; margin-top: 6px; }
    section, details { margin-top: 14px; }
    summary { cursor: pointer; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }
    th { color: var(--muted); font-size: 13px; }
    .muted { color: var(--muted); }
    .notice { background: #fff; border-left: 4px solid var(--primary); padding: 12px 14px; border-radius: 8px; margin-top: 14px; }
    .empty { padding: 16px; color: var(--muted); text-align: center; }
    .progress { width: 110px; height: 8px; background: #eef0f6; border-radius: 999px; overflow: hidden; margin-top: 5px; }
    .bar { height: 100%; background: linear-gradient(90deg, #6d5dfc, #8b5cf6); }
    .viewer { background: #0f172a; border-radius: 8px; padding: 10px; }
    .viewer img, .viewer video { display: block; width: 100%; max-height: 72vh; object-fit: contain; border-radius: 6px; background: #0f172a; }
    form.inline { display: inline; }
    label { display: block; font-weight: 650; margin: 10px 0 5px; }
    input[type=text], input[type=password], input[type=url] { width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 10px; font: inherit; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
    @media (max-width: 720px) { header, .form-grid { display: block; } .actions { margin-top: 12px; } th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) { display: none; } }
    """
    script = """
    function copyLink(path) {
      const url = new URL(path, window.location.href).href;
      navigator.clipboard.writeText(url).then(function(){ alert('链接已复制'); });
    }
    """
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{css}</style>
</head>
<body>
  <main>{body}</main>
  <script>{script}</script>
</body>
</html>"""
    return html_doc.encode("utf-8")


def render_file_rows(files: list[dict[str, object]], compact: bool = False) -> str:
    if not files:
        return '<div class="empty">暂无可下载文件</div>'
    rows = []
    for item in files:
        name = str(item["name"])
        url_name = str(item["url_name"])
        preview = f'<a class="button secondary" href="/view/{url_name}">预览</a> ' if media_kind(name) else ""
        if compact:
            actions = f'{preview}<a class="button secondary" href="/file/{url_name}">下载</a>'
        else:
            actions = (
                preview +
                f'<a class="button secondary" href="/file/{url_name}">普通下载</a> '
                f'<a class="button" href="/once/{url_name}">一次性下载</a> '
                f'<button class="secondary" type="button" onclick="copyLink(\'/file/{url_name}\')">复制链接</button>'
            )
        rows.append(
            "<tr>"
            f'<td class="code">{html.escape(name)}</td>'
            f"<td>{html.escape(str(item['size_human']))}</td>"
            f"<td>{html.escape(str(item['created_at_text']))}</td>"
            f"<td>{html.escape(str(item['expires_at_text']))}</td>"
            f"<td>{html.escape(str(item['remaining_text']))}</td>"
            f'<td class="row-actions">{actions}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>文件名</th><th>大小</th><th>入库时间</th><th>过期时间</th><th>剩余时间</th><th>操作</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_task_rows(task_data: dict[str, object]) -> str:
    if not task_data.get("ok"):
        error = html.escape(str(task_data.get("error") or "aria2 RPC 暂不可用"))
        return f'<p class="muted">无法读取 aria2 任务：{error}</p>'
    tasks = task_data.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return '<div class="empty">暂无下载任务</div>'
    rows = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        gid = html.escape(str(task["gid"]))
        hint = f'<p class="muted">{html.escape(str(task["hint"]))}</p>' if task.get("hint") else ""
        progress = float(task["progress"])
        rows.append(
            "<tr>"
            f'<td class="code">{html.escape(str(task["name"]))}{hint}</td>'
            f"<td>{html.escape(str(task['status']))}</td>"
            f'<td>{progress:.1f}%<div class="progress"><div class="bar" style="width:{min(progress, 100):.1f}%"></div></div></td>'
            f"<td>{html.escape(str(task['speed_human']))}</td>"
            f"<td>{html.escape(str(task['completed_human']))} / {html.escape(str(task['total_human']))}</td>"
            f'<td class="code">{gid}</td>'
            '<td><form class="inline" method="post" action="/api/remove-task">'
            f'<input type="hidden" name="gid" value="{gid}">'
            '<input type="password" name="password" placeholder="管理密码" required>'
            '<button class="danger" type="submit">删除</button></form></td>'
            "</tr>"
        )
    clear_form = (
        '<form method="post" action="/api/clear-stopped">'
        '<input type="password" name="password" placeholder="管理密码" required> '
        '<button class="secondary" type="submit">清理已完成任务记录</button>'
        "</form>"
    )
    return (
        "<table><thead><tr><th>任务名</th><th>状态</th><th>进度</th><th>速度</th><th>已下载/总大小</th><th>GID</th><th>操作</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{clear_form}"
    )


def render_home(message: str = "") -> bytes:
    cleanup_expired()
    files = scan_files()
    stats = get_disk_stats()
    tasks = get_aria2_tasks()
    message_html = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    body = f"""
<header>
  <div>
    <h1>临时下载站</h1>
    <p>管理员添加 HTTP/HTTPS 链接后，小鸡下载到本地，朋友再从这里取文件。</p>
  </div>
  <div class="actions">
    <a class="button" href="/downloads/">下载目录</a>
    <button class="secondary" type="button" onclick="location.reload()">刷新</button>
  </div>
</header>
{message_html}
<div class="cards">
  <div class="card">下载目录占用<strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
  <div class="card">剩余磁盘空间<strong>{html.escape(str(stats["free_human"]))}</strong></div>
  <div class="card">文件数量<strong>{len(files)}</strong></div>
  <div class="card">保留时间<strong>{RETENTION_HOURS:g} 小时</strong></div>
</div>
<section>
  <h2>可用文件</h2>
  {render_file_rows(files, compact=True)}
</section>
<details>
  <summary>下载任务状态</summary>
  {render_task_rows(tasks)}
</details>
<details>
  <summary>添加下载任务</summary>
  <p>管理密码用于防止他人滥用下载任务，普通下载不需要密码。查看命令：<span class="code">cat data/admin_password.txt</span></p>
  <form method="post" action="/api/add-task">
    <label for="url">下载链接</label>
    <input id="url" name="url" type="text" inputmode="url" placeholder="https://example.com/file.zip" required>
    <div class="form-grid">
      <div>
        <label for="filename">自定义文件名（可选）</label>
        <input id="filename" name="filename" type="text" maxlength="180" placeholder="example.zip">
      </div>
      <div>
        <label for="password">管理密码</label>
        <input id="password" name="password" type="password" required>
      </div>
    </div>
    <p class="muted">文件名只允许中文、英文、数字、空格、点、下划线和短横线。</p>
    <input type="submit" value="添加任务">
  </form>
</details>
<div class="notice">
  <p>所有文件 {RETENTION_HOURS:g} 小时后自动删除；一次性下载在完整下载成功后自动删除。</p>
  <p>magnet 为实验性支持，当前平台可能无法稳定获取元数据，推荐 HTTP/HTTPS 直链。</p>
  <p>仅用于合法资源临时中转。剩余空间低于 {format_size(MIN_FREE_BYTES)} 时会拒绝新任务。</p>
</div>
"""
    return page("临时下载站", body)


def render_downloads() -> bytes:
    cleanup_expired()
    files = scan_files()
    stats = get_disk_stats()
    body = f"""
<header>
  <div>
    <h1>下载目录</h1>
    <p>普通下载不会删除文件；一次性下载完整完成后会删除文件。</p>
  </div>
  <div class="actions">
    <a class="button secondary" href="/">返回首页</a>
    <button class="secondary" type="button" onclick="location.reload()">刷新</button>
  </div>
</header>
<div class="cards">
  <div class="card">下载目录占用<strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
  <div class="card">剩余磁盘空间<strong>{html.escape(str(stats["free_human"]))}</strong></div>
  <div class="card">文件数量<strong>{len(files)}</strong></div>
</div>
<section>
  <h2>下载目录</h2>
  {render_file_rows(files)}
</section>
"""
    return page("下载目录", body)


def render_view(path: Path) -> bytes:
    kind = media_kind(path.name)
    url_name = urllib.parse.quote(path.name)
    media_url = f"/media/{url_name}"
    download_url = f"/file/{url_name}"
    once_url = f"/once/{url_name}"
    if kind == "image":
        viewer = f'<div class="viewer"><img src="{media_url}" alt="{html.escape(path.name)}"></div>'
    elif kind == "video":
        viewer = (
            '<div class="viewer">'
            f'<video controls preload="metadata" src="{media_url}">'
            f'<a href="{download_url}">下载视频</a>'
            "</video></div>"
        )
    else:
        viewer = '<section><p>这个文件类型暂不支持在线预览，请使用普通下载。</p></section>'
    body = f"""
<header>
  <div>
    <h1>在线预览</h1>
    <p class="code">{html.escape(path.name)}</p>
  </div>
  <div class="actions">
    <a class="button secondary" href="/downloads/">返回下载目录</a>
    <a class="button secondary" href="{download_url}">普通下载</a>
    <a class="button" href="{once_url}">一次性下载</a>
  </div>
</header>
{viewer}
<div class="notice">
  <p>浏览器能否播放视频取决于文件编码；如果无法播放，请使用普通下载。</p>
</div>
"""
    return page(f"在线预览 - {path.name}", body)


def success_page(title: str, message: str) -> bytes:
    body = f"""
<section>
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(message)}</p>
  <div class="actions"><a class="button" href="/">返回首页</a><a class="button secondary" href="/downloads/">下载目录</a></div>
</section>
"""
    return page(title, body)


def parse_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(min(length, 1024 * 64)).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    safe_name = filename.replace('"', "'")
    quoted = urllib.parse.quote(filename)
    return f'{disposition}; filename="{safe_name}"; filename*=UTF-8\'\'{quoted}'


class DownloadHandler(BaseHTTPRequestHandler):
    server_version = "TempDownloadServer/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        append_log("access.log", f"{self.client_address[0]} {fmt % args}")

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_page(self, status: int, message: str) -> None:
        self.send_bytes(status, success_page(HTTPStatus(status).phrase, message))

    def normalized_path(self) -> str:
        raw_path = urllib.parse.urlsplit(self.path).path
        decoded = decode_path_segment(raw_path)
        return posixpath.normpath(decoded)

    def is_banned_path(self) -> bool:
        decoded = decode_path_segment(urllib.parse.urlsplit(self.path).path)
        if ".." in decoded:
            return True
        normalized = posixpath.normpath(decoded)
        return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in BANNED_PREFIXES)

    def do_HEAD(self) -> None:
        if self.is_banned_path():
            self.send_error_page(403, "禁止访问")
            return
        raw_path = urllib.parse.urlsplit(self.path).path
        if raw_path.startswith("/file/"):
            self.handle_file(raw_path.removeprefix("/file/"), head_only=True)
            return
        if raw_path.startswith("/media/"):
            self.handle_media(raw_path.removeprefix("/media/"), head_only=True)
            return
        if raw_path.startswith("/once/"):
            self.send_error_page(405, "一次性下载不支持 HEAD")
            return
        if raw_path in {"/", "/downloads/"}:
            self.do_GET()
            return
        self.send_error_page(404, "页面不存在")

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_error_page(500, "服务内部错误")

    def route_get(self) -> None:
        if self.is_banned_path():
            self.send_error_page(403, "禁止访问")
            return
        raw_path = urllib.parse.urlsplit(self.path).path
        if raw_path == "/":
            self.send_bytes(200, render_home())
        elif raw_path == "/downloads/":
            self.send_bytes(200, render_downloads())
        elif raw_path == "/api/stats":
            self.send_bytes(200, json_bytes(get_disk_stats()), "application/json; charset=utf-8")
        elif raw_path == "/api/files":
            self.send_bytes(200, json_bytes(scan_files()), "application/json; charset=utf-8")
        elif raw_path == "/api/tasks":
            self.send_bytes(200, json_bytes(get_aria2_tasks()), "application/json; charset=utf-8")
        elif raw_path.startswith("/view/"):
            self.handle_view(raw_path.removeprefix("/view/"))
        elif raw_path.startswith("/media/"):
            self.handle_media(raw_path.removeprefix("/media/"), head_only=False)
        elif raw_path.startswith("/file/"):
            self.handle_file(raw_path.removeprefix("/file/"), head_only=False)
        elif raw_path.startswith("/once/"):
            self.handle_once(raw_path.removeprefix("/once/"))
        else:
            self.send_error_page(404, "页面不存在")

    def do_POST(self) -> None:
        try:
            self.route_post()
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_error_page(500, "服务内部错误")

    def route_post(self) -> None:
        if self.is_banned_path():
            self.send_error_page(403, "禁止访问")
            return
        raw_path = urllib.parse.urlsplit(self.path).path
        form = parse_form(self)
        if raw_path == "/api/add-task":
            self.handle_add_task(form)
        elif raw_path == "/api/remove-task":
            self.handle_remove_task(form)
        elif raw_path == "/api/clear-stopped":
            self.handle_clear_stopped(form)
        else:
            self.send_error_page(404, "接口不存在")

    def handle_add_task(self, form: dict[str, str]) -> None:
        if not check_admin_password(form.get("password", "")):
            self.send_error_page(403, "管理密码错误")
            return
        try:
            filename = form.get("filename", "").strip() or None
            gid = add_aria2_task(form.get("url", ""), filename)
        except Exception as exc:
            self.send_error_page(400, f"添加任务失败：{exc}")
            return
        self.send_bytes(200, success_page("任务已添加", f"GID: {gid}"))

    def handle_remove_task(self, form: dict[str, str]) -> None:
        if not check_admin_password(form.get("password", "")):
            self.send_error_page(403, "管理密码错误")
            return
        try:
            remove_aria2_task(form.get("gid", ""))
        except Exception as exc:
            self.send_error_page(400, f"删除任务失败：{exc}")
            return
        self.send_bytes(200, success_page("任务已删除", "已向 aria2 发送删除请求。"))

    def handle_clear_stopped(self, form: dict[str, str]) -> None:
        if not check_admin_password(form.get("password", "")):
            self.send_error_page(403, "管理密码错误")
            return
        try:
            count = clear_stopped_tasks()
        except Exception as exc:
            self.send_error_page(400, f"清理失败：{exc}")
            return
        self.send_bytes(200, success_page("已清理任务记录", f"清理数量：{count}"))

    def handle_file(self, encoded_name: str, head_only: bool) -> None:
        try:
            path = safe_download_path(encoded_name)
        except FileNotFoundError:
            self.send_error_page(404, "文件不存在")
            return
        except ValueError:
            self.send_error_page(403, "非法文件路径")
            return
        stat = path.stat()
        headers = {
            "Content-Disposition": content_disposition(path.name),
            "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
            "Content-Length": str(stat.st_size),
        }
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def handle_view(self, encoded_name: str) -> None:
        try:
            path = safe_download_path(encoded_name)
        except FileNotFoundError:
            self.send_error_page(404, "文件不存在")
            return
        except ValueError:
            self.send_error_page(403, "非法文件路径")
            return
        self.send_bytes(200, render_view(path))

    def handle_media(self, encoded_name: str, head_only: bool) -> None:
        try:
            path = safe_download_path(encoded_name)
        except FileNotFoundError:
            self.send_error_page(404, "文件不存在")
            return
        except ValueError:
            self.send_error_page(403, "非法文件路径")
            return
        if not media_kind(path.name):
            self.send_error_page(415, "这个文件类型不支持在线预览")
            return

        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        headers = {
            "Content-Type": mime,
            "Content-Disposition": content_disposition(path.name, "inline"),
            "Accept-Ranges": "bytes",
            "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
            "X-Content-Type-Options": "nosniff",
        }

        if range_header:
            try:
                start, end = parse_range_header(range_header, stat.st_size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if not head_only:
                copy_file_range(path, self.wfile, start, length)
            return

        self.send_response(200)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(stat.st_size))
        self.end_headers()
        if not head_only:
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def handle_once(self, encoded_name: str) -> None:
        if self.command != "GET":
            self.send_error_page(405, "一次性下载只允许 GET")
            return
        if self.headers.get("Range"):
            self.send_error_page(416, "一次性下载不支持 Range")
            return
        try:
            path = safe_download_path(encoded_name)
        except FileNotFoundError:
            self.send_error_page(404, "文件不存在")
            return
        except ValueError:
            self.send_error_page(403, "非法文件路径")
            return
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", content_disposition(path.name))
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        try:
            stream_once_file(path, self.wfile.write)
        except (BrokenPipeError, ConnectionError, OSError):
            return


def run_server() -> None:
    ensure_directories()
    get_admin_password()
    get_aria2_secret()
    cleanup_expired()
    server = ThreadingHTTPServer((HOST, PORT), DownloadHandler)
    print(f"Serving on http://{HOST}:{PORT}")
    with server:
        server.serve_forever()


if __name__ == "__main__":
    run_server()
