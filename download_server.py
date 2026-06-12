#!/usr/bin/env python3
"""A tiny temporary download site backed by aria2 RPC."""

from __future__ import annotations

import contextlib
import email.utils
import hmac
import html
import io
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

ALLOWED_URL_PREFIXES = ("http://", "https://")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v", ".mkv", ".avi"}
TEXT_EXTENSIONS = {".txt", ".md", ".json", ".log", ".conf", ".cfg", ".ini",
                   ".yaml", ".yml", ".xml", ".csv", ".sh", ".py", ".js",
                   ".css", ".html", ".toml", ".env", ".bat"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"}
DOC_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".odt", ".ods"}
TEXT_PREVIEW_MAX_BYTES = 512 * 1024
RETENTION_OPTIONS = [
    (3600, "1 小时"),
    (43200, "12 小时"),
    (86400, "24 小时"),
    (259200, "3 天"),
    (604800, "7 天"),
]
RETENTION_OPTIONS_SET = {v for v, _ in RETENTION_OPTIONS}
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


def get_disk_stats() -> dict[str, object]:
    ensure_directories()
    usage = shutil.disk_usage(DOWNLOADS_DIR)
    used = get_downloads_usage()
    disk_pct = (usage.used / usage.total * 100) if usage.total else 0
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "downloads_used": used,
        "total_human": format_size(usage.total),
        "used_human": format_size(usage.used),
        "free_human": format_size(usage.free),
        "downloads_used_human": format_size(used),
        "disk_percent": round(disk_pct, 1),
        "disk_danger": usage.free < MIN_FREE_BYTES,
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
                entry: dict[str, float] = {"created_at": float(item["created_at"])}
                if "retention_seconds" in item:
                    entry["retention_seconds"] = float(item["retention_seconds"])
                result[name] = entry
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
        entry = meta[name]
        created_at = entry["created_at"]
        ret_secs = entry.get("retention_seconds", RETENTION_SECONDS)
        expires_at = created_at + ret_secs
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
                "retention_label": format_retention(ret_secs),
                "file_type": file_type(name),
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
    now = now_ts()
    removed: list[str] = []
    changed = False
    for name, item in list(meta.items()):
        path = DOWNLOADS_DIR / name
        if not path.exists():
            meta.pop(name, None)
            changed = True
            continue
        ret_secs = item.get("retention_seconds", RETENTION_SECONDS)
        created = item.get("created_at", now)
        if created + ret_secs <= now:
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


def renew_file(filename: str, password: str) -> None:
    if not check_admin_password(password):
        raise PermissionError("管理密码错误")
    meta = load_meta()
    if filename not in meta:
        raise FileNotFoundError("文件不存在或已过期")
    path = DOWNLOADS_DIR / filename
    if not path.exists():
        meta.pop(filename, None)
        save_meta(meta)
        raise FileNotFoundError("文件不存在或已过期")
    meta[filename]["created_at"] = now_ts()
    save_meta(meta)
    append_log("renew.log", f"renewed name={filename}")


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
        raise ValueError("只允许 http:// 或 https:// 链接")
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


def ensure_can_store_new_file() -> None:
    stats = get_disk_stats()
    free = int(stats["free"])
    downloads_used = int(stats["downloads_used"])
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"系统剩余空间低于 {format_size(MIN_FREE_BYTES)}，已拒绝上传")
    if downloads_used >= MAX_DOWNLOAD_DIR_BYTES:
        raise RuntimeError(f"downloads 目录超过 {format_size(MAX_DOWNLOAD_DIR_BYTES)}，已拒绝上传")


def save_uploaded_file(filename: str, source_file, retention_seconds: int = 0) -> int:
    ensure_directories()
    ensure_can_store_new_file()
    name = validate_custom_filename(filename)
    target = (DOWNLOADS_DIR / name).resolve()
    target.relative_to(DOWNLOADS_DIR)
    if target.exists():
        raise FileExistsError("同名文件已存在，已拒绝覆盖")

    budget = MAX_DOWNLOAD_DIR_BYTES - get_downloads_usage()
    tmp_path = DOWNLOADS_DIR / f".upload-{secrets.token_hex(8)}.tmp"
    written = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > SINGLE_FILE_LIMIT_BYTES:
                    raise RuntimeError(f"上传文件超过单文件限制 {format_size(SINGLE_FILE_LIMIT_BYTES)}")
                if written > budget:
                    raise RuntimeError(f"downloads 目录将超过 {format_size(MAX_DOWNLOAD_DIR_BYTES)}，已拒绝上传")
                out.write(chunk)
        tmp_path.replace(target)
        meta = load_meta()
        entry: dict[str, float] = {"created_at": now_ts()}
        if retention_seconds and retention_seconds in RETENTION_OPTIONS_SET:
            entry["retention_seconds"] = float(retention_seconds)
        meta[name] = entry
        save_meta(meta)
        append_log("upload.log", f"uploaded name={name} size={written}")
        return written
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


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


def add_aria2_task(url: str, filename: str | None = None, retention_seconds: int = 0) -> str:
    validated_url = validate_task_url(url)
    ensure_can_add_task(validated_url)
    options = {"dir": str(DOWNLOADS_DIR)}
    out_name: str | None = None
    if filename:
        out_name = validate_custom_filename(filename)
        options["out"] = out_name
    result = aria2_rpc("aria2.addUri", [[validated_url], options])
    if not isinstance(result, str):
        raise RuntimeError("aria2 未返回 GID")
    if out_name and retention_seconds and retention_seconds in RETENTION_OPTIONS_SET:
        meta = load_meta()
        if out_name not in meta:
            meta[out_name] = {"created_at": now_ts(), "retention_seconds": float(retention_seconds)}
            save_meta(meta)
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


def file_kind(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return None


def file_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    if suffix in DOC_EXTENSIONS:
        return "document"
    return "other"


def format_retention(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:g}h"
    return f"{hours / 24:g}d"


def copy_file_range(path: Path, output, start: int, length: int, chunk_size: int = 1024 * 128) -> None:
    remaining = length
    with path.open("rb") as f:
        f.seek(start)
        try:
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


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


QR_JS = (Path(__file__).resolve().parent / "qr.js").read_text(encoding="utf-8") if (Path(__file__).resolve().parent / "qr.js").exists() else ""


def page(title: str, body: str) -> bytes:
    css = """
    :root { color-scheme: light; --primary: #6d5dfc; --primary-dark: #5144d8; --bg: #f6f7fb; --text: #1f2430; --muted: #6b7280; --line: #e5e7eb; --card-bg: #fff; --input-bg: #fff; --input-border: #e5e7eb; --file-bg: #fafaff; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    main { width: min(1100px, calc(100% - 28px)); margin: 0 auto; padding: 28px 0 48px; }
    header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; margin-bottom: 18px; }
    h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }
    p { margin: 6px 0; color: var(--muted); line-height: 1.55; }
    a { color: var(--primary-dark); text-decoration: none; }
    .actions, .row-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .button, button, input[type=submit] { border: 0; border-radius: 8px; background: var(--primary); color: #fff; padding: 9px 13px; font-weight: 650; cursor: pointer; line-height: 1.2; transition: background .15s, transform .1s; font-size: inherit; }
    .button:hover, button:hover, input[type=submit]:hover { background: var(--primary-dark); transform: translateY(-1px); }
    .button:active, button:active, input[type=submit]:active { transform: translateY(0); }
    .button.secondary, button.secondary { background: #eef0ff; color: var(--primary-dark); }
    .button.secondary:hover, button.secondary:hover { background: #e0e3ff; }
    button.danger { background: #ef4444; }
    button.danger:hover { background: #dc2626; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
    .card, section, details { background: var(--card-bg); border: 1px solid var(--line); border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04); transition: box-shadow .2s, transform .2s; }
    .card:hover { box-shadow: 0 4px 12px rgba(109, 93, 252, 0.10); transform: translateY(-2px); }
    .card strong { display: block; font-size: 22px; margin-top: 6px; color: var(--primary-dark); }
    .card .card-label { font-size: 13px; color: var(--muted); }
    section, details { margin-top: 14px; }
    summary { cursor: pointer; font-weight: 700; padding: 2px 0; user-select: none; transition: color .15s; }
    summary:hover { color: var(--primary); }
    details[open] summary { margin-bottom: 10px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }
    th { color: var(--muted); font-size: 13px; text-transform: none; }
    tr:hover td { background: rgba(109,93,252,0.04); }
    .muted { color: var(--muted); }
    .notice { background: var(--card-bg); border-left: 4px solid var(--primary); padding: 12px 14px; border-radius: 8px; margin-top: 14px; }
    .empty { padding: 24px 16px; color: var(--muted); text-align: center; }
    .progress { width: 110px; height: 8px; background: var(--line); border-radius: 999px; overflow: hidden; margin-top: 5px; }
    .bar { height: 100%; background: linear-gradient(90deg, #6d5dfc, #8b5cf6); transition: width .3s; }
    .viewer { background: #0f172a; border-radius: 8px; padding: 10px; }
    .viewer img, .viewer video { display: block; width: 100%; max-height: 72vh; object-fit: contain; border-radius: 6px; background: #0f172a; }
    form.inline { display: inline; }
    label { display: block; font-weight: 650; margin: 10px 0 5px; font-size: 14px; }
    input[type=text], input[type=password], input[type=url], select { width: 100%; border: 1px solid var(--input-border); border-radius: 8px; padding: 10px; font: inherit; background: var(--input-bg); color: var(--text); transition: border-color .15s, box-shadow .15s; }
    input[type=text]:focus, input[type=password]:focus, input[type=url]:focus, select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(109, 93, 252, 0.12); }
    input[type=file] { width: 100%; border: 2px dashed var(--line); border-radius: 8px; padding: 18px 12px; font: inherit; cursor: pointer; background: var(--file-bg); color: var(--text); transition: border-color .2s, background .2s; }
    input[type=file]:hover, input[type=file]:focus { border-color: var(--primary); background: #f0edff; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
    .search-box { width: 100%; border: 1px solid var(--input-border); border-radius: 8px; padding: 8px 12px; font: inherit; background: var(--input-bg); color: var(--text); margin-bottom: 10px; }
    .search-box:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(109, 93, 252, 0.12); }
    .theme-toggle { background: none; border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; cursor: pointer; font-size: 16px; line-height: 1; color: var(--text); transition: background .15s; }
    .theme-toggle:hover { background: var(--line); transform: none; }
    .form-submit { margin-top: 14px; }
    .code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
    .code-block { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 13px; line-height: 1.6; max-height: 70vh; white-space: pre-wrap; word-break: break-all; }
    .disk-bar-outer { width: 100%; height: 8px; background: var(--line); border-radius: 999px; overflow: hidden; margin-top: 8px; }
    .disk-bar-inner { height: 100%; border-radius: 999px; transition: width .3s; background: linear-gradient(90deg, #22c55e, #84cc16); }
    .disk-bar-inner.warn { background: linear-gradient(90deg, #f59e0b, #ef4444); }
    .disk-bar-inner.danger { background: #ef4444; }
    .drop-zone { border: 2px dashed var(--line); border-radius: 10px; padding: 32px 16px; text-align: center; color: var(--muted); font-size: 14px; cursor: pointer; transition: border-color .2s, background .2s, transform .15s; position: relative; }
    .drop-zone:hover, .drop-zone.drag-over { border-color: var(--primary); background: rgba(109,93,252,0.06); transform: scale(1.01); }
    .drop-zone.drag-over { border-style: solid; }
    .drop-zone input[type=file] { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
    .filter-bar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
    .filter-btn { background: #eef0ff; color: var(--primary-dark); border: 0; border-radius: 6px; padding: 6px 12px; font-size: 13px; font-weight: 600; cursor: pointer; transition: background .15s; }
    .filter-btn.active, .filter-btn:hover { background: var(--primary); color: #fff; }
    .tag { display: inline-block; background: #eef0ff; color: var(--primary-dark); padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; }
    .modal-overlay.active { display: flex; }
    .modal-content { background: var(--card-bg); border-radius: 12px; padding: 24px; max-width: 340px; width: 90%; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,0.2); color: var(--text); }
    .share-actions { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
    .modal-content h3 { margin: 0 0 4px; font-size: 16px; }
    .modal-content p { font-size: 13px; margin: 4px 0 12px; }
    .modal-content canvas { display: block; margin: 0 auto 12px; }
    .upload-progress { display: none; margin-top: 12px; }
    .upload-progress.active { display: block; }
    .upload-bar-outer { width: 100%; height: 10px; background: #eef0f6; border-radius: 999px; overflow: hidden; }
    .upload-bar-inner { height: 100%; width: 0%; background: linear-gradient(90deg, #6d5dfc, #8b5cf6); border-radius: 999px; transition: width .2s; }
    .upload-status { font-size: 13px; color: var(--muted); margin-top: 6px; }
    .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px); background: #1f2430; color: #fff; padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; opacity: 0; transition: opacity .25s, transform .25s; pointer-events: none; z-index: 999; }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
    html.dark { color-scheme: dark; --bg: #0f172a; --text: #e2e8f0; --muted: #94a3b8; --line: #1e293b; --primary: #818cf8; --primary-dark: #a5b4fc; --card-bg: #1e293b; --input-bg: #0f172a; --input-border: #475569; --file-bg: #1e293b; }
    html.dark .card:hover { box-shadow: 0 4px 12px rgba(129, 140, 248, 0.15); }
    html.dark .notice { background: #1e293b; border-color: var(--primary); }
    html.dark .button.secondary, html.dark button.secondary { background: #1e293b; color: var(--primary); }
    html.dark .button.secondary:hover, html.dark button.secondary:hover { background: #334155; }
    html.dark input[type=file]:hover, html.dark input[type=file]:focus { background: #334155; border-color: var(--primary); }
    html.dark tr:hover td { background: rgba(129,140,248,0.06); }
    html.dark .upload-bar-outer, html.dark .disk-bar-outer { background: #334155; }
    html.dark .filter-btn { background: #1e293b; color: var(--primary); }
    html.dark .tag { background: #1e293b; color: var(--primary); }
    html.dark .toast { background: #e2e8f0; color: #0f172a; }
    html.dark .code-block { background: #1a1a2e; }
    @media (prefers-color-scheme: dark) { html:not(.light) { color-scheme: dark; --bg: #0f172a; --text: #e2e8f0; --muted: #94a3b8; --line: #1e293b; --primary: #818cf8; --primary-dark: #a5b4fc; --card-bg: #1e293b; --input-bg: #0f172a; --input-border: #475569; --file-bg: #1e293b; } }
    @media (prefers-color-scheme: dark) {
      html:not(.light) .card:hover { box-shadow: 0 4px 12px rgba(129, 140, 248, 0.15); }
      html:not(.light) .notice { background: #1e293b; border-color: var(--primary); }
      html:not(.light) .button.secondary, html:not(.light) button.secondary { background: #1e293b; color: var(--primary); }
      html:not(.light) .button.secondary:hover, html:not(.light) button.secondary:hover { background: #334155; }
      html:not(.light) input[type=file]:hover, html:not(.light) input[type=file]:focus { background: #334155; border-color: var(--primary); }
      html:not(.light) tr:hover td { background: rgba(129,140,248,0.06); }
      html:not(.light) .upload-bar-outer, html:not(.light) .disk-bar-outer { background: #334155; }
      html:not(.light) .filter-btn { background: #1e293b; color: var(--primary); }
      html:not(.light) .tag { background: #1e293b; color: var(--primary); }
      html:not(.light) .toast { background: #e2e8f0; color: #0f172a; }
      html:not(.light) .code-block { background: #1a1a2e; }
    }
    @media (max-width: 720px) { header, .form-grid { display: block; } .actions { margin-top: 12px; } th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) { display: none; } }
    """
    script = """
    function showToast(msg) {
      var el = document.getElementById('toast');
      if (!el) { var d = document.createElement('div'); d.id='toast'; d.className='toast'; document.body.appendChild(d); el=d; }
      el.textContent = msg;
      el.classList.add('show');
      clearTimeout(el._t);
      el._t = setTimeout(function(){ el.classList.remove('show'); }, 2000);
    }
    function copyLink(path) {
      var url = new URL(path, window.location.href).href;
      navigator.clipboard.writeText(url).then(function(){ showToast('\u94fe\u63a5\u5df2\u590d\u5236'); });
    }
    async function handleUpload(form) {
      var bar = document.getElementById('upload-bar');
      var status = document.getElementById('upload-status');
      var outer = document.getElementById('upload-progress');
      var btn = form.querySelector('input[type=submit]');
      var fileInput = form.querySelector('[name=file]');
      var file = fileInput.files[0];
      if (!file) return false;
      var password = form.querySelector('[name=password]').value;
      var customName = (form.querySelector('[name=filename]') || {}).value || '';
      var retention = (form.querySelector('[name=retention]') || {}).value || '0';
      outer.classList.add('active');
      btn.disabled = true;
      btn.value = '\u4e0a\u4f20\u4e2d...';
      
      var chunkSize = 5 * 1024 * 1024;
      var totalChunks = Math.ceil(file.size / chunkSize);
      var uploadId = Math.random().toString(36).substring(2) + Date.now().toString(36);
      var uploadedBytes = 0;
      
      for (var i = 0; i < totalChunks; i++) {
        var start = i * chunkSize;
        var end = Math.min(start + chunkSize, file.size);
        var chunk = file.slice(start, end);
        try {
          await new Promise(function(resolve, reject) {
            var xhr = new XMLHttpRequest();
            xhr.upload.onprogress = function(e) {
              if (e.lengthComputable) {
                var currentLoaded = uploadedBytes + e.loaded;
                var pct = Math.round(currentLoaded / file.size * 100);
                bar.style.width = pct + '%';
                var loadedMB = (currentLoaded / 1048576).toFixed(1);
                var totalMB = (file.size / 1048576).toFixed(1);
                status.textContent = pct + '% \u00b7 ' + loadedMB + ' / ' + totalMB + ' MB (' + (i+1) + '/' + totalChunks + ')';
              }
            };
            xhr.onload = function() {
              if (xhr.status === 200) { resolve(); }
              else { reject(xhr); }
            };
            xhr.onerror = function() { reject(xhr); };
            xhr.open('POST', '/api/upload_chunk');
            xhr.setRequestHeader('X-Upload-Id', uploadId);
            xhr.setRequestHeader('X-Chunk-Index', i);
            xhr.setRequestHeader('X-Total-Chunks', totalChunks);
            xhr.setRequestHeader('X-Password', encodeURIComponent(password));
            if (i === totalChunks - 1) {
              xhr.setRequestHeader('X-Filename', encodeURIComponent(customName));
              xhr.setRequestHeader('X-Retention', retention);
              xhr.setRequestHeader('X-Orig-Filename', encodeURIComponent(file.name));
            }
            xhr.send(chunk);
          });
          uploadedBytes += chunk.size;
        } catch (xhr) {
          var msg = '\u4e0a\u4f20\u5931\u8d25';
          try {
            var m = xhr.responseText && xhr.responseText.match(/<p>([^<]+)<\\/p>/);
            if (m) msg = m[1];
          } catch(e) {}
          status.textContent = msg;
          btn.disabled = false;
          btn.value = '\u91cd\u8bd5\u4e0a\u4f20';
          return false;
        }
      }
      bar.style.width = '100%';
      status.textContent = '\u4e0a\u4f20\u5b8c\u6210\uff01\u6b63\u5728\u8df3\u8f6c...';
      setTimeout(function(){ window.location.href = '/downloads/'; }, 800);
      return false;
    }
    (function() {
      var zone = document.getElementById('drop-zone');
      var input = document.getElementById('upload-file');
      var nameEl = document.getElementById('drop-file-name');
      if (!zone || !input) return;
      input.addEventListener('change', function() {
        if (input.files.length) nameEl.textContent = '\u5df2\u9009\u62e9: ' + input.files[0].name;
        else nameEl.textContent = '';
      });
      zone.addEventListener('dragover', function(e) { e.preventDefault(); zone.classList.add('drag-over'); });
      zone.addEventListener('dragenter', function(e) { e.preventDefault(); zone.classList.add('drag-over'); });
      zone.addEventListener('dragleave', function() { zone.classList.remove('drag-over'); });
      zone.addEventListener('drop', function(e) {
        e.preventDefault();
        zone.classList.remove('drag-over');
        if (e.dataTransfer.files.length) {
          input.files = e.dataTransfer.files;
          nameEl.textContent = '\u5df2\u9009\u62e9: ' + e.dataTransfer.files[0].name;
        }
      });
      document.addEventListener('dragover', function(e) {
        e.preventDefault();
        var det = zone.closest('details');
        if (det && !det.open) det.open = true;
      });
    })();
    var _curFilter = 'all';
    var _curSearch = '';
    function _applyFilters() {
      var rows = document.querySelectorAll('tr[data-type]');
      var q = _curSearch.toLowerCase();
      for (var i = 0; i < rows.length; i++) {
        var matchType = _curFilter === 'all' || rows[i].getAttribute('data-type') === _curFilter;
        var matchSearch = !q || rows[i].querySelector('td').textContent.toLowerCase().indexOf(q) >= 0;
        rows[i].style.display = matchType && matchSearch ? '' : 'none';
      }
    }
    function filterFiles(type) {
      _curFilter = type;
      var btns = document.querySelectorAll('.filter-btn');
      for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
      if (event && event.target) event.target.classList.add('active');
      _applyFilters();
    }
    function searchFiles(q) {
      _curSearch = q;
      _applyFilters();
    }
    function toggleTheme() {
      var h = document.documentElement;
      var isDark = h.classList.contains('dark') || (!h.classList.contains('light') && window.matchMedia('(prefers-color-scheme: dark)').matches);
      if (isDark) {
        h.classList.remove('dark');
        h.classList.add('light');
        localStorage.setItem('theme', 'light');
      } else {
        h.classList.remove('light');
        h.classList.add('dark');
        localStorage.setItem('theme', 'dark');
      }
    }
    function showShare(path, name) {
      var url = new URL(path, window.location.href).href;
      var overlay = document.getElementById('share-modal');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'share-modal';
        overlay.className = 'modal-overlay';
        overlay.innerHTML = '<div class="modal-content"><h3 id="share-name"></h3><p id="share-url" class="code muted"></p><canvas id="share-canvas"></canvas><div class="share-actions"><button onclick="shareModalCopy()">\u590d\u5236\u94fe\u63a5</button><button class="secondary" onclick="closeShare()">\u5173\u95ed</button></div></div>';
        overlay.addEventListener('click', function(e) { if (e.target === overlay) closeShare(); });
        document.body.appendChild(overlay);
      }
      document.getElementById('share-name').textContent = name;
      document.getElementById('share-url').textContent = url;
      var canvas = document.getElementById('share-canvas');
      if (typeof generateQR === 'function') {
        var matrix = generateQR(url);
        renderQR(canvas, matrix, 4);
      } else {
        canvas.width = 200; canvas.height = 40;
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#666'; ctx.font = '13px sans-serif';
        ctx.fillText('QR loading...', 10, 25);
      }
      overlay.classList.add('active');
    }
    function shareModalCopy() {
      var urlEl = document.getElementById('share-url');
      if (urlEl) navigator.clipboard.writeText(urlEl.textContent).then(function(){ showToast('\u94fe\u63a5\u5df2\u590d\u5236'); });
    }
    function closeShare() {
      var m = document.getElementById('share-modal');
      if (m) m.classList.remove('active');
    }
    """
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<script>(function(){{var t=localStorage.getItem('theme');if(t==='dark')document.documentElement.classList.add('dark');else if(t==='light')document.documentElement.classList.add('light');}})()</script>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{css}</style>
</head>
<body>
  <main>{body}</main>
  <script>{script}</script>
  <script>{QR_JS}</script>
</body>
</html>"""
    return html_doc.encode("utf-8")


def retention_select_html(field_id: str) -> str:
    options = []
    for val, label in RETENTION_OPTIONS:
        sel = ' selected' if val == RETENTION_SECONDS else ''
        options.append(f'<option value="{val}"{sel}>{label}</option>')
    return (
        f'<label for="{field_id}">保留时间</label>'
        f'<select id="{field_id}" name="retention">'
        + ''.join(options) +
        '</select>'
    )


def render_file_rows(files: list[dict[str, object]], compact: bool = False) -> str:
    if not files:
        return '<div class="empty">暂无可下载文件</div>'
    filter_bar = (
        '<input class="search-box" type="text" placeholder="\u641c\u7d22\u6587\u4ef6\u540d..." oninput="searchFiles(this.value)">'
        '<div class="filter-bar">'
        '<button class="filter-btn active" type="button" onclick="filterFiles(\'all\')">\u5168\u90e8</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'image\')">\u56fe\u7247</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'video\')">\u89c6\u9891</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'text\')">\u6587\u672c</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'document\')">\u6587\u6863</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'archive\')">\u538b\u7f29\u5305</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'other\')">\u5176\u4ed6</button>'
        '</div>'
    )
    rows = []
    for item in files:
        name = str(item["name"])
        url_name = str(item["url_name"])
        ft = str(item.get("file_type", "other"))
        kind = file_kind(name)
        preview = f'<a class="button secondary" href="/view/{url_name}">预览</a> ' if kind else ""
        share_btn = f'<button class="secondary" type="button" onclick="showShare(\'/file/{url_name}\',\'{html.escape(name, quote=True)}\')">分享</button>'
        ret_label = html.escape(str(item.get("retention_label", "")))
        if compact:
            actions = f'{preview}<a class="button secondary" href="/file/{url_name}">下载</a> {share_btn}'
        else:
            renew_btn = (
                f'<form class="inline" method="post" action="/api/renew">'
                f'<input type="hidden" name="filename" value="{html.escape(name, quote=True)}">'
                f'<input type="password" name="password" placeholder="\u5bc6\u7801" style="width:70px;padding:5px 6px;font-size:12px;" required>'
                f'<button class="secondary" type="submit" style="font-size:12px;padding:5px 8px;">\u7eed\u547d</button></form>'
            )
            actions = (
                preview +
                f'<a class="button secondary" href="/file/{url_name}">普通下载</a> '
                f'<a class="button" href="/once/{url_name}">一次性下载</a> '
                + share_btn + ' ' + renew_btn
            )
        rows.append(
            f'<tr data-type="{ft}">'
            f'<td class="code">{html.escape(name)}</td>'
            f"<td>{html.escape(str(item['size_human']))}</td>"
            f"<td>{html.escape(str(item['created_at_text']))}</td>"
            f"<td>{html.escape(str(item['expires_at_text']))}</td>"
            f"<td>{html.escape(str(item['remaining_text']))}</td>"
            f'<td><span class="tag">{ret_label}</span></td>'
            f'<td class="row-actions">{actions}</td>'
            "</tr>"
        )
    return (
        filter_bar +
        "<table><thead><tr><th>文件名</th><th>大小</th><th>入库时间</th><th>过期时间</th><th>剩余时间</th><th>保留</th><th>操作</th></tr></thead>"
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
    <button class="theme-toggle" type="button" onclick="toggleTheme()" title="切换主题">🌓</button>
  </div>
</header>
{message_html}
<div class="cards">
  <div class="card">下载目录占用<strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
  <div class="card">剩余磁盘空间<strong>{html.escape(str(stats["free_human"]))}</strong>
    <div class="disk-bar-outer"><div class="disk-bar-inner{' danger' if stats['disk_danger'] else ' warn' if stats['disk_percent'] > 80 else ''}" style="width:{stats['disk_percent']}%"></div></div>
  </div>
  <div class="card">文件数量<strong>{len(files)}</strong></div>
  <div class="card">默认保留<strong>{RETENTION_HOURS:g}h</strong></div>
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
        {retention_select_html('task-retention')}
      </div>
      <div>
        <label for="password">管理密码</label>
        <input id="password" name="password" type="password" required>
      </div>
    </div>
    <p class="muted">文件名只允许中文、英文、数字、空格、点、下划线和短横线。保留时间仅在指定文件名时生效。</p>
    <input type="submit" value="添加任务">
  </form>
</details>
<details>
  <summary>上传本地文件</summary>
  <p>管理密码用于防止他人滥用上传功能，普通下载不需要密码。</p>
  <form id="upload-form" method="post" action="/api/upload" enctype="multipart/form-data" onsubmit="return handleUpload(this)">
    <div id="drop-zone" class="drop-zone">
      <p>📁 拖拽文件到这里，或点击选择文件</p>
      <input id="upload-file" name="file" type="file" required>
    </div>
    <div id="drop-file-name" class="muted" style="margin:6px 0;font-size:13px;"></div>
    <div class="form-grid">
      <div>
        <label for="upload-filename">自定义文件名（可选）</label>
        <input id="upload-filename" name="filename" type="text" maxlength="180">
      </div>
      <div>
        {retention_select_html('upload-retention')}
      </div>
      <div>
        <label for="upload-password">管理密码</label>
        <input id="upload-password" name="password" type="password" required>
      </div>
    </div>
    <p class="muted">文件名只允许中文、英文、数字、空格、点、下划线和短横线。单文件上限 {format_size(SINGLE_FILE_LIMIT_BYTES)}。</p>
    <div class="form-submit"><input type="submit" value="上传"></div>
    <div id="upload-progress" class="upload-progress">
      <div class="upload-bar-outer"><div id="upload-bar" class="upload-bar-inner"></div></div>
      <div id="upload-status" class="upload-status"></div>
    </div>
  </form>
</details>
<div class="notice">
  <p>文件按设定时间自动删除（可选1h/12h/24h/3d/7d）；一次性下载完整成功后立即删除。</p>
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
    <button class="theme-toggle" type="button" onclick="toggleTheme()" title="切换主题">🌓</button>
  </div>
</header>
<div class="cards">
  <div class="card">下载目录占用<strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
  <div class="card">剩余磁盘空间<strong>{html.escape(str(stats["free_human"]))}</strong>
    <div class="disk-bar-outer"><div class="disk-bar-inner{' danger' if stats['disk_danger'] else ' warn' if stats['disk_percent'] > 80 else ''}" style="width:{stats['disk_percent']}%"></div></div>
  </div>
  <div class="card">文件数量<strong>{len(files)}</strong></div>
</div>
<section>
  <h2>下载目录</h2>
  {render_file_rows(files)}
</section>
"""
    return page("下载目录", body)


def render_view(path: Path) -> bytes:
    kind = file_kind(path.name)
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
    elif kind == "text":
        try:
            raw = path.read_bytes()[:TEXT_PREVIEW_MAX_BYTES]
            text_content = raw.decode("utf-8", errors="replace")
            truncated = ' <span class="muted">(文件过大，仅显示前 512KB)</span>' if path.stat().st_size > TEXT_PREVIEW_MAX_BYTES else ""
        except Exception:
            text_content = "无法读取文件内容"
            truncated = ""
        viewer = f'<section><p>文本预览{truncated}</p><pre class="code-block"><code>{html.escape(text_content)}</code></pre></section>'
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
    <button class="theme-toggle" type="button" onclick="toggleTheme()" title="切换主题">🌓</button>
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
    protocol_version = "HTTP/1.1"
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
        if raw_path == "/api/upload":
            self.handle_upload()
            return
        if raw_path == "/api/upload_chunk":
            self.handle_upload_chunk()
            return
        form = parse_form(self)
        if raw_path == "/api/add-task":
            self.handle_add_task(form)
        elif raw_path == "/api/remove-task":
            self.handle_remove_task(form)
        elif raw_path == "/api/clear-stopped":
            self.handle_clear_stopped(form)
        elif raw_path == "/api/renew":
            self.handle_renew(form)
        else:
            self.send_error_page(404, "接口不存在")

    def handle_add_task(self, form: dict[str, str]) -> None:
        if not check_admin_password(form.get("password", "")):
            self.send_error_page(403, "管理密码错误")
            return
        try:
            filename = form.get("filename", "").strip() or None
            retention = int(form.get("retention", "0") or "0")
            gid = add_aria2_task(form.get("url", ""), filename, retention)
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

    def handle_renew(self, form: dict[str, str]) -> None:
        filename = form.get("filename", "").strip()
        password = form.get("password", "")
        try:
            renew_file(filename, password)
        except PermissionError:
            self.send_error_page(403, "管理密码错误")
            return
        except FileNotFoundError as exc:
            self.send_error_page(404, str(exc))
            return
        except Exception as exc:
            self.send_error_page(400, f"续命失败：{exc}")
            return
        self.send_bytes(200, success_page("续命成功", f"{filename} 的保留时间已重置。"))

    def handle_upload(self) -> None:
        """Streaming multipart upload — never buffers the full file in memory."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_error_page(400, "请使用 multipart/form-data 表单上传")
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self.send_error_page(400, "缺少 Content-Length")
            return
        bm = re.search(r'boundary=([^\s;]+)', content_type)
        if not bm:
            self.send_error_page(400, "缺少 boundary")
            return
        boundary = bm.group(1).encode("ascii")
        delim = b"--" + boundary

        consumed = 0

        def _readline():
            nonlocal consumed
            line = self.rfile.readline(65536)
            consumed += len(line)
            return line

        def _read(n):
            nonlocal consumed
            data = self.rfile.read(n)
            consumed += len(data)
            return data

        def _drain():
            nonlocal consumed
            left = content_length - consumed
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                consumed += len(chunk)
                left -= len(chunk)

        fields: dict[str, str] = {}
        file_orig_name = ""
        tmp_path: Path | None = None
        written = 0

        try:
            # Read initial boundary line: --boundary\r\n
            _readline()

            while consumed < content_length:
                # ---- Read part headers until blank line ----
                headers_raw = b""
                while True:
                    line = _readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    headers_raw += line
                if not headers_raw:
                    break

                header_text = headers_raw.decode("utf-8", errors="replace")
                name_match = re.search(r'name="([^"]+)"', header_text)
                if not name_match:
                    break
                fname_match = re.search(r'filename="([^"]*)"', header_text)

                if fname_match and fname_match.group(1):
                    # ---- FILE PART: stream body to disk ----
                    file_orig_name = fname_match.group(1)
                    file_remaining = content_length - consumed
                    closing = b"\r\n" + delim + b"--"
                    tail_reserve = len(closing) + 4

                    tmp_path = DOWNLOADS_DIR / f".upload-{secrets.token_hex(8)}.tmp"
                    tail_buf = b""
                    with tmp_path.open("wb") as out:
                        while file_remaining > 0:
                            to_read = min(1048576, file_remaining)
                            chunk = _read(to_read)
                            if not chunk:
                                break
                            file_remaining = content_length - consumed
                            data = tail_buf + chunk
                            if file_remaining > 0:
                                if len(data) > tail_reserve:
                                    to_write = data[:-tail_reserve]
                                    out.write(to_write)
                                    written += len(to_write)
                                    tail_buf = data[-tail_reserve:]
                                else:
                                    tail_buf = data
                            else:
                                idx = data.rfind(b"\r\n" + delim)
                                if idx >= 0:
                                    data = data[:idx]
                                out.write(data)
                                written += len(data)
                    
                    if file_remaining > 0:
                        raise RuntimeError("网络连接异常中断，文件接收不完整")
                        
                    break  # file part is last (JS sends it last)

                else:
                    # ---- TEXT FIELD: read until next boundary line ----
                    field_lines: list[bytes] = []
                    while consumed < content_length:
                        line = _readline()
                        stripped = line.rstrip(b"\r\n")
                        if stripped == delim or stripped == delim + b"--":
                            break
                        field_lines.append(line)
                    val = b"".join(field_lines)
                    if val.endswith(b"\r\n"):
                        val = val[:-2]
                    fields[name_match.group(1)] = val.decode("utf-8", errors="replace")

            # ---- Validate & finalise ----
            password = fields.get("password", "")
            if not check_admin_password(password):
                self.send_error_page(403, "管理密码错误")
                return
            if tmp_path is None or not file_orig_name:
                self.send_error_page(400, "请选择要上传的文件")
                return

            custom_name = fields.get("filename", "").strip()
            filename = custom_name if custom_name else file_orig_name
            retention = int(fields.get("retention", "0") or "0")

            name = validate_custom_filename(filename)
            target = (DOWNLOADS_DIR / name).resolve()
            target.relative_to(DOWNLOADS_DIR)
            if target.exists():
                raise FileExistsError("同名文件已存在，已拒绝覆盖")

            ensure_can_store_new_file()
            budget = MAX_DOWNLOAD_DIR_BYTES - get_downloads_usage()
            if written > SINGLE_FILE_LIMIT_BYTES:
                raise RuntimeError(f"上传文件超过单文件限制 {format_size(SINGLE_FILE_LIMIT_BYTES)}")
            if written > budget:
                raise RuntimeError(f"downloads 目录将超过 {format_size(MAX_DOWNLOAD_DIR_BYTES)}")

            tmp_path.replace(target)
            tmp_path = None  # prevent cleanup

            meta = load_meta()
            entry: dict[str, float] = {"created_at": now_ts()}
            if retention and retention in RETENTION_OPTIONS_SET:
                entry["retention_seconds"] = float(retention)
            meta[name] = entry
            save_meta(meta)
            append_log("upload.log", f"uploaded name={name} size={written}")
            self.send_bytes(200, success_page("上传成功", f"文件 {name}（{format_size(written)}）已保存"))

        except FileExistsError as exc:
            self.send_error_page(409, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.send_error_page(400, f"上传失败：{exc}")
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_error_page(500, "上传处理异常")
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def handle_upload_chunk(self) -> None:
        try:
            upload_id = self.headers.get("X-Upload-Id", "")
            chunk_index = int(self.headers.get("X-Chunk-Index", "-1"))
            total_chunks = int(self.headers.get("X-Total-Chunks", "-1"))
            password = urllib.parse.unquote(self.headers.get("X-Password", ""))
            
            if not check_admin_password(password):
                self.send_error_page(403, "管理密码错误")
                return
                
            if not upload_id.isalnum() or chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
                self.send_error_page(400, "分片参数错误")
                return
                
            ensure_can_store_new_file()
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 10 * 1024 * 1024:
                self.send_error_page(413, "单分片过大")
                return
                
            tmp_path = DOWNLOADS_DIR / f".upload-{upload_id}.tmp"
            
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                self.send_error_page(400, "网络中断导致数据不完整")
                return
                
            with tmp_path.open("ab") as out:
                out.write(body)
                
            if tmp_path.stat().st_size > SINGLE_FILE_LIMIT_BYTES:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                self.send_error_page(400, f"文件超过上限 {format_size(SINGLE_FILE_LIMIT_BYTES)}")
                return
                
            if chunk_index == total_chunks - 1:
                orig_name = urllib.parse.unquote(self.headers.get("X-Orig-Filename", ""))
                custom_name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                retention = int(self.headers.get("X-Retention", "0"))
                
                filename = custom_name if custom_name else orig_name
                if not filename:
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                    self.send_error_page(400, "缺少文件名")
                    return
                    
                name = validate_custom_filename(filename)
                target = (DOWNLOADS_DIR / name).resolve()
                
                if target.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                    raise FileExistsError("同名文件已存在，已拒绝覆盖")
                    
                budget = MAX_DOWNLOAD_DIR_BYTES - get_downloads_usage()
                if tmp_path.stat().st_size > budget:
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                    raise RuntimeError("磁盘空间不足")
                    
                tmp_path.replace(target)
                
                meta = load_meta()
                entry: dict[str, float] = {"created_at": now_ts()}
                if retention and retention in RETENTION_OPTIONS_SET:
                    entry["retention_seconds"] = float(retention)
                meta[name] = entry
                save_meta(meta)
                append_log("upload.log", f"uploaded name={name} size={target.stat().st_size}")
                
            self.send_bytes(200, b"OK")
            
        except FileExistsError as exc:
            self.send_error_page(409, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.send_error_page(400, f"上传失败：{exc}")
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_error_page(500, "上传分片处理异常")

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
        if not file_kind(path.name):
            self.send_error_page(415, "这个文件类型不支持在线预览")
            return

        stat = path.stat()
        kind = file_kind(path.name)
        if kind == "text":
            mime = "text/plain; charset=utf-8"
        else:
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
