"""SSRF 防护：解析校验 + IP 直连 + SNI 固定。

策略（规格 13.3.3）：
- 拒绝环回（除非显式 allow_loopback，仅测试用）、RFC1918 私网、
  链路本地（含云元数据 169.254.169.254）、ULA、组播、保留段、非 http(s)；
- 在 **connect 时刻** 连接经过校验的 IP（重写 URL 主机 + ``sni_hostname``
  扩展固定 TLS SNI 与证书校验主机名），消除 DNS rebinding TOCTOU；
- 重定向不自动跟随，逐跳重新校验；
- 响应大小与超时受限。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field

import httpx

USER_AGENT = "learning-wiki/0.1 (personal knowledge capture)"


class SSRFBlockedError(Exception):
    def __init__(self, host: str, reason: str) -> None:
        self.host = host
        self.reason = reason
        super().__init__(f"SSRF 防护拦截 {host}: {reason}")


class CaptureBlockedError(Exception):
    """受限网页（登录墙/反爬/验证码/付费墙）的可解释降级。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PermanentCaptureError(CaptureBlockedError):
    """平台适配器已确认该来源**不可自动提取**，通用兜底无意义的失败。

    与基类的区别：基类表示"我这条路没走通，也许通用提取还能救"，
    本类表示"我认得这个来源，缺的是凭证（xsec_token / 登录态），
    换通用提取只会拿到 JS 空壳，必然失败"。

    接入层降级链对本类直接上抛，把可行动的原因原样交给用户，
    而不是退化成一句无信息量的"无法提取正文"。
    """


def block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """返回该 IP 应被拦截的原因；None 表示放行。"""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return block_reason(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return block_reason(ip.sixtofour)
        if ip.teredo is not None:
            inner = ipaddress.ip_address(ip.teredo[1])
            return block_reason(inner) or "teredo 隧道"
    if ip.is_loopback:
        return "环回地址"
    if ip.is_private:
        return "私网地址"
    if ip.is_link_local:
        return "链路本地地址（含云元数据 169.254.169.254）"
    if ip.is_multicast:
        return "组播地址"
    if ip.is_reserved:
        return "保留地址"
    if ip.is_unspecified:
        return "未指定地址"
    return None


@dataclass
class FetchedPage:
    url: str  # 最终 URL（跟随重定向后）
    status: int
    headers: dict[str, str]
    content: bytes
    history: list[str] = field(default_factory=list)


class GuardedFetcher:
    def __init__(
        self,
        *,
        allow_loopback: bool = False,
        timeout: float = 30.0,
        max_bytes: int = 50 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        self.allow_loopback = allow_loopback
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    # -- 校验 ---------------------------------------------------------------

    def _validate_host(self, host: str) -> str:
        """校验主机名/字面 IP；返回可安全连接的 IP 字符串。"""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            reason = block_reason(literal)
            if reason == "环回地址" and self.allow_loopback:
                return str(literal)
            if reason:
                raise SSRFBlockedError(host, reason)
            return str(literal)
        # 域名：解析出的**所有**记录都必须通过校验（防多记录混淆）
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise SSRFBlockedError(host, f"域名解析失败: {exc}") from exc
        validated: list[str] = []
        for info in infos:
            addr = str(info[4][0])
            ip = ipaddress.ip_address(addr)
            reason = block_reason(ip)
            if reason == "环回地址" and self.allow_loopback:
                pass
            elif reason:
                raise SSRFBlockedError(host, f"解析记录 {addr}: {reason}")
            if addr not in validated:
                validated.append(addr)
        if not validated:
            raise SSRFBlockedError(host, "无可用解析记录")
        return validated[0]

    # -- 抓取 ---------------------------------------------------------------

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchedPage:
        current = url
        history: list[str] = []
        for _ in range(self.max_redirects + 1):
            page, redirect = self._fetch_once(current, headers)
            if redirect is None:
                page.history = history
                return page
            history.append(current)
            current = str(httpx.URL(current).join(redirect))
        raise CaptureBlockedError(f"重定向超过 {self.max_redirects} 次")

    def _fetch_once(
        self, url: str, headers: dict[str, str] | None = None
    ) -> tuple[FetchedPage, str | None]:
        u = httpx.URL(url)
        if u.scheme not in ("http", "https"):
            raise SSRFBlockedError(url, f"不允许的 scheme: {u.scheme}")
        if u.host is None:
            raise SSRFBlockedError(url, "URL 无主机")
        ip = self._validate_host(u.host)

        # 重写主机为已校验 IP，SNI/Host 保持原主机名（connect 时刻保证）
        pinned = u.copy_with(host=ip)
        # 注意：u.port 在未显式写端口时为 None，不能拼进 Host（否则畸形头被拒）
        netloc = u.host if u.port is None else f"{u.host}:{u.port}"
        transport = httpx.HTTPTransport(trust_env=False, retries=0)
        with httpx.Client(
            transport=transport,
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            try:
                with client.stream(
                    "GET",
                    pinned,
                    headers={"User-Agent": USER_AGENT, "Host": netloc, **(headers or {})},
                    extensions={"sni_hostname": u.host},
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        return (
                            FetchedPage(
                                url=str(u), status=resp.status_code, headers={}, content=b""
                            ),
                            resp.headers.get("location"),
                        )
                    length = int(resp.headers.get("content-length", 0) or 0)
                    if length > self.max_bytes:
                        raise CaptureBlockedError(f"响应过大: {length} > {self.max_bytes}")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in resp.iter_bytes(65536):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise CaptureBlockedError(f"响应超过大小上限 {self.max_bytes}")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    return (
                        FetchedPage(
                            url=str(u),
                            status=resp.status_code,
                            headers=dict(resp.headers),
                            content=content,
                        ),
                        None,
                    )
            except httpx.HTTPError as exc:
                raise CaptureBlockedError(f"抓取失败: {exc}") from exc
