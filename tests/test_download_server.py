import contextlib
import importlib
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
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

    def test_custom_filename_validation_accepts_allowed_characters(self):
        name = "中文 File_01-测试.txt"
        self.assertEqual(self.ds.validate_custom_filename(name), name)

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


if __name__ == "__main__":
    unittest.main()
