"""프록시가 실제로 어떤 출구 IP 를 주는지 확인한다.

왜 필요한가
    레지덴셜은 "고정 IP" 가 아니라 sticky 다. 공급사가 세션 토큰을 얼마나
    지켜주는지는 문서가 아니라 측정으로만 알 수 있다. 이 모듈은 묶음마다
    프록시를 통해 IP 에코 서비스를 찔러 보고, 실제로 나온 출구 IP 를 기록한다.

    같은 묶음을 시간 간격을 두고 여러 번 재보면 sticky 가 유지되는지 알 수
    있다. **이 확인 없이 운영에 들어가면 안 된다** — 세션이 안 지켜지면
    "묶음은 늘 같은 IP" 라는 전제 자체가 성립하지 않는다.

의존성 없이 SOCKS5 를 직접 말한다
    표준 라이브러리에는 SOCKS 클라이언트가 없다. RFC1928(연결) 과
    RFC1929(사용자/비밀번호 인증)만 쓰면 되므로 직접 구현한다.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

# 출구 IP 를 돌려주는 외부 서비스. 자체 서비스의 접속 로그로 확인하는 편이
# 가장 정확하므로, 가능하면 --echo-url 로 본인 엔드포인트를 지정할 것.
DEFAULT_ECHO_URL = "http://api.ipify.org/"
DEFAULT_TIMEOUT = 15.0

_SOCKS5_ERRORS = {
    0x01: "일반 실패",
    0x02: "규칙에 의해 거부됨",
    0x03: "네트워크 도달 불가",
    0x04: "호스트 도달 불가",
    0x05: "연결 거부됨",
    0x06: "TTL 만료",
    0x07: "지원하지 않는 명령",
    0x08: "지원하지 않는 주소 형식",
}


class ProxyCheckError(RuntimeError):
    """프록시 확인 실패."""


@dataclass(frozen=True)
class CheckResult:
    """묶음 하나의 확인 결과."""

    group_id: str
    ok: bool
    egress_ip: str = ""
    error: str = ""
    elapsed_ms: int = 0

    def line(self) -> str:
        if self.ok:
            return f"{self.group_id}  {self.egress_ip:<16} {self.elapsed_ms:>5} ms"
        return f"{self.group_id}  실패 — {self.error}"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyCheckError("프록시가 응답 중에 연결을 끊었다.")
        buf += chunk
    return buf


def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    *,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> socket.socket:
    """SOCKS5 로 목적지까지 터널을 열고 그 소켓을 돌려준다 (RFC1928/1929)."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        # 인사: 인증 방식 제시
        methods = b"\x00\x02" if username else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 0x05:
            raise ProxyCheckError(f"SOCKS5 가 아니다 (버전 {ver}). HTTP 프록시를 SOCKS 로 지정하지 않았는지 볼 것.")
        if method == 0xFF:
            raise ProxyCheckError("프록시가 제시한 인증 방식을 모두 거부했다. 사용자명/비밀번호를 확인할 것.")

        if method == 0x02:
            if not username:
                raise ProxyCheckError("프록시가 인증을 요구하는데 사용자명이 없다.")
            u = username.encode()
            p = password.encode()
            if len(u) > 255 or len(p) > 255:
                raise ProxyCheckError("사용자명/비밀번호가 255바이트를 넘는다.")
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _, status = _recv_exact(sock, 2)
            if status != 0x00:
                raise ProxyCheckError("프록시 인증에 실패했다. sticky 사용자명 형식이 공급사 규칙과 맞는지 볼 것.")
        elif method != 0x00:
            raise ProxyCheckError(f"프록시가 지원하지 않는 인증 방식을 골랐다 ({method}).")

        # 연결 요청 — 도메인을 그대로 넘겨 DNS 를 프록시 쪽에서 풀게 한다.
        host = dest_host.encode()
        if len(host) > 255:
            raise ProxyCheckError("목적지 호스트명이 255바이트를 넘는다.")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", dest_port))
        ver, rep, _, atyp = _recv_exact(sock, 4)
        if rep != 0x00:
            raise ProxyCheckError(f"프록시가 연결을 거절했다: {_SOCKS5_ERRORS.get(rep, f'코드 {rep}')}")
        # 바인드 주소를 읽어 버려야 이후 바이트가 응답 본문과 섞이지 않는다.
        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x03:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        else:
            raise ProxyCheckError(f"프록시 응답의 주소 형식을 모른다 ({atyp}).")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def fetch_through_socks5(
    proxy_host: str,
    proxy_port: int,
    url: str,
    *,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 8192,
) -> str:
    """SOCKS5 를 통해 URL 을 GET 하고 본문을 돌려준다.

    HTTPS 는 지원하지 않는다 — 출구 IP 확인에는 평문으로 충분하고, 터널 위에
    TLS 를 다시 올리면 표준 라이브러리만으로는 복잡해진다.
    """
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise ProxyCheckError(f"http:// URL 만 지원한다 (받은 값: {url}).")
    if not parts.hostname:
        raise ProxyCheckError(f"URL 에서 호스트를 읽을 수 없다: {url}")
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    sock = socks5_connect(proxy_host, proxy_port, parts.hostname, port, username=username, password=password, timeout=timeout)
    try:
        request = f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}\r\nUser-Agent: ipalloc-proxycheck\r\n" "Accept: */*\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode())
        chunks: list[bytes] = []
        total = 0
        while total < max_bytes:
            chunk = sock.recv(min(4096, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in status:
        raise ProxyCheckError(f"에코 서비스가 200 을 주지 않았다: {status}")
    return body.decode("utf-8", "replace").strip()


def check_group(
    group_id: str,
    proxy_host: str,
    proxy_port: int,
    *,
    username: str = "",
    password: str = "",
    echo_url: str = DEFAULT_ECHO_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> CheckResult:
    """묶음 하나의 실제 출구 IP 를 확인한다. 예외를 던지지 않고 결과로 감싼다."""
    import time

    started = time.monotonic()
    try:
        body = fetch_through_socks5(proxy_host, proxy_port, echo_url, username=username, password=password, timeout=timeout)
    except (ProxyCheckError, OSError) as exc:
        return CheckResult(group_id=group_id, ok=False, error=str(exc), elapsed_ms=int((time.monotonic() - started) * 1000))
    return CheckResult(
        group_id=group_id,
        ok=True,
        egress_ip=body.splitlines()[0].strip() if body else "(빈 응답)",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def summarize(results: list[CheckResult]) -> list[str]:
    """결과를 훑어 운영상 문제를 뽑아낸다."""
    out: list[str] = []
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    if failed:
        out.append(f"확인 실패 {len(failed)}개 / 전체 {len(results)}개")
    if not ok:
        return out or ["확인된 묶음이 없다."]

    by_ip: dict[str, list[str]] = {}
    for r in ok:
        by_ip.setdefault(r.egress_ip, []).append(r.group_id)
    shared = {ip: gids for ip, gids in by_ip.items() if len(gids) > 1}
    if shared:
        out.append(f"서로 다른 묶음이 같은 출구 IP 를 쓰고 있다 ({len(shared)}건) — sticky 세션이 " "묶음별로 갈리지 않았다는 뜻이다:")
        for ip, gids in sorted(shared.items())[:5]:
            out.append(f"  {ip} ← {', '.join(sorted(gids))}")
    out.append(f"고유 출구 IP {len(by_ip)}개 / 확인 성공 {len(ok)}개")
    return out


def compare_runs(before: list[CheckResult], after: list[CheckResult]) -> list[str]:
    """두 번의 확인을 비교해 sticky 가 유지됐는지 본다."""
    b = {r.group_id: r.egress_ip for r in before if r.ok}
    a = {r.group_id: r.egress_ip for r in after if r.ok}
    common = sorted(set(b) & set(a))
    if not common:
        return ["두 번 모두 성공한 묶음이 없어 비교할 수 없다."]
    changed = [g for g in common if b[g] != a[g]]
    out = [f"비교 대상 {len(common)}개 · 유지 {len(common) - len(changed)}개 · 변경 {len(changed)}개"]
    for g in changed[:10]:
        out.append(f"  {g}: {b[g]} → {a[g]}")
    if changed:
        out.append("출구가 바뀐 묶음이 있다. sticky 세션 유효시간이 측정 간격보다 짧다는 뜻이다.")
    return out


__all__ = [
    "DEFAULT_ECHO_URL",
    "DEFAULT_TIMEOUT",
    "CheckResult",
    "ProxyCheckError",
    "check_group",
    "compare_runs",
    "fetch_through_socks5",
    "socks5_connect",
    "summarize",
]
