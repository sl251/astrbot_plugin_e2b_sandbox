"""Bounded attachment reads on the AstrBot host."""

import time
import urllib.request
from urllib.parse import urlsplit


def validate_http_url(url):
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Attachment URLs must use HTTP or HTTPS with a host.")


class HTTPOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_http_url(newurl)
        except ValueError:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_limited(stream, max_bytes, deadline=None):
    # read1 returns available HTTP data, allowing deadline checks on slow streams.
    read = getattr(stream, "read1", stream.read)
    content = bytearray()
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Attachment download exceeded its time limit.")
        chunk = read(min(64 * 1024, max_bytes + 1 - len(content)))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Attachment download exceeded its time limit.")
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ValueError(f"Attachment exceeds the upload limit ({max_bytes} bytes).")


def download_http(url, max_bytes):
    validate_http_url(url)
    deadline = time.monotonic() + 30
    opener = urllib.request.build_opener(HTTPOnlyRedirectHandler())
    # Check GET headers directly: HEAD may be unsupported or differ from GET.
    with opener.open(url, timeout=30) as response:
        validate_http_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise ValueError("Invalid attachment Content-Length.") from exc
            if length < 0 or length > max_bytes:
                raise ValueError("Attachment Content-Length exceeds the upload limit or is invalid.")
        content = read_limited(response, max_bytes, deadline)
        if declared is not None and len(content) != length:
            raise ValueError("Attachment download was truncated or Content-Length was incorrect.")
        return content
