"""AstrBot 宿主机侧受限附件读取与防 SSRF 的安全 HTTP(S) 下载实现。"""

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.request
from urllib.parse import urlsplit

# 运营商级 NAT 地址段（同时覆盖阿里云等云厂商 100.100.100.200 实例元数据服务）
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_BLOCKED_EXACT_HOSTS = frozenset({"localhost", "metadata.google.internal", "metadata"})


def validate_public_ip(raw_ip):
    """校验 IP 是否为安全的公网单播地址，拦截环回、私网、链路本地与云元数据地址。"""
    addr = ipaddress.ip_address(raw_ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK)
    ):
        raise ValueError(f"Blocked non-public attachment target IP: {raw_ip}")
    return addr


def validate_http_url(url):
    """校验附件 URL 协议、主机名与字面量 IP，禁止内嵌凭据、非 HTTP(S) 协议及本地/内网主机。"""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Attachment URLs must use HTTP or HTTPS with a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Attachment URLs must not embed user credentials.")
    host = parsed.hostname.rstrip(".").lower()
    if not host or host in _BLOCKED_EXACT_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError("Attachment URL host points to a restricted local or internal domain.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    validate_public_ip(host)


def resolve_public_targets(hostname, port):
    """解析主机名并校验所有解析结果均为公网单播 IP，防止域名指向内网或云元数据。"""
    validate_http_url(f"https://{hostname}")
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve attachment host: {hostname}") from exc
    if not infos:
        raise ValueError(f"Attachment host resolved to no addresses: {hostname}")
    checked = []
    for family, socktype, proto, canonname, sockaddr in infos:
        validate_public_ip(sockaddr[0])
        checked.append((family, socktype, proto, canonname, sockaddr))
    return checked


def _connect_verified_socket(hostname, port, timeout, source_address=None):
    """直接连接通过公网校验的解析结果，阻断先返回公网 IP 再切换内网 IP 的 DNS Rebinding。"""
    infos = resolve_public_targets(hostname, port)
    last_error = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error or OSError(f"Failed to connect to {hostname}:{port}")


class SafeHTTPConnection(http.client.HTTPConnection):
    """绑定公网 IP 校验的 HTTP 连接器。"""

    def connect(self):
        self.sock = _connect_verified_socket(
            self.host, self.port, self.timeout, self.source_address
        )


class SafeHTTPSConnection(http.client.HTTPSConnection):
    """绑定公网 IP 校验且保留原始域名 TLS SNI 证书校验的 HTTPS 连接器。"""

    def connect(self):
        raw_sock = _connect_verified_socket(
            self.host, self.port, self.timeout, self.source_address
        )
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=server_hostname)


class SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(SafeHTTPConnection, req)


class SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context=None):
        super().__init__(context=context)
        self._ssl_context = context or ssl.create_default_context()

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: SafeHTTPSConnection(host, context=self._ssl_context, **kwargs),
            req,
        )


class HTTPOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """限制重定向目标只能使用公网 HTTP(S) 地址，防止 302 跳转探测本机或内网。"""

    def __init__(self, verify_dns=False):
        super().__init__()
        self._verify_dns = verify_dns

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_http_url(newurl)
            if self._verify_dns:
                parsed = urlsplit(newurl)
                port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
                resolve_public_targets(parsed.hostname, port)
        except ValueError:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_limited(stream, max_bytes, deadline=None):
    """按块受限读取字节流，防止慢速连接拖延或声明虚假 Content-Length 撑爆宿主机内存。"""
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


def download_http(url, max_bytes, proxy=None):
    """安全下载公网 HTTP(S) 附件并校验响应体积完整性。"""
    validate_http_url(url)
    deadline = time.monotonic() + 30
    clean_proxy = str(proxy or "").strip()
    if clean_proxy:
        # 走显式代理时连接的目标是代理端口，需预先解析校验目标 URL 及重定向目标的公网域名/IP
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        resolve_public_targets(parsed.hostname, port)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": clean_proxy, "https": clean_proxy}),
            HTTPOnlyRedirectHandler(verify_dns=True),
        )
    else:
        # 直连禁用环境变量隐式代理，并启用连接层公网 IP 绑定以防御 DNS Rebinding
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            HTTPOnlyRedirectHandler(verify_dns=False),
            SafeHTTPHandler(),
            SafeHTTPSHandler(),
        )
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
