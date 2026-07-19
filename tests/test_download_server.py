import contextlib
import http.cookiejar
import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


class DownloadServerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="download-server-test-"))
        self.old_env = os.environ.copy()
        os.environ["APP_ROOT"] = str(self.tmp)
        os.environ["RETENTION_HOURS"] = "24"
        os.environ["MIN_FREE_BYTES"] = "0"
        if "download_server" in importlib.sys.modules:
            self.ds = importlib.reload(importlib.import_module("download_server"))
        else:
            self.ds = importlib.import_module("download_server")
        self.ds.ensure_directories()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_test_server(self):
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return server, thread, base

    def multipart_upload_body(self, password="good-password", filename="upload.txt", content=b"hello"):
        boundary = "----codex-upload-boundary"
        parts = [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="password"\r\n\r\n'
                f"{password}\r\n"
            ).encode("utf-8"),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + content
            + b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        return body, f"multipart/form-data; boundary={boundary}"

    def post_upload(self, base, password="good-password", filename="upload.txt", content=b"hello"):
        body, content_type = self.multipart_upload_body(password, filename, content)
        request = urllib.request.Request(
            f"{base}/api/upload",
            data=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=3)

    def post_form_json(self, base, path, fields):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            f"{base}{path}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post_upload_chunk(self, base, session, index, content, token=None):
        request = urllib.request.Request(
            f"{base}/api/upload_chunk",
            data=content,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Upload-Id": str(session["upload_id"]),
                "X-Upload-Token": token if token is not None else str(session["upload_token"]),
                "X-Chunk-Index": str(index),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_custom_filename_validation_accepts_allowed_characters(self):
        for name in ("中文 File_01-测试.txt", "视频 (1080p).mp4", "图片（原图）.png"):
            with self.subTest(name=name):
                self.assertEqual(self.ds.validate_custom_filename(name), name)

    def test_format_time_uses_local_time_without_timezone_suffix(self):
        formatted = self.ds.format_time(1_700_000_000)

        self.assertRegex(formatted, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("UTC+8", formatted)

    def test_log_write_failure_does_not_break_service_flow(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("locked")):
            self.ds.append_log("access.log", "request")

    def test_visitor_stats_track_active_and_recent_ips(self):
        current = 1_700_000_000.0
        with mock.patch.object(self.ds, "now_ts", return_value=current):
            self.ds.record_visit("203.0.113.10", "/")
            self.ds.record_visit("203.0.113.11", "/view/example.png")
            stats = self.ds.get_visitor_stats(current)

        self.assertEqual(stats["active"], 2)
        self.assertEqual(stats["unique_24h"], 2)
        self.assertEqual({item["ip"] for item in stats["recent"]}, {"203.0.113.10", "203.0.113.11"})

    def test_metadata_backup_keeps_status_and_manifest(self):
        self.ds.save_meta({"example.txt": {"created_at": 1_700_000_000.0}})

        status = self.ds.create_metadata_backup()

        self.assertTrue(status["ok"])
        self.assertEqual(status["count"], 1)
        latest = status["latest"]
        archive_path = self.ds.BACKUPS_DIR / latest["name"]
        with self.ds.zipfile.ZipFile(archive_path) as archive:
            self.assertIn("filemeta.json", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())

    def test_admin_login_failures_block_ip_and_success_can_clear_state(self):
        self.ds.ADMIN_LOGIN_MAX_FAILURES = 3
        self.ds.ADMIN_LOGIN_WINDOW_SECONDS = 600
        self.ds.ADMIN_LOGIN_BLOCK_SECONDS = 600
        ip = "203.0.113.20"

        self.assertEqual(self.ds.record_admin_login_failure(ip, 1000), 0)
        self.assertEqual(self.ds.record_admin_login_failure(ip, 1001), 0)
        self.assertEqual(self.ds.record_admin_login_failure(ip, 1002), 600)
        self.assertEqual(self.ds.admin_login_retry_after(ip, 1003), 599)
        self.ds.clear_admin_login_failures(ip)
        self.assertEqual(self.ds.admin_login_retry_after(ip, 1003), 0)

    def test_admin_login_endpoint_returns_429_after_repeated_failures(self):
        self.ds.ADMIN_LOGIN_MAX_FAILURES = 2
        self.ds.ADMIN_LOGIN_BLOCK_SECONDS = 600
        server, thread, base = self.start_test_server()
        try:
            for attempt in range(2):
                request = urllib.request.Request(
                    f"{base}/admin/login",
                    data=urllib.parse.urlencode({"password": "wrong"}).encode("utf-8"),
                    headers={"X-Forwarded-For": "203.0.113.21"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(error.exception.code, 403 if attempt == 0 else 429)
            self.assertGreaterEqual(int(error.exception.headers["Retry-After"]), 1)
            self.assertIn("管理密码尝试过多", error.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_failed_task_renders_reason_and_retry_action(self):
        normalized = self.ds.normalize_task(
            {
                "gid": "abc123",
                "status": "error",
                "errorCode": "2",
                "errorMessage": "",
                "totalLength": "100",
                "completedLength": "25",
                "files": [
                    {
                        "path": str(self.ds.DOWNLOADS_DIR / "archive.zip"),
                        "uris": [{"uri": "https://example.com/archive.zip"}],
                    }
                ],
            }
        )

        rendered = self.ds.render_task_rows({"ok": True, "tasks": [normalized]})

        self.assertEqual(normalized["error"], "aria2 错误 2：连接超时")
        self.assertTrue(normalized["retryable"])
        self.assertIn("查看失败原因", rendered)
        self.assertIn("/api/retry-task", rendered)
        self.assertNotIn('class="progress"', rendered)

    def test_retry_task_reuses_source_and_transfers_retention(self):
        old_gid = "abc123"
        self.ds.save_task_retentions({old_gid: 604800})
        task = {
            "gid": old_gid,
            "status": "error",
            "files": [
                {
                    "path": str(self.ds.DOWNLOADS_DIR / "archive.zip"),
                    "uris": [{"uri": "https://example.com/archive.zip"}],
                }
            ],
        }

        def rpc(method, params=None):
            if method == "aria2.tellStatus":
                return task
            if method == "aria2.addUri":
                self.assertEqual(params[1]["out"], "archive.zip")
                self.assertEqual(params[1]["continue"], "true")
                return "def456"
            if method == "aria2.removeDownloadResult":
                return "OK"
            raise AssertionError(method)

        with (
            mock.patch.object(self.ds, "ensure_can_add_task"),
            mock.patch.object(self.ds, "aria2_rpc", side_effect=rpc),
        ):
            new_gid = self.ds.retry_aria2_task(old_gid)

        self.assertEqual(new_gid, "def456")
        self.assertEqual(self.ds.load_task_retentions(), {"def456": 604800})

    def test_maintenance_cycle_cleans_files_and_creates_due_backup(self):
        current = 1_800_000_000.0
        target = self.ds.DOWNLOADS_DIR / "expired.txt"
        target.write_text("expired", encoding="utf-8")
        self.ds.save_meta({target.name: {"created_at": current - 90000}})

        with mock.patch.object(self.ds, "now_ts", return_value=current):
            state = self.ds.run_maintenance_cycle(current)

        self.assertFalse(target.exists())
        self.assertEqual(state["last_removed"], 1)
        self.assertTrue(state["last_backup"])
        self.assertEqual(self.ds.get_backup_status()["count"], 1)

    def test_recent_events_are_sorted_and_rendered_in_admin_dashboard(self):
        with mock.patch.object(self.ds, "now_ts", return_value=1_800_000_000.0):
            self.ds.append_log("task.log", "failed gid=abc error=连接超时")
        with mock.patch.object(self.ds, "now_ts", return_value=1_800_000_060.0):
            self.ds.append_log("backup.log", "created name=metadata.zip size=100")

        events = self.ds.get_recent_events()
        with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
            dashboard = self.ds.render_admin_dashboard().decode("utf-8")

        self.assertEqual(events[0]["source"], "元数据备份")
        self.assertEqual(events[1]["level"], "bad")
        self.assertIn("最近事件", dashboard)
        self.assertIn("连接超时", dashboard)

    def test_admin_login_session_dashboard_backup_and_logout(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        try:
            unauthorized_backup = urllib.request.Request(f"{base}/api/admin/backup", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as unauthorized_error:
                urllib.request.urlopen(unauthorized_backup, timeout=3)
            self.assertEqual(unauthorized_error.exception.code, 403)

            with opener.open(f"{base}/admin/", timeout=3) as response:
                login_page = response.read().decode("utf-8")
            self.assertIn("管理员登录", login_page)
            self.assertNotIn("系统监控", login_page)

            login_data = urllib.parse.urlencode({"password": password}).encode("utf-8")
            login_request = urllib.request.Request(
                f"{base}/admin/login",
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-For": "203.0.113.5"},
                method="POST",
            )
            with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
                with opener.open(login_request, timeout=3) as response:
                    dashboard = response.read().decode("utf-8")
            self.assertIn("系统监控", dashboard)
            self.assertIn("Backup 状态", dashboard)
            self.assertIn("203.0.113.5", dashboard)

            backup_request = urllib.request.Request(f"{base}/api/admin/backup", data=b"", method="POST")
            with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
                with opener.open(backup_request, timeout=3) as response:
                    backup_page = response.read().decode("utf-8")
            self.assertIn("备份完成", backup_page)
            self.assertEqual(self.ds.get_backup_status()["count"], 1)

            logout_request = urllib.request.Request(f"{base}/admin/logout", data=b"", method="POST")
            with opener.open(logout_request, timeout=3) as response:
                logged_out = response.read().decode("utf-8")
            self.assertIn("管理员登录", logged_out)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_healthz_reports_service_aria_and_disk_without_secrets(self):
        server, thread, base = self.start_test_server()
        try:
            with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
                with urllib.request.urlopen(f"{base}/healthz", timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["service"])
            self.assertTrue(payload["aria2"])
            self.assertIn("free_human", payload["disk"])
            self.assertNotIn("password", json.dumps(payload).lower())
            self.assertNotIn("secret", json.dumps(payload).lower())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_session_round_trip_and_paths_are_server_derived(self):
        session = self.ds.create_upload_session("video.bin", 9, 4, 0)

        loaded = self.ds.load_upload_session(session["upload_id"])

        self.assertEqual(loaded["filename"], "video.bin")
        self.assertEqual(loaded["total_chunks"], 3)
        self.assertEqual(loaded["received_chunks"], [])
        self.assertEqual(
            self.ds.upload_tmp_path(session["upload_id"]).parent,
            self.ds.DOWNLOADS_DIR,
        )
        self.assertEqual(
            self.ds.upload_session_path(session["upload_id"]).parent,
            self.ds.UPLOADS_DIR,
        )

    def test_link_task_without_custom_filename_remembers_retention_by_gid(self):
        with (
            mock.patch.object(self.ds, "ensure_can_add_task"),
            mock.patch.object(self.ds, "aria2_rpc", return_value="abc123"),
        ):
            gid = self.ds.add_aria2_task("https://example.com/photo.avif", None, 604800)

        self.assertEqual(gid, "abc123")
        self.assertEqual(self.ds.load_task_retentions(), {"abc123": 604800})

    def test_completed_link_task_applies_pending_retention_to_actual_filename(self):
        target = self.ds.DOWNLOADS_DIR / "resolved-name.avif"
        target.write_bytes(b"image")
        self.ds.save_task_retentions({"abc123": 604800})
        task = {
            "gid": "abc123",
            "status": "complete",
            "files": [{"path": str(target)}],
        }

        self.ds.sync_task_retentions([task])

        self.assertEqual(self.ds.load_meta()[target.name]["retention_seconds"], 604800.0)
        self.assertEqual(self.ds.load_task_retentions(), {})
        self.assertEqual(self.ds.scan_files()[0]["retention_label"], "7d")

    def test_load_upload_session_rejects_invalid_id(self):
        for upload_id in ("../bad", "bad/slash", "", "a" * 65):
            with self.subTest(upload_id=upload_id):
                with self.assertRaises(ValueError):
                    self.ds.load_upload_session(upload_id)

    def test_load_upload_session_rejects_invalid_persisted_types(self):
        session = self.ds.create_upload_session("broken.bin", 4, 4, 0)
        session["size"] = "not-an-integer"
        self.ds.save_upload_session(session)

        with self.assertRaises(ValueError):
            self.ds.load_upload_session(session["upload_id"])

    def test_upload_init_rejects_bad_password(self):
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_form_json(base, "/api/upload_init", {
                    "password": "wrong",
                    "filename": "file.bin",
                    "size": "9",
                    "retention": "0",
                })
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(list(self.ds.UPLOADS_DIR.glob("*.json")), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_init_rejects_invalid_filename(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_form_json(base, "/api/upload_init", {
                    "password": password,
                    "filename": "../bad.bin",
                    "size": "9",
                    "retention": "0",
                })
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_init_rejects_oversized_file(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            with mock.patch.object(self.ds, "SINGLE_FILE_LIMIT_BYTES", 8):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post_form_json(base, "/api/upload_init", {
                        "password": password,
                        "filename": "large.bin",
                        "size": "9",
                        "retention": "0",
                    })
            self.assertEqual(error.exception.code, 413)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_init_rejects_existing_target(self):
        password = self.ds.get_admin_password()
        (self.ds.DOWNLOADS_DIR / "same.bin").write_bytes(b"old")
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_form_json(base, "/api/upload_init", {
                    "password": password,
                    "filename": "same.bin",
                    "size": "9",
                    "retention": "0",
                })
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_init_creates_server_session(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            with mock.patch.object(self.ds, "UPLOAD_CHUNK_BYTES", 4), mock.patch.object(
                self.ds, "UPLOAD_CONCURRENCY", 2
            ):
                status, payload = self.post_form_json(base, "/api/upload_init", {
                    "password": password,
                    "filename": "original.bin",
                    "custom_filename": "final.bin",
                    "size": "9",
                    "retention": "604800",
                })
            self.assertEqual(status, 200)
            self.assertEqual(payload["chunk_size"], 4)
            self.assertEqual(payload["total_chunks"], 3)
            self.assertEqual(payload["concurrency"], 2)
            self.assertTrue(payload["upload_id"])
            self.assertTrue(payload["upload_token"])
            session = self.ds.load_upload_session(payload["upload_id"])
            self.assertEqual(session["filename"], "final.bin")
            self.assertEqual(session["retention_seconds"], 604800)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_chunks_write_directly_by_offset(self):
        session = self.ds.create_upload_session("ordered.bin", 9, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            self.post_upload_chunk(base, session, 1, b"EFGH")
            self.post_upload_chunk(base, session, 0, b"ABCD")
            self.post_upload_chunk(base, session, 2, b"I")

            self.assertEqual(
                self.ds.upload_tmp_path(session["upload_id"]).read_bytes(),
                b"ABCDEFGHI",
            )
            loaded = self.ds.load_upload_session(session["upload_id"])
            self.assertEqual(loaded["received_chunks"], [0, 1, 2])
            self.assertEqual(loaded["received_bytes"], 9)
            self.assertEqual(list(self.ds.DOWNLOADS_DIR.glob("*.part*")), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_chunk_retry_is_idempotent(self):
        session = self.ds.create_upload_session("retry.bin", 4, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            self.post_upload_chunk(base, session, 0, b"ABCD")
            status, payload = self.post_upload_chunk(base, session, 0, b"ABCD")

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            loaded = self.ds.load_upload_session(session["upload_id"])
            self.assertEqual(loaded["received_chunks"], [0])
            self.assertEqual(loaded["received_bytes"], 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_chunk_rejects_wrong_token(self):
        session = self.ds.create_upload_session("secret.bin", 4, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_upload_chunk(base, session, 0, b"ABCD", token="wrong")
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(self.ds.load_upload_session(session["upload_id"])["received_chunks"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_chunk_early_rejection_closes_connection_without_reading_body(self):
        session = self.ds.create_upload_session("connection.bin", 4, 4, 0)
        handler = object.__new__(self.ds.DownloadHandler)
        handler.headers = {
            "X-Upload-Id": session["upload_id"],
            "X-Upload-Token": "wrong",
            "X-Chunk-Index": "0",
            "Content-Length": "4",
        }
        handler.rfile = mock.Mock()
        handler.send_json = mock.Mock()
        handler.close_connection = False

        handler.handle_upload_chunk()

        handler.send_json.assert_called_once_with(403, {"error": "上传任务凭证无效"})
        self.assertEqual(handler.rfile.method_calls, [])
        self.assertTrue(handler.close_connection)

    def test_upload_chunk_rejects_wrong_length(self):
        session = self.ds.create_upload_session("short.bin", 4, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_upload_chunk(base, session, 0, b"ABC")
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.ds.load_upload_session(session["upload_id"])["received_chunks"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_finish_rejects_missing_chunk(self):
        session = self.ds.create_upload_session("missing.bin", 8, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            self.post_upload_chunk(base, session, 0, b"ABCD")
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_form_json(base, "/api/upload_finish", {
                    "upload_id": session["upload_id"],
                    "upload_token": session["upload_token"],
                })
            self.assertEqual(error.exception.code, 400)
            self.assertTrue(self.ds.upload_tmp_path(session["upload_id"]).exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_finish_rejects_wrong_token(self):
        session = self.ds.create_upload_session("token.bin", 4, 4, 0)
        server, thread, base = self.start_test_server()
        try:
            self.post_upload_chunk(base, session, 0, b"ABCD")
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_form_json(base, "/api/upload_finish", {
                    "upload_id": session["upload_id"],
                    "upload_token": "wrong",
                })
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_finish_renames_and_writes_seven_day_meta(self):
        session = self.ds.create_upload_session("final.bin", 9, 4, 604800)
        server, thread, base = self.start_test_server()
        try:
            self.post_upload_chunk(base, session, 0, b"ABCD")
            self.post_upload_chunk(base, session, 1, b"EFGH")
            self.post_upload_chunk(base, session, 2, b"I")
            status, payload = self.post_form_json(base, "/api/upload_finish", {
                "upload_id": session["upload_id"],
                "upload_token": session["upload_token"],
            })

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["url"], "/")
            self.assertEqual((self.ds.DOWNLOADS_DIR / "final.bin").read_bytes(), b"ABCDEFGHI")
            self.assertFalse(self.ds.upload_tmp_path(session["upload_id"]).exists())
            self.assertFalse(self.ds.upload_session_path(session["upload_id"]).exists())
            self.assertEqual(self.ds.load_meta()["final.bin"]["retention_seconds"], 604800.0)
            files = self.ds.scan_files()
            self.assertEqual(files[0]["retention_label"], "7d")
            self.assertGreater(files[0]["expires_at"] - self.ds.now_ts(), 604790)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_cancel_is_idempotent(self):
        session = self.ds.create_upload_session("cancel.bin", 4, 4, 0)
        server, thread, base = self.start_test_server()
        fields = {
            "upload_id": session["upload_id"],
            "upload_token": session["upload_token"],
        }
        try:
            status, payload = self.post_form_json(base, "/api/upload_cancel", fields)
            second_status, second_payload = self.post_form_json(base, "/api/upload_cancel", fields)

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(second_status, 200)
            self.assertTrue(second_payload["ok"])
            self.assertFalse(self.ds.upload_tmp_path(session["upload_id"]).exists())
            self.assertFalse(self.ds.upload_session_path(session["upload_id"]).exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_usage_counts_temporary_files(self):
        session = self.ds.create_upload_session("usage.bin", 8, 4, 0)
        self.ds.upload_tmp_path(session["upload_id"]).write_bytes(b"ABCD")

        self.assertEqual(self.ds.get_upload_tmp_usage(), 4)
        self.assertEqual(self.ds.get_active_upload_reserved_bytes(), 4)

    def test_upload_capacity_does_not_double_count_preallocated_tmp_size(self):
        session = self.ds.create_upload_session("sparse.bin", 8, 4, 0)
        self.ds.upload_tmp_path(session["upload_id"]).write_bytes(b"ABCDEFGH")

        with mock.patch.object(self.ds, "MAX_DOWNLOAD_DIR_BYTES", 12), mock.patch.object(
            self.ds, "MIN_FREE_BYTES", 0
        ):
            self.ds.ensure_upload_capacity(4)

    def test_cleanup_removes_expired_upload_session(self):
        now = 1_800_000_000.0
        session = self.ds.create_upload_session("stale.bin", 8, 4, 0)
        session["updated_at"] = now - 61
        self.ds.save_upload_session(session)
        with mock.patch.object(self.ds, "UPLOAD_SESSION_TTL_SECONDS", 60):
            removed = self.ds.cleanup_upload_sessions(now)

        self.assertEqual(removed, [session["upload_id"]])
        self.assertFalse(self.ds.upload_tmp_path(session["upload_id"]).exists())
        self.assertFalse(self.ds.upload_session_path(session["upload_id"]).exists())
        self.assertIn("reason=expired", (self.ds.LOGS_DIR / "upload.log").read_text(encoding="utf-8"))

    def test_legacy_upload_rejects_oversized_body_before_read(self):
        handler = object.__new__(self.ds.DownloadHandler)
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=test",
            "Content-Length": "9",
        }
        handler.rfile = mock.Mock()
        handler.send_error_page = mock.Mock()
        handler.close_connection = False
        with mock.patch.object(self.ds, "UPLOAD_FALLBACK_MAX_BYTES", 8):
            handler.handle_upload()

        handler.send_error_page.assert_called_once()
        self.assertEqual(handler.send_error_page.call_args.args[0], 413)
        self.assertEqual(handler.rfile.method_calls, [])
        self.assertTrue(handler.close_connection)

    def test_home_renders_upload_v2_client(self):
        body = self.ds.render_home().decode("utf-8")

        for endpoint in (
            "/api/upload_init",
            "/api/upload_chunk",
            "/api/upload_finish",
            "/api/upload_cancel",
        ):
            self.assertIn(endpoint, body)
        self.assertIn("session.chunk_size", body)
        self.assertIn("session.concurrency", body)
        self.assertIn("X-Upload-Token", body)
        self.assertIn("[1000, 3000]", body)
        self.assertIn('id="upload-cancel"', body)
        self.assertIn("activeXhrs", body)
        self.assertIn('onsubmit="handleUpload(this); return false"', body)
        self.assertNotIn('onsubmit="return handleUpload(this)"', body)
        self.assertNotIn("Math.random()", body)
        self.assertNotIn("X-Password", body)
        self.assertIn("await refreshFilePanel(true)", body)
        self.assertNotIn("window.location.href = '/'", body)
        self.assertGreater(
            body.index("cancel.hidden = false"),
            body.index("session = await initUpload(form, file, selectedRetention)"),
        )

    def test_file_rows_render_icons_primary_actions_and_more_menu(self):
        files = [
            {
                "name": "clip.mp4",
                "url_name": "clip.mp4",
                "file_type": "video",
                "size_human": "12 MiB",
                "created_at_text": "2026-07-16 01:00",
                "expires_at_text": "2026-07-17 01:00",
                "remaining_text": "23h",
                "retention_label": "24h",
            }
        ]

        body = self.ds.render_file_rows(files, compact=True)

        self.assertIn('class="file-row"', body)
        self.assertIn('class="file-type-icon"', body)
        self.assertIn('href="/view/clip.mp4"', body)
        self.assertIn('href="/file/clip.mp4"', body)
        self.assertIn('class="file-menu-toggle', body)
        self.assertIn('data-url="/file/clip.mp4"', body)
        self.assertIn('href="/once/clip.mp4"', body)
        self.assertIn('onclick="return confirmOnceDownload(this)"', body)
        self.assertIn('id="file-sort"', body)
        self.assertIn('value="created-desc"', body)
        self.assertIn('value="expires-asc"', body)
        self.assertIn('data-created=', body)
        self.assertIn('data-expires=', body)
        self.assertIn('class="menu-command renew-btn"', body)
        self.assertIn("复制下载链接", body)
        self.assertIn("copyLink('/file/clip.mp4')", body)
        self.assertIn('class="menu-command danger-text delete-file-btn"', body)
        self.assertIn('onclick="deleteFile(this)"', body)
        self.assertIn('data-filename="clip.mp4"', body)
        self.assertNotIn('name="password"', body)
        self.assertIn('aria-expanded="false"', body)
        self.assertIn('id="filter-empty"', body)
        self.assertIn("filterFiles('video', this)", body)

    def test_file_rows_escape_names_in_menu_and_metadata(self):
        files = [
            {
                "name": '<bad&".txt',
                "url_name": "%3Cbad%26%22.txt",
                "file_type": "text",
                "size_human": "1 B",
                "created_at_text": "now",
                "expires_at_text": "later",
                "remaining_text": "1h",
                "retention_label": "1h",
            }
        ]

        body = self.ds.render_file_rows(files, compact=True)

        self.assertNotIn('<bad&".txt', body)
        self.assertIn("&lt;bad&amp;&quot;.txt", body)
        self.assertIn('data-filename="&lt;bad&amp;&quot;.txt"', body)

    def test_file_rows_highlight_urgent_expiration(self):
        current = 1_700_000_000.0
        base = {
            "url_name": "urgent.txt",
            "file_type": "text",
            "size_human": "1 B",
            "created_at_text": "now",
            "expires_at_text": "later",
            "remaining_text": "30 分钟",
            "retention_label": "1h",
        }
        with mock.patch.object(self.ds, "now_ts", return_value=current):
            danger = self.ds.render_file_rows([{**base, "name": "danger.txt", "expires_at": current + 1800}], compact=True)
            warning = self.ds.render_file_rows([{**base, "name": "warning.txt", "expires_at": current + 7200}], compact=True)

        self.assertIn("expiry-danger", danger)
        self.assertIn("expiry-warning", warning)

    def test_home_renders_status_cards_and_file_toolbar_without_legacy_sidebar(self):
        tasks = {
            "ok": True,
            "tasks": [
                {
                    "gid": "abc123",
                    "name": "queued.bin",
                    "hint": "",
                    "status": "active",
                    "progress": 25.0,
                    "speed_human": "1 MiB/s",
                    "completed_human": "1 MiB",
                    "total_human": "4 MiB",
                }
            ],
        }
        with mock.patch.object(self.ds, "get_aria2_tasks", return_value=tasks):
            body = self.ds.render_home("hello <world>").decode("utf-8")

        for marker in (
            'class="app-shell"',
            'class="file-workspace"',
            'class="status-strip"',
            'class="workspace-columns"',
            'class="task-panel"',
            'class="task-list"',
            'class="task-item"',
            'class="file-tools"',
            'id="open-add-task"',
            'id="open-upload"',
            'id="file-page-prev"',
            'id="task-page-prev"',
            'class="file-panel-body"',
            'aria-controls="add-task-modal"',
            'aria-controls="upload-modal"',
            'id="add-task-modal"',
            'id="upload-modal"',
            'action="/api/add-task"',
            'id="upload-form"',
            'action="/api/remove-task"',
            'action="/api/clear-stopped"',
            'id="upload-cancel"',
        ):
            self.assertIn(marker, body)

        for field in (
            'id="url"',
            'id="paste-task-urls"',
            'id="filename"',
            'id="task-retention"',
            'id="password"',
            'id="upload-file"',
            'id="upload-filename"',
            'id="upload-retention"',
            'id="upload-password"',
        ):
            self.assertIn(field, body)

        self.assertIn("hello &lt;world&gt;", body)
        self.assertNotIn("hello <world>", body)
        self.assertIn('onsubmit="handleAddTasks(this); return false"', body)
        self.assertNotIn('onsubmit="return handleAddTasks(this)"', body)
        self.assertIn('<span class="admin-tool-icon" aria-hidden="true">+</span>', body)
        self.assertEqual(body.count('class="status-item"'), 3)
        self.assertNotIn('class="admin-tool-rail"', body)
        self.assertNotIn('href="/downloads/"', body)
        self.assertNotIn('id="open-tasks"', body)
        self.assertNotIn('id="tasks-modal"', body)
        self.assertLess(body.index('class="file-section"'), body.index('class="task-panel"'))
        self.assertLess(body.index('<h2>可用文件</h2>'), body.index('id="open-add-task"'))

    def test_home_status_card_links_to_expiring_file_filter(self):
        target = self.ds.DOWNLOADS_DIR / "soon.txt"
        target.write_text("soon", encoding="utf-8")
        current = self.ds.now_ts()
        self.ds.save_meta(
            {
                target.name: {
                    "created_at": current,
                    "retention_seconds": 3600.0,
                }
            }
        )
        with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
            body = self.ds.render_home().decode("utf-8")

        self.assertIn("onclick=\"focusFilePanel('all')\"", body)
        self.assertIn("onclick=\"focusFilePanel('expiring')\"", body)
        self.assertIn('data-filter="expiring"', body)
        self.assertIn('id="expiring-count-value">1</span>', body)
        self.assertIn("_curFilter === 'expiring'", body)

    def test_task_panel_payload_polls_running_tasks_and_marks_completed_tasks(self):
        active = {
            "ok": True,
            "tasks": [{
                "gid": "a1", "name": "active.bin", "hint": "", "status": "active",
                "progress": 50.0, "speed_human": "1 MiB/s",
                "completed_human": "1 MiB", "total_human": "2 MiB",
            }],
        }
        complete = {
            "ok": True,
            "tasks": [{
                "gid": "b2", "name": "done.bin", "hint": "", "status": "complete",
                "progress": 100.0, "speed_human": "0 B/s",
                "completed_human": "2 MiB", "total_human": "2 MiB",
            }],
        }

        active_payload = self.ds.task_panel_payload(active)
        complete_payload = self.ds.task_panel_payload(complete)

        self.assertTrue(active_payload["poll"])
        self.assertIn('class="progress"', active_payload["html"])
        self.assertFalse(complete_payload["poll"])
        self.assertIn("已完成", complete_payload["html"])
        self.assertNotIn('class="progress"', complete_payload["html"])

    def test_task_panel_api_returns_refreshable_html(self):
        server, thread, base = self.start_test_server()
        try:
            with mock.patch.object(self.ds, "get_aria2_tasks", return_value={"ok": True, "tasks": []}):
                with urllib.request.urlopen(f"{base}/api/task-panel", timeout=3) as response:
                    status = response.status
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(status, 200)
            self.assertIn("暂无下载任务", payload["html"])
            self.assertFalse(payload["poll"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_file_panel_api_returns_signature_html_and_stats(self):
        target = self.ds.DOWNLOADS_DIR / "panel.txt"
        target.write_text("hello", encoding="utf-8")
        server, thread, base = self.start_test_server()
        try:
            with urllib.request.urlopen(f"{base}/api/file-panel", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["file_count"], 1)
            self.assertEqual(len(payload["signature"]), 16)
            self.assertIn("panel.txt", payload["html"])
            self.assertIn("downloads_used_human", payload["stats"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_legacy_downloads_page_redirects_to_home(self):
        server, thread, base = self.start_test_server()
        try:
            with urllib.request.urlopen(f"{base}/downloads/", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.geturl(), f"{base}/")
                body = response.read().decode("utf-8")
            self.assertIn("临时下载站", body)
            self.assertNotIn("<h1>下载目录</h1>", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_add_task_api_accepts_multiple_urls_and_returns_json(self):
        server, thread, base = self.start_test_server()
        try:
            with (
                mock.patch.object(self.ds, "check_admin_password", return_value=True),
                mock.patch.object(self.ds, "add_aria2_task", side_effect=["gid-a", "gid-b"]) as add_task,
            ):
                status, payload = self.post_form_json(base, "/api/add-task", {
                    "password": "good-password",
                    "url": "https://example.com/a.zip\nhttps://example.com/b.zip",
                    "filename": "",
                    "retention": "604800",
                })

            self.assertEqual(status, 200)
            self.assertEqual(payload["added"], 2)
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["gids"], ["gid-a", "gid-b"])
            self.assertEqual(add_task.call_args_list, [
                mock.call("https://example.com/a.zip", None, 604800),
                mock.call("https://example.com/b.zip", None, 604800),
            ])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_add_task_api_rejects_filename_for_multiple_urls(self):
        server, thread, base = self.start_test_server()
        try:
            with mock.patch.object(self.ds, "check_admin_password", return_value=True):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post_form_json(base, "/api/add-task", {
                        "password": "good-password",
                        "url": "https://example.com/a.zip\nhttps://example.com/b.zip",
                        "filename": "same.zip",
                        "retention": "86400",
                    })
            self.assertEqual(error.exception.code, 400)
            payload = json.loads(error.exception.read().decode("utf-8"))
            self.assertIn("批量", payload["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_renew_api_requires_no_password_and_returns_updated_remaining_time(self):
        target = self.ds.DOWNLOADS_DIR / "renew.txt"
        target.write_text("hello", encoding="utf-8")
        self.ds.save_meta({
            target.name: {
                "created_at": self.ds.now_ts() - 3600,
                "retention_seconds": 604800.0,
            }
        })
        server, thread, base = self.start_test_server()
        try:
            status, payload = self.post_form_json(base, "/api/renew", {"filename": target.name})

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertIn("小时", payload["remaining"])
            self.assertGreater(self.ds.load_meta()[target.name]["created_at"], self.ds.now_ts() - 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_delete_file_api_requires_password_and_removes_file_metadata(self):
        target = self.ds.DOWNLOADS_DIR / "delete-me.txt"
        target.write_text("hello", encoding="utf-8")
        self.ds.save_meta({target.name: {"created_at": self.ds.now_ts(), "download_count": 2.0}})
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            data = urllib.parse.urlencode({"filename": target.name, "password": "wrong"}).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/api/delete-file",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
            self.assertTrue(target.exists())

            status, payload = self.post_form_json(
                base, "/api/delete-file", {"filename": target.name, "password": password}
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertIn("downloads_used_human", payload["stats"])
            self.assertIn("free_human", payload["stats"])
            self.assertIn("disk_percent", payload["stats"])
            self.assertEqual(payload["stats"]["downloads_used_human"], "0 B")
            self.assertFalse(target.exists())
            self.assertNotIn(target.name, self.ds.load_meta())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_page_includes_responsive_workspace_modals_and_file_menu(self):
        body = self.ds.render_home().decode("utf-8")

        for css_or_script in (
            ".workspace-columns",
            "minmax(0, 1.45fr)",
            "height: 680px",
            ".file-list-scroll",
            ".file-table tbody tr",
            "@media (max-width: 900px)",
            ".file-type-icon",
            "font-size: 28px",
            ".admin-modal-overlay",
            "max-height: 88vh",
            "resize: none",
            "overflow-y: auto",
            "html.dark .filter-btn.active",
            "html:not(.light) .filter-btn.active",
            "bottom: 12px",
            "function toggleFileMenu",
            "function closeFileMenus",
            "aria-expanded",
            "filter-empty",
            "Escape",
        ):
            self.assertIn(css_or_script, body)

        self.assertNotIn("300px minmax(0, 1fr)", body)

    def test_page_includes_accessible_admin_modal_and_upload_busy_behavior(self):
        body = self.ds.render_home().decode("utf-8")

        for script_marker in (
            "function openAdminModal",
            "function closeAdminModal",
            "function closeActiveAdminModal",
            "previousAdminTrigger.focus()",
            "modal.dataset.busy === 'true'",
            "uploadModal.dataset.busy = 'true'",
            "uploadModal.dataset.busy = 'false'",
            "[data-close-admin-modal]",
            "[data-admin-modal]",
            "function handleAddTasks",
            "navigator.clipboard.readText()",
            "document.getElementById('upload-retention').value",
            "function renewFile",
            "function deleteFile",
            "'/api/delete-file'",
            "function confirmOnceDownload",
            "function openDangerConfirm",
            "function closeDangerConfirm",
            "function submitDangerConfirm",
            "function updateDiskStats",
            "id=\"danger-confirm-modal\"",
            "id=\"downloads-used-value\"",
            "id=\"disk-usage-bar\"",
            "function sortFiles",
            "if (resetPage) _taskPage = 1",
            "body.contains(document.activeElement)",
            "await refreshTaskPanel(true)",
            "d.setAttribute('role', 'status')",
            "'，剩余 ' + payload.remaining",
            "function changeFilePage",
            "function applyTaskPagination",
            "FILE_PAGE_SIZE",
            "noRowsLabel.textContent = '0 / 0'",
            "function refreshTaskPanel",
            "function refreshFilePanel",
            "'/api/file-panel'",
            "sessionStorage.setItem('fileViewState'",
            "function restoreFileViewControls",
            "scheduleTaskRefresh",
        ):
            self.assertIn(script_marker, body)

        self.assertNotIn("window.confirm", body)
        self.assertNotIn("window.prompt", body)

    def test_mobile_workspace_stacks_tasks_after_files_without_fixed_toolbar(self):
        body = self.ds.render_home().decode("utf-8")

        self.assertLess(
            body.index('class="file-section"'),
            body.index('class="task-panel"'),
        )
        self.assertNotIn('class="admin-tool-rail"', body)

    def test_page_avoids_oversized_radii_and_legacy_purple_gradient(self):
        body = self.ds.render_home().decode("utf-8")

        self.assertNotIn("border-radius: 10px", body)
        self.assertNotIn("border-radius: 12px", body)
        self.assertNotIn("#6d5dfc", body.lower())
        self.assertNotIn("#8b5cf6", body.lower())

    def test_custom_filename_validation_rejects_dangerous_names(self):
        bad_names = [
            "",
            "a" * 181,
            "../x",
            "..",
            "a..txt",
            "dir/file.txt",
            "dir\\file.txt",
            "bad\nname.txt",
            "bad:name.txt",
            "semi;colon.txt",
        ]
        for name in bad_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.ds.validate_custom_filename(name)

    def test_safe_download_path_rejects_encoded_traversal_and_private_paths(self):
        safe_file = self.ds.DOWNLOADS_DIR / "ok.txt"
        safe_file.write_text("ok", encoding="utf-8")

        self.assertEqual(self.ds.safe_download_path("ok.txt"), safe_file)
        for name in ["..%2fdownload_server.py", "%2e%2e/download_server.py", "data/admin_password.txt"]:
            with self.subTest(name=name):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.ds.safe_download_path(name)

    def test_scan_files_keeps_original_created_at_and_prunes_missing(self):
        first_time = 1_700_000_000.0
        second_time = first_time + 600
        file_path = self.ds.DOWNLOADS_DIR / "file.txt"
        file_path.write_text("hello", encoding="utf-8")

        with mock.patch("download_server.time.time", return_value=first_time):
            first = self.ds.scan_files()
        with mock.patch("download_server.time.time", return_value=second_time):
            second = self.ds.scan_files()

        self.assertEqual(first[0]["created_at"], first_time)
        self.assertEqual(second[0]["created_at"], first_time)

        file_path.unlink()
        self.assertEqual(self.ds.scan_files(), [])
        self.assertEqual(self.ds.load_meta(), {})

    def test_file_metadata_preserves_and_renders_download_count(self):
        target = self.ds.DOWNLOADS_DIR / "counted.txt"
        target.write_text("hello", encoding="utf-8")
        self.ds.save_meta({
            target.name: {
                "created_at": self.ds.now_ts(),
                "retention_seconds": 86400.0,
                "download_count": 3.0,
                "preview_count": 2.0,
            }
        })

        files = self.ds.scan_files()
        body = self.ds.render_file_rows(files, compact=True)

        self.assertEqual(files[0]["download_count"], 3)
        self.assertEqual(files[0]["preview_count"], 2)
        self.assertIn("下载 3 次", body)

    def test_full_file_download_increments_count_but_head_does_not(self):
        target = self.ds.DOWNLOADS_DIR / "counter.bin"
        target.write_bytes(b"abcdef")
        self.ds.save_meta({target.name: {"created_at": self.ds.now_ts(), "download_count": 0.0}})
        server, thread, base = self.start_test_server()
        try:
            head = urllib.request.Request(f"{base}/file/{target.name}", method="HEAD")
            with urllib.request.urlopen(head, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.ds.load_meta()[target.name]["download_count"], 0.0)

            with urllib.request.urlopen(f"{base}/file/{target.name}", timeout=3) as response:
                self.assertEqual(response.read(), b"abcdef")
            for _ in range(50):
                if self.ds.load_meta()[target.name].get("download_count") == 1.0:
                    break
                self.ds.time.sleep(0.01)
            self.assertEqual(self.ds.load_meta()[target.name]["download_count"], 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_interrupted_file_download_does_not_increment_count(self):
        target = self.ds.DOWNLOADS_DIR / "interrupted.bin"
        target.write_bytes(b"abcdef")
        self.ds.save_meta({target.name: {"created_at": self.ds.now_ts(), "download_count": 2.0}})
        handler = object.__new__(self.ds.DownloadHandler)
        handler.command = "GET"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = ConnectionError("client closed")

        with self.assertRaises(ConnectionError):
            handler.handle_file(target.name, head_only=False)

        self.assertEqual(self.ds.load_meta()[target.name]["download_count"], 2.0)

    def test_cleanup_expired_removes_old_files_and_logs(self):
        now = 1_700_100_000.0
        old_file = self.ds.DOWNLOADS_DIR / "old.txt"
        new_file = self.ds.DOWNLOADS_DIR / "new.txt"
        old_file.write_text("old", encoding="utf-8")
        new_file.write_text("new", encoding="utf-8")
        self.ds.save_meta(
            {
                "old.txt": {"created_at": now - (25 * 3600)},
                "new.txt": {"created_at": now - 60},
            }
        )

        with mock.patch("download_server.time.time", return_value=now):
            removed = self.ds.cleanup_expired()

        self.assertIn("old.txt", removed)
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())
        self.assertNotIn("old.txt", self.ds.load_meta())
        self.assertIn("expired", (self.ds.LOGS_DIR / "cleanup.log").read_text(encoding="utf-8"))

    def test_once_download_deletes_only_after_complete_transfer(self):
        target = self.ds.DOWNLOADS_DIR / "once.txt"
        target.write_bytes(b"abcdef")
        self.ds.save_meta({"once.txt": {"created_at": 1_700_000_000.0}})

        sink = bytearray()
        self.ds.stream_once_file(target, sink.extend, chunk_size=2)

        self.assertEqual(bytes(sink), b"abcdef")
        self.assertFalse(target.exists())
        self.assertNotIn("once.txt", self.ds.load_meta())
        self.assertIn("completed", (self.ds.LOGS_DIR / "once-download.log").read_text(encoding="utf-8"))

    def test_once_download_keeps_file_when_transfer_fails(self):
        target = self.ds.DOWNLOADS_DIR / "broken.txt"
        target.write_bytes(b"abcdef")
        self.ds.save_meta({"broken.txt": {"created_at": 1_700_000_000.0}})

        def failing_writer(_chunk):
            raise ConnectionError("client closed")

        with contextlib.suppress(ConnectionError):
            self.ds.stream_once_file(target, failing_writer, chunk_size=2)

        self.assertTrue(target.exists())
        self.assertIn("broken.txt", self.ds.load_meta())
        self.assertIn("interrupted", (self.ds.LOGS_DIR / "once-download.log").read_text(encoding="utf-8"))

    def test_once_route_rejects_head_and_range_without_deleting_file(self):
        target = self.ds.DOWNLOADS_DIR / "route-once.txt"
        target.write_bytes(b"abcdef")
        self.ds.save_meta({"route-once.txt": {"created_at": 1_700_000_000.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(f"{base}/once/route-once.txt", method="HEAD")
            with self.assertRaises(urllib.error.HTTPError) as head_error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(head_error.exception.code, 405)
            self.assertTrue(target.exists())

            range_request = urllib.request.Request(
                f"{base}/once/route-once.txt", headers={"Range": "bytes=0-1"}
            )
            with self.assertRaises(urllib.error.HTTPError) as range_error:
                urllib.request.urlopen(range_request, timeout=3)
            self.assertEqual(range_error.exception.code, 416)
            self.assertTrue(target.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_view_route_renders_image_preview(self):
        target = self.ds.DOWNLOADS_DIR / "picture.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.ds.save_meta({"picture.png": {"created_at": 1_700_000_000.0, "preview_count": 2.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/view/picture.png", timeout=3) as response:
                body = response.read().decode("utf-8")
            self.assertIn("<img", body)
            self.assertIn("/media/picture.png", body)
            self.assertNotIn("<video", body)
            self.assertIn("点击 3 次", body)
            self.assertIn('href="/">返回文件列表</a>', body)
            self.assertNotIn('href="/downloads/"', body)
            self.assertIn('onclick="return confirmOnceDownload(this)"', body)
            self.assertIn('id="danger-confirm-modal"', body)
            self.assertEqual(self.ds.load_meta()["picture.png"]["preview_count"], 3.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_view_route_renders_video_preview(self):
        target = self.ds.DOWNLOADS_DIR / "clip.mp4"
        target.write_bytes(b"0123456789")
        self.ds.save_meta({"clip.mp4": {"created_at": 1_700_000_000.0, "preview_count": 4.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/view/clip.mp4", timeout=3) as response:
                body = response.read().decode("utf-8")
            self.assertIn("<video", body)
            self.assertIn("controls", body)
            self.assertIn("/media/clip.mp4", body)
            self.assertIn("播放/点击 5 次", body)
            self.assertEqual(self.ds.load_meta()["clip.mp4"]["preview_count"], 5.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_media_route_rejects_non_media_files(self):
        target = self.ds.DOWNLOADS_DIR / "data.bin"
        target.write_bytes(b"binary content")
        self.ds.save_meta({"data.bin": {"created_at": 1_700_000_000.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"{base}/media/data.bin", timeout=3)
            self.assertEqual(error.exception.code, 415)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_view_route_renders_text_preview(self):
        target = self.ds.DOWNLOADS_DIR / "notes.txt"
        target.write_text("hello world\nline2", encoding="utf-8")
        self.ds.save_meta({"notes.txt": {"created_at": 1_700_000_000.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            resp = urllib.request.urlopen(f"{base}/view/notes.txt", timeout=3)
            body = resp.read().decode()
            self.assertIn("hello world", body)
            self.assertIn("code-block", body)
            self.assertNotIn("preview_count", self.ds.load_meta()["notes.txt"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_media_route_supports_range_for_video_playback(self):
        target = self.ds.DOWNLOADS_DIR / "range.mp4"
        target.write_bytes(b"0123456789")
        self.ds.save_meta({"range.mp4": {"created_at": 1_700_000_000.0, "preview_count": 7.0}})
        server = self.ds.ThreadingHTTPServer(("127.0.0.1", 0), self.ds.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(f"{base}/media/range.mp4", headers={"Range": "bytes=2-5"})
            with urllib.request.urlopen(request, timeout=3) as response:
                body = response.read()
                status = response.status
                content_range = response.headers.get("Content-Range")
            self.assertEqual(status, 206)
            self.assertEqual(body, b"2345")
            self.assertEqual(content_range, "bytes 2-5/10")
            self.assertEqual(self.ds.load_meta()["range.mp4"]["preview_count"], 7.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_requires_admin_password(self):
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_upload(base, password="wrong", filename="secret.txt", content=b"nope")
            self.assertEqual(error.exception.code, 403)
            self.assertFalse((self.ds.DOWNLOADS_DIR / "secret.txt").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_rejects_invalid_filename(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_upload(base, password=password, filename="../evil.txt", content=b"bad")
            self.assertEqual(error.exception.code, 400)
            self.assertFalse((self.ds.DOWNLOADS_DIR / "evil.txt").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_saves_file_and_meta(self):
        password = self.ds.get_admin_password()
        server, thread, base = self.start_test_server()
        try:
            with self.post_upload(base, password=password, filename="photo.jpg", content=b"image-data") as response:
                body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("photo.jpg", body)
            self.assertEqual((self.ds.DOWNLOADS_DIR / "photo.jpg").read_bytes(), b"image-data")
            self.assertIn("photo.jpg", self.ds.load_meta())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_upload_rejects_existing_file_without_overwrite(self):
        password = self.ds.get_admin_password()
        existing = self.ds.DOWNLOADS_DIR / "same.txt"
        existing.write_text("old", encoding="utf-8")
        server, thread, base = self.start_test_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_upload(base, password=password, filename="same.txt", content=b"new")
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(existing.read_text(encoding="utf-8"), "old")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
