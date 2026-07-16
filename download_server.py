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
import threading

APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent)).resolve()
META_LOCK = threading.RLock()
DOWNLOADS_DIR = (APP_ROOT / "downloads").resolve()
LOGS_DIR = (APP_ROOT / "logs").resolve()
DATA_DIR = (APP_ROOT / "data").resolve()
META_PATH = DATA_DIR / "filemeta.json"
UPLOADS_DIR = (DATA_DIR / "uploads").resolve()
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
UPLOAD_CHUNK_BYTES = int(os.environ.get("UPLOAD_CHUNK_BYTES", str(5 * 1024**2)))
UPLOAD_CONCURRENCY = int(os.environ.get("UPLOAD_CONCURRENCY", "3"))
UPLOAD_SESSION_TTL_SECONDS = int(os.environ.get("UPLOAD_SESSION_TTL_SECONDS", "21600"))
UPLOAD_FALLBACK_MAX_BYTES = int(
    os.environ.get("UPLOAD_FALLBACK_MAX_BYTES", str(50 * 1024**2))
)
UPLOAD_LOCKS: dict[str, threading.RLock] = {}
UPLOAD_LOCKS_GUARD = threading.Lock()
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
    for directory in (DOWNLOADS_DIR, LOGS_DIR, DATA_DIR, UPLOADS_DIR):
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


def get_upload_tmp_usage() -> int:
    total = 0
    if not DOWNLOADS_DIR.exists():
        return 0
    for path in DOWNLOADS_DIR.glob(".upload-*.tmp"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def get_active_upload_reserved_bytes() -> int:
    total = 0
    if not UPLOADS_DIR.exists():
        return 0
    for path in UPLOADS_DIR.glob("*.json"):
        try:
            session = load_upload_session(path.stem)
            tmp_path = upload_tmp_path(path.stem)
            current_size = tmp_path.stat().st_size if tmp_path.exists() else 0
            total += max(0, int(session["size"]) - current_size)
        except (OSError, TypeError, ValueError):
            continue
    return total


def ensure_upload_capacity(size: int) -> None:
    ensure_can_store_new_file()
    reserved = get_active_upload_reserved_bytes()
    projected = get_downloads_usage() + get_upload_tmp_usage() + reserved + size
    if projected > MAX_DOWNLOAD_DIR_BYTES:
        raise RuntimeError(f"downloads 目录将超过 {format_size(MAX_DOWNLOAD_DIR_BYTES)}")
    free_after_reservations = shutil.disk_usage(DOWNLOADS_DIR).free - MIN_FREE_BYTES - reserved
    if size > free_after_reservations:
        raise RuntimeError("磁盘剩余空间不足")


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


def validate_upload_id(upload_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", upload_id or ""):
        raise ValueError("上传任务 ID 无效")
    return upload_id


def upload_session_path(upload_id: str) -> Path:
    return UPLOADS_DIR / f"{validate_upload_id(upload_id)}.json"


def upload_tmp_path(upload_id: str) -> Path:
    return DOWNLOADS_DIR / f".upload-{validate_upload_id(upload_id)}.tmp"


def get_upload_lock(upload_id: str) -> threading.RLock:
    upload_id = validate_upload_id(upload_id)
    with UPLOAD_LOCKS_GUARD:
        return UPLOAD_LOCKS.setdefault(upload_id, threading.RLock())


def load_upload_session(upload_id: str) -> dict[str, object]:
    path = upload_session_path(upload_id)
    if not path.exists():
        raise FileNotFoundError("上传任务不存在")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("上传任务状态损坏") from exc
    required = {
        "upload_id",
        "upload_token",
        "filename",
        "size",
        "chunk_size",
        "total_chunks",
        "retention_seconds",
        "created_at",
        "updated_at",
        "received_chunks",
        "received_bytes",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        raise ValueError("上传任务状态损坏")
    integer_fields = ("size", "chunk_size", "total_chunks", "retention_seconds", "received_bytes")
    if any(type(data[field]) is not int for field in integer_fields):
        raise ValueError("上传任务状态损坏")
    if any(
        not isinstance(data[field], (int, float)) or isinstance(data[field], bool)
        for field in ("created_at", "updated_at")
    ):
        raise ValueError("上传任务状态损坏")
    if (
        data["upload_id"] != upload_id
        or not isinstance(data["upload_token"], str)
        or not data["upload_token"]
        or not isinstance(data["filename"], str)
        or not isinstance(data["received_chunks"], list)
    ):
        raise ValueError("上传任务状态损坏")
    validate_custom_filename(data["filename"])
    size = data["size"]
    chunk_size = data["chunk_size"]
    total_chunks = data["total_chunks"]
    if size <= 0 or chunk_size <= 0 or total_chunks != (size + chunk_size - 1) // chunk_size:
        raise ValueError("上传任务状态损坏")
    received = data["received_chunks"]
    if any(type(index) is not int or index < 0 or index >= total_chunks for index in received):
        raise ValueError("上传任务状态损坏")
    if len(received) != len(set(received)):
        raise ValueError("上传任务状态损坏")
    expected_received_bytes = sum(
        min(chunk_size, size - index * chunk_size) for index in received
    )
    if data["received_bytes"] != expected_received_bytes:
        raise ValueError("上传任务状态损坏")
    return data


def save_upload_session(session: dict[str, object]) -> None:
    ensure_directories()
    path = upload_session_path(str(session["upload_id"]))
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(path)
    chmod_600(path)


def remove_upload_session(upload_id: str, remove_tmp: bool = True) -> None:
    with contextlib.suppress(FileNotFoundError):
        upload_session_path(upload_id).unlink()
    if remove_tmp:
        with contextlib.suppress(FileNotFoundError):
            upload_tmp_path(upload_id).unlink()


def cleanup_upload_sessions(now: float | None = None) -> list[str]:
    ensure_directories()
    current = now_ts() if now is None else now
    removed: list[str] = []
    for path in UPLOADS_DIR.glob("*.json"):
        upload_id = path.stem
        try:
            validate_upload_id(upload_id)
            lock = get_upload_lock(upload_id)
            with lock:
                session = load_upload_session(upload_id)
                if current - float(session["updated_at"]) <= UPLOAD_SESSION_TTL_SECONDS:
                    continue
                remove_upload_session(upload_id)
            with UPLOAD_LOCKS_GUARD:
                UPLOAD_LOCKS.pop(upload_id, None)
            removed.append(upload_id)
            append_log("upload.log", f"event=cleanup upload_id={upload_id} reason=expired")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
    return removed


def create_upload_session(
    filename: str,
    size: int,
    chunk_size: int,
    retention_seconds: int,
) -> dict[str, object]:
    ensure_directories()
    upload_id = secrets.token_urlsafe(18)
    now = now_ts()
    session: dict[str, object] = {
        "upload_id": upload_id,
        "upload_token": secrets.token_urlsafe(24),
        "filename": filename,
        "size": size,
        "chunk_size": chunk_size,
        "total_chunks": (size + chunk_size - 1) // chunk_size,
        "retention_seconds": retention_seconds,
        "created_at": now,
        "updated_at": now,
        "received_chunks": [],
        "received_bytes": 0,
    }
    tmp_path = upload_tmp_path(upload_id)
    tmp_path.touch(exist_ok=False)
    try:
        save_upload_session(session)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return session


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
    with META_LOCK:
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
    now = now_ts()
    removed: list[str] = []
    cleanup_upload_sessions(now)
    with META_LOCK:
        meta = load_meta()
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
        for f in DOWNLOADS_DIR.glob(".upload-*.tmp.part*"):
            try:
                if now - f.stat().st_mtime > UPLOAD_SESSION_TTL_SECONDS:
                    f.unlink()
            except OSError:
                pass
        if changed:
            save_meta(meta)
    return removed


def renew_file(filename: str, password: str) -> None:
    if not check_admin_password(password):
        raise PermissionError("管理密码错误")
    with META_LOCK:
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
        with META_LOCK:
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
        with META_LOCK:
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
            with META_LOCK:
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
    :root { color-scheme: light; --primary: #1769aa; --primary-dark: #0f548c; --bg: #f3f5f7; --text: #18212b; --muted: #64707d; --line: #dce1e6; --card-bg: #fff; --input-bg: #fff; --input-border: #cbd3db; --file-bg: #f8fafb; }
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
    .button.secondary, button.secondary { background: #e8eef3; color: var(--primary-dark); }
    .button.secondary:hover, button.secondary:hover { background: #d9e3eb; }
    button.danger { background: #ef4444; }
    button.danger:hover { background: #dc2626; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
    .card, section, details { background: var(--card-bg); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04); transition: box-shadow .2s; }
    .card:hover { box-shadow: 0 4px 12px rgba(23, 105, 170, 0.10); }
    .card strong { display: block; font-size: 22px; margin-top: 6px; color: var(--primary-dark); }
    .card .card-label { font-size: 13px; color: var(--muted); }
    section, details { margin-top: 14px; }
    summary { cursor: pointer; font-weight: 700; padding: 2px 0; user-select: none; transition: color .15s; }
    summary:hover { color: var(--primary); }
    details[open] summary { margin-bottom: 10px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }
    th { color: var(--muted); font-size: 13px; text-transform: none; }
    tr:hover td { background: rgba(23,105,170,0.04); }
    .muted { color: var(--muted); }
    .notice { background: var(--card-bg); border-left: 4px solid var(--primary); padding: 12px 14px; border-radius: 8px; margin-top: 14px; }
    .empty { padding: 24px 16px; color: var(--muted); text-align: center; }
    .progress { width: 110px; height: 8px; background: var(--line); border-radius: 999px; overflow: hidden; margin-top: 5px; }
    .bar { height: 100%; background: var(--primary); transition: width .3s; }
    .viewer { background: #0f172a; border-radius: 8px; padding: 10px; }
    .viewer img, .viewer video { display: block; width: 100%; max-height: 72vh; object-fit: contain; border-radius: 6px; background: #0f172a; }
    form.inline { display: inline; }
    label { display: block; font-weight: 650; margin: 10px 0 5px; font-size: 14px; }
    input[type=text], input[type=password], input[type=url], select { width: 100%; border: 1px solid var(--input-border); border-radius: 8px; padding: 10px; font: inherit; background: var(--input-bg); color: var(--text); transition: border-color .15s, box-shadow .15s; }
    input[type=text]:focus, input[type=password]:focus, input[type=url]:focus, select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(23, 105, 170, 0.14); }
    input[type=file] { width: 100%; border: 2px dashed var(--line); border-radius: 8px; padding: 18px 12px; font: inherit; cursor: pointer; background: var(--file-bg); color: var(--text); transition: border-color .2s, background .2s; }
    input[type=file]:hover, input[type=file]:focus { border-color: var(--primary); background: #eef5fa; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
    .search-box { width: 100%; border: 1px solid var(--input-border); border-radius: 8px; padding: 8px 12px; font: inherit; background: var(--input-bg); color: var(--text); margin-bottom: 10px; }
    .search-box:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(23, 105, 170, 0.14); }
    .theme-toggle { background: none; border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; cursor: pointer; font-size: 16px; line-height: 1; color: var(--text); transition: background .15s; }
    .theme-toggle:hover { background: var(--line); transform: none; }
    .form-submit { margin-top: 14px; }
    .code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
    .code-block { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 13px; line-height: 1.6; max-height: 70vh; white-space: pre-wrap; word-break: break-all; }
    .disk-bar-outer { width: 100%; height: 8px; background: var(--line); border-radius: 999px; overflow: hidden; margin-top: 8px; }
    .disk-bar-inner { height: 100%; border-radius: 999px; transition: width .3s; background: linear-gradient(90deg, #22c55e, #84cc16); }
    .disk-bar-inner.warn { background: linear-gradient(90deg, #f59e0b, #ef4444); }
    .disk-bar-inner.danger { background: #ef4444; }
    .drop-zone { border: 2px dashed var(--line); border-radius: 8px; padding: 32px 16px; text-align: center; color: var(--muted); font-size: 14px; cursor: pointer; transition: border-color .2s, background .2s; position: relative; }
    .drop-zone:hover, .drop-zone.drag-over { border-color: var(--primary); background: rgba(23,105,170,0.06); }
    .drop-zone.drag-over { border-style: solid; }
    .drop-zone input[type=file] { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
    .filter-bar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
    .filter-btn { background: #e8eef3; color: var(--primary-dark); border: 0; border-radius: 6px; padding: 6px 12px; font-size: 13px; font-weight: 600; cursor: pointer; transition: background .15s; }
    .filter-btn.active, .filter-btn:hover { background: var(--primary); color: #fff; }
    .tag { display: inline-block; background: #edf2f5; color: var(--primary-dark); padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; }
    .modal-overlay.active { display: flex; }
    .modal-content { background: var(--card-bg); border-radius: 8px; padding: 24px; max-width: 340px; width: 90%; text-align: center; box-shadow: 0 8px 32px rgba(0,0,0,0.2); color: var(--text); }
    .share-actions { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
    .modal-content h3 { margin: 0 0 4px; font-size: 16px; }
    .modal-content p { font-size: 13px; margin: 4px 0 12px; }
    .modal-content canvas { display: block; margin: 0 auto 12px; }
    .upload-progress { display: none; margin-top: 12px; }
    .upload-progress.active { display: block; }
    .upload-bar-outer { width: 100%; height: 10px; background: #eef0f6; border-radius: 999px; overflow: hidden; }
    .upload-bar-inner { height: 100%; width: 0%; background: var(--primary); border-radius: 999px; transition: width .2s; }
    .upload-status { font-size: 13px; color: var(--muted); margin-top: 6px; }
    .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px); background: #1f2430; color: #fff; padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; opacity: 0; transition: opacity .25s, transform .25s; pointer-events: none; z-index: 999; }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
    html.dark { color-scheme: dark; --bg: #14181d; --text: #e7ebef; --muted: #9aa6b2; --line: #333b44; --primary: #4da3df; --primary-dark: #2987c7; --card-bg: #1d232a; --input-bg: #161b21; --input-border: #46515d; --file-bg: #252c34; }
    html.dark .card:hover { box-shadow: 0 4px 12px rgba(77, 163, 223, 0.15); }
    html.dark .notice { background: #1e293b; border-color: var(--primary); }
    html.dark .button.secondary, html.dark button.secondary { background: #1e293b; color: var(--primary); }
    html.dark .button.secondary:hover, html.dark button.secondary:hover { background: #334155; }
    html.dark input[type=file]:hover, html.dark input[type=file]:focus { background: #334155; border-color: var(--primary); }
    html.dark tr:hover td { background: rgba(77,163,223,0.06); }
    html.dark .upload-bar-outer, html.dark .disk-bar-outer { background: #334155; }
    html.dark .filter-btn { background: #1e293b; color: var(--primary); }
    html.dark .tag { background: #1e293b; color: var(--primary); }
    html.dark .toast { background: #e2e8f0; color: #0f172a; }
    html.dark .code-block { background: #1a1a2e; }
    @media (prefers-color-scheme: dark) { html:not(.light) { color-scheme: dark; --bg: #14181d; --text: #e7ebef; --muted: #9aa6b2; --line: #333b44; --primary: #4da3df; --primary-dark: #2987c7; --card-bg: #1d232a; --input-bg: #161b21; --input-border: #46515d; --file-bg: #252c34; } }
    @media (prefers-color-scheme: dark) {
      html:not(.light) .card:hover { box-shadow: 0 4px 12px rgba(77, 163, 223, 0.15); }
      html:not(.light) .notice { background: #1e293b; border-color: var(--primary); }
      html:not(.light) .button.secondary, html:not(.light) button.secondary { background: #1e293b; color: var(--primary); }
      html:not(.light) .button.secondary:hover, html:not(.light) button.secondary:hover { background: #334155; }
      html:not(.light) input[type=file]:hover, html:not(.light) input[type=file]:focus { background: #334155; border-color: var(--primary); }
      html:not(.light) tr:hover td { background: rgba(77,163,223,0.06); }
      html:not(.light) .upload-bar-outer, html:not(.light) .disk-bar-outer { background: #334155; }
      html:not(.light) .filter-btn { background: #1e293b; color: var(--primary); }
      html:not(.light) .tag { background: #1e293b; color: var(--primary); }
      html:not(.light) .toast { background: #e2e8f0; color: #0f172a; }
      html:not(.light) .code-block { background: #1a1a2e; }
    }
    :root { --primary: #1769aa; --primary-dark: #0f548c; --bg: #f3f5f7; --text: #18212b; --muted: #64707d; --line: #dce1e6; --card-bg: #fff; --input-bg: #fff; --input-border: #cbd3db; --file-bg: #f8fafb; }
    main { width: min(1440px, calc(100% - 32px)); margin: 0 auto; padding: 20px 0 40px; }
    h1 { font-size: 26px; }
    h2 { font-size: 16px; }
    .button, button, input[type=submit] { border-radius: 6px; background: var(--primary); padding: 9px 12px; transition: background .15s; }
    .button:hover, button:hover, input[type=submit]:hover { background: var(--primary-dark); transform: none; }
    .button.secondary, button.secondary { background: #e8eef3; color: #23435d; }
    .button.secondary:hover, button.secondary:hover { background: #d9e3eb; }
    .site-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
    .header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
    .icon-button { width: 40px; height: 40px; padding: 0; display: inline-grid; place-items: center; font-size: 20px; }
    .theme-toggle { border: 1px solid var(--line); color: var(--text); }
    .app-shell { display: grid; grid-template-columns: 64px minmax(0, 1fr); grid-template-areas: "tools files"; gap: 12px; align-items: start; }
    .file-workspace { grid-area: files; min-width: 0; padding: 0; }
    .admin-tool-rail { grid-area: tools; position: sticky; top: 16px; z-index: 30; display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); box-shadow: 0 6px 18px rgba(15, 23, 42, .08); }
    .admin-tool-button { position: relative; width: 46px; height: 46px; padding: 0; display: grid; place-items: center; border-radius: 6px; background: transparent; color: var(--text); }
    .admin-tool-button:hover, .admin-tool-button[aria-expanded="true"] { background: var(--primary); color: #fff; }
    .admin-tool-icon { font-size: 25px; line-height: 1; }
    .admin-tool-tooltip { position: absolute; left: calc(100% + 12px); top: 50%; transform: translateY(-50%); width: max-content; max-width: 180px; padding: 6px 9px; border-radius: 4px; background: #1f2933; color: #fff; font-size: 12px; font-weight: 600; opacity: 0; visibility: hidden; pointer-events: none; transition: opacity .15s; }
    .admin-tool-button:hover .admin-tool-tooltip, .admin-tool-button:focus-visible .admin-tool-tooltip { opacity: 1; visibility: visible; }
    body.admin-modal-open { overflow: hidden; }
    .admin-modal-overlay { position: fixed; inset: 0; z-index: 1100; display: flex; align-items: center; justify-content: center; padding: 24px; background: rgba(15, 23, 42, .58); }
    .admin-modal-overlay[hidden] { display: none; }
    .admin-modal { display: flex; flex-direction: column; width: min(100%, 520px); max-height: 88vh; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); color: var(--text); box-shadow: 0 20px 60px rgba(15, 23, 42, .28); }
    .admin-modal-upload { width: min(100%, 560px); }
    .admin-modal-tasks { width: min(100%, 860px); }
    .admin-modal-header { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 18px; border-bottom: 1px solid var(--line); }
    .admin-modal-header h2, .admin-modal-header p { margin: 0; }
    .admin-modal-header p { margin-top: 3px; color: var(--muted); font-size: 12px; }
    .admin-modal-body { min-height: 0; overflow-y: auto; padding: 18px; }
    .admin-modal-close { flex: 0 0 40px; width: 40px; height: 40px; padding: 0; display: grid; place-items: center; background: transparent; color: var(--text); font-size: 24px; line-height: 1; }
    .admin-modal-close:hover { background: var(--line); color: var(--text); }
    .admin-modal input[type=submit] { width: 100%; }
    .task-section { overflow-x: auto; }
    .task-section table { min-width: 680px; font-size: 12px; }
    .task-section input[type=password] { min-width: 110px; }
    .status-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); overflow: hidden; }
    .status-item { min-width: 0; padding: 12px; border-right: 1px solid var(--line); }
    .status-item:last-child { border-right: 0; }
    .status-item > span { display: block; color: var(--muted); font-size: 12px; }
    .status-item strong { display: block; margin-top: 4px; font-size: 17px; overflow-wrap: anywhere; }
    .disk-bar-outer { height: 6px; }
    .disk-bar-inner { background: #2f855a; }
    .disk-bar-inner.warn { background: #d69e2e; }
    .disk-bar-inner.danger { background: #d64545; }
    .file-section { margin-top: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); padding: 16px; box-shadow: none; }
    .section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .section-heading h2 { margin: 0; }
    .site-note { margin-top: 12px; padding: 0 2px; font-size: 12px; }
    .search-box { margin-bottom: 10px; border-radius: 6px; }
    .filter-bar { gap: 5px; margin-bottom: 8px; }
    .filter-btn { background: #e8eef3; color: #29465e; border-radius: 6px; padding: 6px 10px; }
    .filter-btn.active, .filter-btn:hover { background: var(--primary); color: #fff; }
    .file-table { table-layout: auto; }
    .file-table th:last-child { width: 190px; text-align: right; }
    .file-row-main { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
    .file-type-icon { width: 32px; flex: 0 0 32px; font-size: 28px; line-height: 1; text-align: center; color: #3b596f; }
    .file-details { min-width: 0; }
    .file-name { overflow-wrap: anywhere; font-weight: 650; }
    .file-meta { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 4px; color: var(--muted); font-size: 12px; }
    .tag { background: #edf2f5; color: #415566; }
    .file-actions { display: flex; justify-content: flex-end; align-items: center; gap: 6px; min-width: 184px; }
    .file-action { white-space: nowrap; }
    .file-menu { position: relative; }
    .file-menu-toggle { width: 40px; height: 40px; padding: 0; font-size: 20px; }
    .file-menu-panel { position: absolute; z-index: 20; right: 0; top: calc(100% + 6px); width: min(260px, calc(100vw - 32px)); padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); box-shadow: 0 10px 28px rgba(15, 23, 42, .16); }
    .file-menu-panel[hidden] { display: none; }
    .menu-command { display: block; width: 100%; padding: 9px 10px; border-radius: 4px; text-align: left; background: transparent; color: var(--text); font-weight: 600; }
    .menu-command:hover { background: var(--file-bg); color: var(--text); }
    .danger-text { color: #b4232c; }
    .renew-form { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; padding-top: 6px; }
    .renew-form input[type=password] { min-width: 0; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    .filter-empty { display: none; padding: 24px 16px; color: var(--muted); text-align: center; }
    .filter-empty.visible { display: block; }
    .drop-zone { border-radius: 8px; padding: 22px 12px; transform: none; }
    .drop-zone:hover, .drop-zone.drag-over { background: #eef5fa; transform: none; }
    .drop-icon { display: block; font-size: 28px; color: var(--primary); }
    #drop-file-name { margin-top: 6px; font-size: 12px; overflow-wrap: anywhere; }
    .bar, .upload-bar-inner { background: var(--primary); }
    .modal-content { border-radius: 8px; }
    html.dark { --bg: #14181d; --text: #e7ebef; --muted: #9aa6b2; --line: #333b44; --primary: #4da3df; --primary-dark: #2987c7; --card-bg: #1d232a; --input-bg: #161b21; --input-border: #46515d; --file-bg: #252c34; }
    html.dark .button.secondary, html.dark button.secondary { background: #2b343d; color: #d9e4ec; }
    html.dark .button.secondary:hover, html.dark button.secondary:hover { background: #35414c; }
    html.dark .filter-btn, html.dark .tag { background: #2b343d; color: #d9e4ec; }
    html.dark .menu-command:hover { background: #2b343d; }
    @media (prefers-color-scheme: dark) {
      html:not(.light) { --bg: #14181d; --text: #e7ebef; --muted: #9aa6b2; --line: #333b44; --primary: #4da3df; --primary-dark: #2987c7; --card-bg: #1d232a; --input-bg: #161b21; --input-border: #46515d; --file-bg: #252c34; }
      html:not(.light) .button.secondary, html:not(.light) button.secondary { background: #2b343d; color: #d9e4ec; }
      html:not(.light) .button.secondary:hover, html:not(.light) button.secondary:hover { background: #35414c; }
      html:not(.light) .filter-btn, html:not(.light) .tag { background: #2b343d; color: #d9e4ec; }
      html:not(.light) .menu-command:hover { background: #2b343d; }
    }
    @media (max-width: 900px) {
      main { width: min(calc(100% - 24px), 720px); padding: 12px 0 104px; }
      .site-header { align-items: flex-start; }
      .app-shell { display: block; }
      .file-workspace { width: 100%; }
      .admin-tool-rail { position: fixed; top: auto; right: 50%; bottom: 12px; z-index: 900; flex-direction: row; width: max-content; transform: translateX(50%); padding: 7px; box-shadow: 0 10px 30px rgba(15, 23, 42, .22); }
      .admin-tool-tooltip { display: none; }
      .admin-modal-overlay { padding: 16px 3vw; }
      .admin-modal, .admin-modal-upload, .admin-modal-tasks { width: 94vw; max-height: 88vh; }
      .status-strip { grid-template-columns: 1fr 1fr; }
      .status-item:nth-child(2) { border-right: 0; }
      .status-item:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .file-table thead { display: none; }
      .file-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--line); }
      .file-row:last-child { border-bottom: 0; }
      .file-row > td { display: block; border: 0; padding: 0; }
      .file-actions { min-width: 0; }
    }
    @media (max-width: 520px) {
      .site-header { flex-wrap: wrap; }
      .header-actions { width: 100%; justify-content: flex-start; }
      .status-strip { grid-template-columns: 1fr; }
      .status-item { border-right: 0; border-bottom: 1px solid var(--line); }
      .status-item:last-child { border-bottom: 0; }
      .file-row { grid-template-columns: 1fr; }
      .file-actions { justify-content: flex-start; padding-left: 42px; }
    }
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
    function apiError(xhr) {
      try {
        var payload = JSON.parse(xhr.responseText || '{}');
        return payload.error || '\u8bf7\u6c42\u5931\u8d25';
      } catch (e) {
        return '\u8bf7\u6c42\u5931\u8d25';
      }
    }
    function delay(ms) {
      return new Promise(function(resolve) { setTimeout(resolve, ms); });
    }
    async function postFormJson(path, fields) {
      var response = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams(fields).toString()
      });
      var payload = await response.json();
      if (!response.ok) throw new Error(payload.error || '\u8bf7\u6c42\u5931\u8d25');
      return payload;
    }
    async function initUpload(form, file) {
      return postFormJson('/api/upload_init', {
        password: form.querySelector('[name=password]').value,
        filename: file.name,
        custom_filename: (form.querySelector('[name=filename]') || {}).value || '',
        size: String(file.size),
        retention: (form.querySelector('[name=retention]') || {}).value || '0'
      });
    }
    async function finishUpload(session) {
      return postFormJson('/api/upload_finish', {
        upload_id: session.upload_id,
        upload_token: session.upload_token
      });
    }
    async function cancelUpload(session) {
      return postFormJson('/api/upload_cancel', {
        upload_id: session.upload_id,
        upload_token: session.upload_token
      });
    }
    async function sendChunk(session, file, index, state) {
      var retryDelays = [1000, 3000];
      var start = index * session.chunk_size;
      var end = Math.min(start + session.chunk_size, file.size);
      var chunk = file.slice(start, end);
      for (var attempt = 0; attempt < 3; attempt++) {
        if (state.cancelled) throw new Error('\u4e0a\u4f20\u5df2\u53d6\u6d88');
        try {
          await new Promise(function(resolve, reject) {
            var xhr = new XMLHttpRequest();
            state.activeXhrs.add(xhr);
            function settle(callback, value) {
              state.activeXhrs.delete(xhr);
              callback(value);
            }
            xhr.upload.onprogress = function(event) {
              if (event.lengthComputable) {
                state.loaded[index] = event.loaded;
                state.render();
              }
            };
            xhr.onload = function() {
              if (xhr.status >= 200 && xhr.status < 300) settle(resolve);
              else settle(reject, new Error(apiError(xhr)));
            };
            xhr.onerror = function() { settle(reject, new Error('\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25')); };
            xhr.onabort = function() { settle(reject, new Error('\u4e0a\u4f20\u5df2\u53d6\u6d88')); };
            xhr.open('POST', '/api/upload_chunk');
            xhr.setRequestHeader('X-Upload-Id', session.upload_id);
            xhr.setRequestHeader('X-Upload-Token', session.upload_token);
            xhr.setRequestHeader('X-Chunk-Index', String(index));
            xhr.send(chunk);
          });
          state.loaded[index] = chunk.size;
          state.completed += 1;
          state.render();
          return;
        } catch (error) {
          state.loaded[index] = 0;
          state.render();
          if (state.cancelled || attempt === 2) throw error;
          state.retries += 1;
          state.render();
          await delay(retryDelays[attempt]);
        }
      }
    }
    async function handleUpload(form) {
      var bar = document.getElementById('upload-bar');
      var status = document.getElementById('upload-status');
      var outer = document.getElementById('upload-progress');
      var submit = form.querySelector('input[type=submit]');
      var cancel = document.getElementById('upload-cancel');
      var uploadModal = document.getElementById('upload-modal');
      var file = form.querySelector('[name=file]').files[0];
      if (!file) return false;
      var session = null;
      var state = {
        activeXhrs: new Set(), loaded: [], completed: 0, active: 0,
        retries: 0, cancelled: false, render: function() {}
      };
      outer.classList.add('active');
      if (uploadModal) uploadModal.dataset.busy = 'true';
      submit.disabled = true;
      submit.value = '\u4e0a\u4f20\u4e2d...';
      try {
        session = await initUpload(form, file);
        cancel.hidden = false;
        state.loaded = new Array(session.total_chunks).fill(0);
        state.render = function() {
          var loaded = state.loaded.reduce(function(total, value) { return total + value; }, 0);
          var percent = Math.round(loaded / file.size * 100);
          bar.style.width = percent + '%';
          status.textContent = percent + '% \u00b7 ' + (loaded / 1048576).toFixed(1) + ' / ' +
            (file.size / 1048576).toFixed(1) + ' MiB \u00b7 \u5206\u7247 ' + state.completed + '/' +
            session.total_chunks + ' \u00b7 \u6d3b\u8dc3 ' + state.active + ' \u00b7 \u91cd\u8bd5 ' + state.retries;
        };
        cancel.onclick = async function() {
          if (state.cancelled) return;
          state.cancelled = true;
          state.activeXhrs.forEach(function(xhr) { xhr.abort(); });
          try { await cancelUpload(session); } catch (error) {}
          status.textContent = '\u4e0a\u4f20\u5df2\u53d6\u6d88';
        };
        var nextIndex = 0;
        async function worker() {
          while (!state.cancelled) {
            var index = nextIndex++;
            if (index >= session.total_chunks) return;
            state.active += 1;
            state.render();
            try {
              await sendChunk(session, file, index, state);
            } finally {
              state.active -= 1;
              state.render();
            }
          }
          throw new Error('\u4e0a\u4f20\u5df2\u53d6\u6d88');
        }
        var workers = [];
        var workerCount = Math.min(session.concurrency, session.total_chunks);
        for (var i = 0; i < workerCount; i++) workers.push(worker());
        await Promise.all(workers);
        if (state.cancelled) throw new Error('\u4e0a\u4f20\u5df2\u53d6\u6d88');
        status.textContent = '\u6821\u9a8c\u6587\u4ef6\u4e2d...';
        await finishUpload(session);
        bar.style.width = '100%';
        status.textContent = '\u4e0a\u4f20\u5b8c\u6210\uff01\u6b63\u5728\u8df3\u8f6c...';
        setTimeout(function() { window.location.href = '/downloads/'; }, 800);
      } catch (error) {
        if (!state.cancelled) status.textContent = error.message || '\u4e0a\u4f20\u5931\u8d25';
      } finally {
        state.activeXhrs.forEach(function(xhr) { xhr.abort(); });
        if (uploadModal) uploadModal.dataset.busy = 'false';
        cancel.hidden = true;
        submit.disabled = false;
        submit.value = '\u91cd\u8bd5\u4e0a\u4f20';
      }
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
      var rows = document.querySelectorAll('.file-row');
      var q = _curSearch.toLowerCase();
      var visible = 0;
      for (var i = 0; i < rows.length; i++) {
        var name = rows[i].getAttribute('data-name') || '';
        var matchType = _curFilter === 'all' || rows[i].getAttribute('data-type') === _curFilter;
        var matchSearch = !q || name.toLowerCase().indexOf(q) >= 0;
        rows[i].style.display = matchType && matchSearch ? '' : 'none';
        if (matchType && matchSearch) visible += 1;
      }
      var empty = document.getElementById('filter-empty');
      if (empty) empty.classList.toggle('visible', rows.length > 0 && visible === 0);
    }
    function filterFiles(type, selectedButton) {
      _curFilter = type;
      var btns = document.querySelectorAll('.filter-btn');
      for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
      if (selectedButton) selectedButton.classList.add('active');
      _applyFilters();
    }
    function searchFiles(q) {
      _curSearch = q;
      _applyFilters();
    }
    function closeFileMenus(exceptMenu) {
      var menus = document.querySelectorAll('.file-menu');
      for (var i = 0; i < menus.length; i++) {
        if (menus[i] === exceptMenu) continue;
        var button = menus[i].querySelector('.file-menu-toggle');
        var panel = menus[i].querySelector('.file-menu-panel');
        if (button) button.setAttribute('aria-expanded', 'false');
        if (panel) panel.hidden = true;
      }
    }
    function toggleFileMenu(button) {
      var menu = button.closest('.file-menu');
      var panel = document.getElementById(button.getAttribute('aria-controls'));
      if (!menu || !panel) return;
      var opening = button.getAttribute('aria-expanded') !== 'true';
      closeFileMenus(opening ? menu : null);
      button.setAttribute('aria-expanded', opening ? 'true' : 'false');
      panel.hidden = !opening;
      if (opening) {
        var first = panel.querySelector('button, a, input');
        if (first) first.focus();
      }
    }
    var activeAdminModal = null;
    var previousAdminTrigger = null;
    function openAdminModal(button) {
      var modal = document.getElementById(button.getAttribute('data-admin-modal'));
      if (!modal) return;
      if (activeAdminModal && activeAdminModal !== modal && !closeActiveAdminModal()) return;
      activeAdminModal = modal;
      previousAdminTrigger = button;
      modal.hidden = false;
      modal.setAttribute('aria-hidden', 'false');
      button.setAttribute('aria-expanded', 'true');
      document.body.classList.add('admin-modal-open');
      var first = modal.querySelector('input:not([type=hidden]), select, button, a');
      if (first) first.focus();
    }
    function closeAdminModal(modal) {
      if (!modal) return true;
      if (modal.dataset.busy === 'true') {
        showToast('请先完成或取消上传');
        return false;
      }
      modal.hidden = true;
      modal.setAttribute('aria-hidden', 'true');
      var trigger = document.querySelector('[aria-controls="' + modal.id + '"]');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('admin-modal-open');
      activeAdminModal = null;
      if (previousAdminTrigger) previousAdminTrigger.focus();
      previousAdminTrigger = null;
      return true;
    }
    function closeActiveAdminModal() {
      return closeAdminModal(activeAdminModal);
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
    document.addEventListener('click', function(e) {
      var adminTrigger = e.target.closest && e.target.closest('[data-admin-modal]');
      if (adminTrigger) {
        openAdminModal(adminTrigger);
        return;
      }
      var adminClose = e.target.closest && e.target.closest('[data-close-admin-modal]');
      if (adminClose) {
        closeActiveAdminModal();
        return;
      }
      var adminOverlay = e.target.closest && e.target.closest('.admin-modal-overlay');
      if (adminOverlay && e.target === adminOverlay) {
        closeAdminModal(adminOverlay);
        return;
      }
      var menuButton = e.target.closest && e.target.closest('.file-menu-toggle');
      if (menuButton) {
        toggleFileMenu(menuButton);
        return;
      }
      var shareButton = e.target.closest && e.target.closest('.share-btn');
      if (shareButton) {
        showShare(shareButton.dataset.url, shareButton.dataset.name);
        closeFileMenus();
        return;
      }
      if (!(e.target.closest && e.target.closest('.file-menu'))) closeFileMenus();
    });
    document.addEventListener('keydown', function(e) {
      if (e.key !== 'Escape') return;
      var openButton = document.querySelector('.file-menu-toggle[aria-expanded="true"]');
      closeFileMenus();
      closeShare();
      closeActiveAdminModal();
      if (openButton) openButton.focus();
    });
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


FILE_TYPE_ICONS = {
    "image": "▧",
    "video": "▶",
    "text": "≡",
    "document": "▤",
    "archive": "▣",
    "other": "•",
}


def file_type_icon(file_type_name: str) -> str:
    return FILE_TYPE_ICONS.get(file_type_name, FILE_TYPE_ICONS["other"])


def render_file_rows(files: list[dict[str, object]], compact: bool = False) -> str:
    if not files:
        return '<div class="empty">暂无可下载文件</div>'
    filter_bar = (
        '<input class="search-box" type="text" placeholder="\u641c\u7d22\u6587\u4ef6\u540d..." oninput="searchFiles(this.value)">'
        '<div class="filter-bar">'
        '<button class="filter-btn active" type="button" onclick="filterFiles(\'all\', this)">\u5168\u90e8</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'image\', this)">\u56fe\u7247</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'video\', this)">\u89c6\u9891</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'text\', this)">\u6587\u672c</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'document\', this)">\u6587\u6863</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'archive\', this)">\u538b\u7f29\u5305</button>'
        '<button class="filter-btn" type="button" onclick="filterFiles(\'other\', this)">\u5176\u4ed6</button>'
        '</div>'
    )
    rows = []
    for index, item in enumerate(files):
        name = str(item["name"])
        url_name = str(item["url_name"])
        ft = str(item.get("file_type", "other"))
        kind = file_kind(name)
        safe_name = html.escape(name)
        safe_name_attr = html.escape(name, quote=True)
        menu_id = f"file-menu-{index}"
        preview = (
            f'<a class="button secondary file-action" href="/view/{url_name}">预览</a>'
            if kind else ""
        )
        ret_label = html.escape(str(item.get("retention_label", "")))
        renew_form = (
            '<form class="renew-form" method="post" action="/api/renew">'
            f'<input type="hidden" name="filename" value="{safe_name_attr}">'
            f'<label class="sr-only" for="renew-password-{index}">管理密码</label>'
            f'<input id="renew-password-{index}" type="password" name="password" '
            'placeholder="管理密码" required>'
            '<button class="secondary" type="submit">续期</button></form>'
        )
        more_menu = (
            '<div class="file-menu">'
            f'<button class="file-menu-toggle secondary" type="button" '
            f'aria-label="更多操作：{safe_name_attr}" aria-expanded="false" '
            f'aria-controls="{menu_id}">⋯</button>'
            f'<div id="{menu_id}" class="file-menu-panel" hidden>'
            f'<button class="share-btn menu-command" type="button" '
            f'data-url="/file/{url_name}" data-name="{safe_name_attr}">二维码分享</button>'
            f'<a class="menu-command danger-text" href="/once/{url_name}">一次性下载</a>'
            f'{renew_form}</div></div>'
        )
        actions = (
            preview
            + f'<a class="button secondary file-action" href="/file/{url_name}">下载</a>'
            + more_menu
        )
        rows.append(
            f'<tr class="file-row" data-type="{html.escape(ft, quote=True)}" '
            f'data-name="{safe_name_attr}">'
            '<td><div class="file-row-main">'
            f'<span class="file-type-icon" aria-hidden="true">{file_type_icon(ft)}</span>'
            '<div class="file-details">'
            f'<div class="file-name code">{safe_name}</div>'
            '<div class="file-meta">'
            f'<span>{html.escape(str(item["size_human"]))}</span>'
            f'<span>入库 {html.escape(str(item["created_at_text"]))}</span>'
            f'<span>剩余 {html.escape(str(item["remaining_text"]))}</span>'
            f'<span class="tag">{ret_label}</span>'
            '</div></div></div></td>'
            f'<td class="file-actions">{actions}</td>'
            "</tr>"
        )
    return (
        filter_bar +
        '<table class="file-table"><thead><tr><th>文件</th><th>操作</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<div id="filter-empty" class="filter-empty">没有匹配的文件</div>'
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
    task_items = tasks.get("tasks") if isinstance(tasks, dict) else []
    task_count = len(task_items) if isinstance(task_items, list) else 0
    tasks_ok = bool(tasks.get("ok")) if isinstance(tasks, dict) else False
    task_summary = f"{task_count} 个任务" if tasks_ok else "状态不可用"
    message_html = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    body = f"""
<header class="site-header">
  <div>
    <h1>临时下载站</h1>
    <p>给朋友分享临时文件</p>
  </div>
  <div class="header-actions">
    <a class="button secondary" href="/downloads/">下载目录</a>
    <button class="icon-button secondary" type="button" onclick="location.reload()" aria-label="刷新" title="刷新">↻</button>
    <button class="icon-button theme-toggle" type="button" onclick="toggleTheme()" aria-label="切换主题" title="切换主题">◐</button>
  </div>
</header>
{message_html}
<div class="app-shell">
  <div class="file-workspace">
    <div class="status-strip">
      <div class="status-item"><span>文件</span><strong>{len(files)}</strong></div>
      <div class="status-item"><span>目录占用</span><strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
      <div class="status-item"><span>剩余空间</span><strong>{html.escape(str(stats["free_human"]))}</strong>
        <div class="disk-bar-outer"><div class="disk-bar-inner{' danger' if stats['disk_danger'] else ' warn' if stats['disk_percent'] > 80 else ''}" style="width:{stats['disk_percent']}%"></div></div>
      </div>
      <div class="status-item"><span>默认保留</span><strong>{RETENTION_HOURS:g}h</strong></div>
    </div>
    <section class="file-section">
      <div class="section-heading"><h2>可用文件</h2></div>
      {render_file_rows(files, compact=True)}
    </section>
    <div class="site-note">
      <p>文件按设定时间自动删除；一次性下载完整成功后立即删除。</p>
      <p>仅用于合法资源临时中转。剩余空间低于 {format_size(MIN_FREE_BYTES)} 时会拒绝新任务。</p>
    </div>
  </div>
  <nav class="admin-tool-rail" aria-label="管理员工具">
    <button id="open-add-task" class="admin-tool-button" type="button" data-admin-modal="add-task-modal"
            aria-controls="add-task-modal" aria-expanded="false" aria-label="添加链接" title="添加链接">
      <span class="admin-tool-icon" aria-hidden="true">↗</span><span class="admin-tool-tooltip">添加链接</span>
    </button>
    <button id="open-upload" class="admin-tool-button" type="button" data-admin-modal="upload-modal"
            aria-controls="upload-modal" aria-expanded="false" aria-label="上传文件" title="上传文件">
      <span class="admin-tool-icon" aria-hidden="true">⇧</span><span class="admin-tool-tooltip">上传文件</span>
    </button>
    <button id="open-tasks" class="admin-tool-button" type="button" data-admin-modal="tasks-modal"
            aria-controls="tasks-modal" aria-expanded="false" aria-label="下载任务：{html.escape(task_summary)}" title="下载任务">
      <span class="admin-tool-icon" aria-hidden="true">≡</span><span class="admin-tool-tooltip">下载任务</span>
    </button>
  </nav>
</div>
<div id="add-task-modal" class="admin-modal-overlay" hidden aria-hidden="true">
  <section class="admin-modal admin-modal-form" role="dialog" aria-modal="true" aria-labelledby="add-task-title">
    <header class="admin-modal-header">
      <h2 id="add-task-title">添加链接</h2>
      <button class="admin-modal-close" type="button" data-close-admin-modal aria-label="关闭添加链接">×</button>
    </header>
    <div class="admin-modal-body">
      <form method="post" action="/api/add-task">
        <label for="url">下载链接</label>
        <input id="url" name="url" type="text" inputmode="url" placeholder="https://example.com/file.zip" required>
        <label for="filename">自定义文件名（可选）</label>
        <input id="filename" name="filename" type="text" maxlength="180" placeholder="example.zip">
        {retention_select_html('task-retention')}
        <label for="password">管理密码</label>
        <input id="password" name="password" type="password" required>
        <p class="muted">指定文件名后可设置保留时间。</p>
        <input type="submit" value="添加任务">
      </form>
    </div>
  </section>
</div>
<div id="upload-modal" class="admin-modal-overlay" hidden aria-hidden="true" data-busy="false">
  <section class="admin-modal admin-modal-upload" role="dialog" aria-modal="true" aria-labelledby="upload-title">
    <header class="admin-modal-header">
      <h2 id="upload-title">上传文件</h2>
      <button class="admin-modal-close" type="button" data-close-admin-modal aria-label="关闭上传文件">×</button>
    </header>
    <div class="admin-modal-body">
      <form id="upload-form" method="post" action="/api/upload" enctype="multipart/form-data" onsubmit="return handleUpload(this)">
        <div id="drop-zone" class="drop-zone">
          <span class="drop-icon" aria-hidden="true">▣</span>
          <p>拖拽文件到这里，或点击选择文件</p>
          <input id="upload-file" name="file" type="file" required>
        </div>
        <div id="drop-file-name" class="muted"></div>
        <label for="upload-filename">自定义文件名（可选）</label>
        <input id="upload-filename" name="filename" type="text" maxlength="180">
        {retention_select_html('upload-retention')}
        <label for="upload-password">管理密码</label>
        <input id="upload-password" name="password" type="password" required>
        <p class="muted">单文件上限 {format_size(SINGLE_FILE_LIMIT_BYTES)}。</p>
        <div class="form-submit"><input type="submit" value="上传"></div>
        <div id="upload-progress" class="upload-progress">
          <div class="upload-bar-outer"><div id="upload-bar" class="upload-bar-inner"></div></div>
          <div id="upload-status" class="upload-status"></div>
          <button id="upload-cancel" class="danger" type="button" hidden>取消上传</button>
        </div>
      </form>
    </div>
  </section>
</div>
<div id="tasks-modal" class="admin-modal-overlay" hidden aria-hidden="true">
  <section class="admin-modal admin-modal-tasks" role="dialog" aria-modal="true" aria-labelledby="tasks-title">
    <header class="admin-modal-header">
      <div><h2 id="tasks-title">下载任务</h2><p>{html.escape(task_summary)}</p></div>
      <button class="admin-modal-close" type="button" data-close-admin-modal aria-label="关闭下载任务">×</button>
    </header>
    <div class="admin-modal-body task-section">
      {render_task_rows(tasks)}
    </div>
  </section>
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
    ext = Path(filename).suffix
    ascii_fallback = "download" + ext if ext else "download"
    quoted = urllib.parse.quote(filename)
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted}'


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

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        self.send_bytes(status, json_bytes(payload), "application/json; charset=utf-8")

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
        if raw_path == "/api/upload_init":
            self.handle_upload_init(form)
        elif raw_path == "/api/upload_finish":
            self.handle_upload_finish(form)
        elif raw_path == "/api/upload_cancel":
            self.handle_upload_cancel(form)
        elif raw_path == "/api/add-task":
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

    def handle_upload_init(self, form: dict[str, str]) -> None:
        if not check_admin_password(form.get("password", "")):
            self.send_json(403, {"error": "管理密码错误"})
            return
        try:
            filename = form.get("custom_filename", "").strip() or form.get("filename", "")
            name = validate_custom_filename(filename)
            size = int(form.get("size", "0") or "0")
            retention = int(form.get("retention", "0") or "0")
            if size <= 0:
                raise ValueError("文件大小必须大于 0")
            if retention and retention not in RETENTION_OPTIONS_SET:
                raise ValueError("保留时间无效")
            target = (DOWNLOADS_DIR / name).resolve()
            target.relative_to(DOWNLOADS_DIR)
            if target.exists():
                self.send_json(409, {"error": "同名文件已存在，已拒绝覆盖"})
                return
            if size > SINGLE_FILE_LIMIT_BYTES:
                self.send_json(413, {"error": f"上传文件超过单文件限制 {format_size(SINGLE_FILE_LIMIT_BYTES)}"})
                return
            ensure_upload_capacity(size)
            session = create_upload_session(name, size, UPLOAD_CHUNK_BYTES, retention)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self.send_json(413, {"error": str(exc)})
            return
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_json(500, {"error": "创建上传任务失败"})
            return
        upload_id = str(session["upload_id"])
        append_log(
            "upload.log",
            f"event=init upload_id={upload_id} name={urllib.parse.quote(name, safe='')} "
            f"size={size} chunks={session['total_chunks']}",
        )
        self.send_json(
            200,
            {
                "upload_id": upload_id,
                "upload_token": session["upload_token"],
                "chunk_size": session["chunk_size"],
                "total_chunks": session["total_chunks"],
                "concurrency": UPLOAD_CONCURRENCY,
            },
        )

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
        if content_length > UPLOAD_FALLBACK_MAX_BYTES:
            self.close_connection = True
            self.send_error_page(
                413,
                f"传统上传最大支持 {format_size(UPLOAD_FALLBACK_MAX_BYTES)}，大文件请启用 JavaScript 分片上传",
            )
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

            with META_LOCK:
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
        started = time.monotonic()
        body_consumed = False
        try:
            upload_id = self.headers.get("X-Upload-Id", "")
            chunk_index = int(self.headers.get("X-Chunk-Index", "-1"))
            upload_token = self.headers.get("X-Upload-Token", "")
            validate_upload_id(upload_id)
            lock = get_upload_lock(upload_id)
            with lock:
                try:
                    session = load_upload_session(upload_id)
                except FileNotFoundError:
                    self.send_json(404, {"error": "上传任务不存在"})
                    return
                if not hmac.compare_digest(str(session["upload_token"]), upload_token):
                    self.send_json(403, {"error": "上传任务凭证无效"})
                    return
                total_chunks = int(session["total_chunks"])
                if chunk_index < 0 or chunk_index >= total_chunks:
                    self.send_json(400, {"error": "分片编号超出范围"})
                    return
                size = int(session["size"])
                chunk_size = int(session["chunk_size"])
                offset = chunk_index * chunk_size
                expected_length = min(chunk_size, size - offset)
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                if content_length != expected_length:
                    self.send_json(400, {"error": "分片长度与上传任务不一致"})
                    return

                ensure_can_store_new_file()
                remaining = content_length
                written = 0
                read_seconds = 0.0
                write_seconds = 0.0
                tmp_path = upload_tmp_path(upload_id)
                with tmp_path.open("r+b") as out:
                    out.seek(offset)
                    while remaining > 0:
                        read_started = time.monotonic()
                        body = self.rfile.read(min(1024 * 1024, remaining))
                        read_seconds += time.monotonic() - read_started
                        if not body:
                            self.send_json(400, {"error": "网络中断导致分片不完整"})
                            return
                        write_started = time.monotonic()
                        out.write(body)
                        write_seconds += time.monotonic() - write_started
                        written += len(body)
                        remaining -= len(body)
                    body_consumed = True
                    out.flush()

                received = {int(index) for index in session["received_chunks"]}
                received.add(chunk_index)
                received_chunks = sorted(received)
                received_bytes = sum(
                    min(chunk_size, size - index * chunk_size) for index in received_chunks
                )
                session["received_chunks"] = received_chunks
                session["received_bytes"] = received_bytes
                session["updated_at"] = now_ts()
                save_upload_session(session)

            read_ms = round(read_seconds * 1000)
            write_ms = round(write_seconds * 1000)
            total_ms = round((time.monotonic() - started) * 1000)
            append_log(
                "upload.log",
                f"event=chunk upload_id={upload_id} idx={chunk_index} bytes={written} "
                f"read_ms={read_ms} write_ms={write_ms} total_ms={total_ms}",
            )
            self.send_json(
                200,
                {
                    "ok": True,
                    "received_chunks": len(received_chunks),
                    "received_bytes": received_bytes,
                },
            )
        except (TypeError, ValueError) as exc:
            self.send_json(400, {"error": str(exc) or "分片参数错误"})
        except RuntimeError as exc:
            self.send_json(413, {"error": str(exc)})
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_json(500, {"error": "上传分片处理异常"})
        finally:
            if not body_consumed:
                self.close_connection = True

    def handle_upload_finish(self, form: dict[str, str]) -> None:
        started = time.monotonic()
        upload_id = form.get("upload_id", "")
        renamed = False
        try:
            validate_upload_id(upload_id)
            lock = get_upload_lock(upload_id)
            with lock:
                try:
                    session = load_upload_session(upload_id)
                except FileNotFoundError:
                    self.send_json(404, {"error": "上传任务不存在"})
                    return
                if not hmac.compare_digest(
                    str(session["upload_token"]), form.get("upload_token", "")
                ):
                    self.send_json(403, {"error": "上传任务凭证无效"})
                    return
                total_chunks = int(session["total_chunks"])
                received = {int(index) for index in session["received_chunks"]}
                missing = [index for index in range(total_chunks) if index not in received]
                if missing:
                    self.send_json(400, {"error": f"分片 {missing[0]} 缺失"})
                    return
                tmp_path = upload_tmp_path(upload_id)
                size = int(session["size"])
                if not tmp_path.exists() or tmp_path.stat().st_size != size:
                    self.send_json(400, {"error": "临时文件大小与上传任务不一致"})
                    return
                name = str(session["filename"])
                target = (DOWNLOADS_DIR / name).resolve()
                target.relative_to(DOWNLOADS_DIR)
                if target.exists():
                    self.send_json(409, {"error": "同名文件已存在，已拒绝覆盖"})
                    return
                rename_started = time.monotonic()
                tmp_path.replace(target)
                rename_ms = round((time.monotonic() - rename_started) * 1000)
                renamed = True

                with META_LOCK:
                    meta = load_meta()
                    entry: dict[str, float] = {"created_at": now_ts()}
                    retention = int(session["retention_seconds"])
                    if retention:
                        entry["retention_seconds"] = float(retention)
                    meta[name] = entry
                    save_meta(meta)
                remove_upload_session(upload_id, remove_tmp=False)
                with UPLOAD_LOCKS_GUARD:
                    UPLOAD_LOCKS.pop(upload_id, None)

            total_ms = round((time.monotonic() - started) * 1000)
            append_log(
                "upload.log",
                f"event=finish upload_id={upload_id} size={size} "
                f"rename_ms={rename_ms} total_ms={total_ms}",
            )
            self.send_json(200, {"ok": True, "filename": name, "url": "/downloads/"})
        except (TypeError, ValueError) as exc:
            self.send_json(400, {"error": str(exc) or "完成上传参数错误"})
        except Exception:
            append_log("error.log", traceback.format_exc())
            if renamed:
                with contextlib.suppress(OSError):
                    upload_session_path(upload_id).unlink()
            self.send_json(500, {"error": "完成上传处理异常"})

    def handle_upload_cancel(self, form: dict[str, str]) -> None:
        upload_id = form.get("upload_id", "")
        try:
            validate_upload_id(upload_id)
            lock = get_upload_lock(upload_id)
            with lock:
                try:
                    session = load_upload_session(upload_id)
                except FileNotFoundError:
                    self.send_json(200, {"ok": True})
                    return
                if not hmac.compare_digest(
                    str(session["upload_token"]), form.get("upload_token", "")
                ):
                    self.send_json(403, {"error": "上传任务凭证无效"})
                    return
                remove_upload_session(upload_id)
            with UPLOAD_LOCKS_GUARD:
                UPLOAD_LOCKS.pop(upload_id, None)
            append_log("upload.log", f"event=cancel upload_id={upload_id} reason=user")
            self.send_json(200, {"ok": True})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            append_log("error.log", traceback.format_exc())
            self.send_json(500, {"error": "取消上传处理异常"})

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
