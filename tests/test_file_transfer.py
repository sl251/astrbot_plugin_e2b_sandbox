import io
import types
import unittest
import urllib.request
from collections import defaultdict
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from helpers import Event, TestDirectory, load_main

main = load_main()
from sandbox_plugin_test import file_transfer as transfer


class Response(io.BytesIO):
    def __init__(self, data=b"", headers=None, url="https://files.example/a"):
        super().__init__(data)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


class DownloadTests(unittest.TestCase):
    def download(self, response, limit=4):
        opener = Mock()
        opener.open.return_value = response
        with patch.object(transfer.urllib.request, "build_opener", return_value=opener):
            return transfer.download_http("https://files.example/a", limit)

    def test_rejects_non_http_before_opening(self):
        with patch.object(transfer.urllib.request, "build_opener") as opener:
            for url in ("file:///etc/passwd", "ftp://files.example/a", "data:text/plain,secret", "https:///a"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    transfer.download_http(url, 10)
            opener.assert_not_called()

    def test_redirects_cannot_escape_http(self):
        handler = transfer.HTTPOnlyRedirectHandler()
        request = urllib.request.Request("https://files.example/a")
        for url in ("file:///etc/passwd", "ftp://files.example/a", "data:text/plain,secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/b")
        self.assertEqual(redirected.full_url, "https://cdn.example/b")

    def test_oversize_header_rejected_before_read(self):
        response = Response(b"data", {"Content-Length": "5"})
        response.read1 = Mock(side_effect=AssertionError("must not read"))
        with self.assertRaises(ValueError):
            self.download(response)
        self.assertTrue(response.closed)

    def test_missing_or_inaccurate_length_cannot_bypass_limit(self):
        for headers in ({}, {"Content-Length": "1"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.download(Response(b"12345", headers))

    def test_exact_limit_and_empty_attachment_are_allowed(self):
        self.assertEqual(self.download(Response(b"1234", {"Content-Length": "4"})), b"1234")
        self.assertEqual(self.download(Response()), b"")

    def test_invalid_and_truncated_lengths_are_rejected(self):
        for length in ("-1", "invalid", "4"):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.download(Response(b"x", {"Content-Length": length}))

    def test_slow_download_stops_at_deadline(self):
        with patch.object(transfer.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                transfer.read_limited(io.BytesIO(b"1234"), 4, deadline=30)


class FileBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.Main.__new__(main.Main)
        self.plugin.config = {"max_upload_file_size_mb": 1}

    async def test_binary_sdk_read_preserves_all_bytes_without_shell(self):
        payload = bytes(range(256)) * 4096
        for data in (payload, bytearray(payload), memoryview(payload)):
            sandbox = types.SimpleNamespace(files=types.SimpleNamespace(read=AsyncMock(return_value=data)))
            self.assertEqual(await self.plugin._read_sandbox_file_bytes(sandbox, "/home/user/a.xlsx"), payload)
            sandbox.files.read.assert_awaited_once_with("/home/user/a.xlsx", format="bytes", request_timeout=30)

    async def test_text_sdk_response_is_not_silently_encoded(self):
        sandbox = types.SimpleNamespace(files=types.SimpleNamespace(read=AsyncMock(return_value="broken")))
        with self.assertRaises(TypeError):
            await self.plugin._read_sandbox_file_bytes(sandbox, "/home/user/a")

    async def test_bot_api_fallback_cannot_read_file_url(self):
        self.plugin._get_file_url_from_bot = AsyncMock(return_value="file:///etc/passwd")
        with self.assertRaises(ValueError):
            await self.plugin._resolve_file_payload(Event(), {"name": "a.txt", "file_id": "id"})

    async def test_read_errors_propagate(self):
        sandbox = types.SimpleNamespace(files=types.SimpleNamespace(read=AsyncMock(side_effect=TimeoutError)))
        with self.assertRaises(TimeoutError):
            await self.plugin._read_sandbox_file_bytes(sandbox, "/home/user/a")

    async def test_changed_generated_file_is_not_cached(self):
        path = "/home/user/a.csv"
        self.plugin._snapshot_sandbox_files = AsyncMock(return_value={path: {"size": 4, "mtime": 1}})
        self.plugin.sent_file_signatures = defaultdict(set)
        sandbox = types.SimpleNamespace(
            commands=types.SimpleNamespace(run=AsyncMock(return_value=types.SimpleNamespace(stdout="4"))),
            files=types.SimpleNamespace(read=AsyncMock(return_value=b"changed")),
        )
        result = await self.plugin._collect_generated_files(sandbox, [], [], "session", {})
        self.assertEqual(result, [])

    def test_local_attachment_size_is_bounded(self):
        directory = TestDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "input.bin"
        path.write_bytes(b"x" * (1024 * 1024))
        self.assertEqual(len(self.plugin._read_local_file(str(path))), 1024 * 1024)
        with path.open("ab") as output:
            output.write(b"x")
        with self.assertRaises(ValueError):
            self.plugin._read_local_file(str(path))
        with self.assertRaises(ValueError):
            self.plugin._read_local_file(directory.name)

    def test_attachment_names_stay_inside_upload_directory(self):
        for name in ("../../etc/report.csv", "/home/user/report.csv", r"C:\temp\report.csv", "report.csv"):
            self.assertEqual(self.plugin._resolve_remote_path(name), "/home/user/uploads/report.csv")
        for name in ("", "/", "..", "a/..", "a\x00.csv"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.plugin._resolve_remote_path(name)

    def test_group_members_still_share_session(self):
        first, second = Event("a"), Event("b")
        first.unified_msg_origin = second.unified_msg_origin = "test:group:shared"
        self.assertEqual(self.plugin._get_session_id(first), self.plugin._get_session_id(second))
