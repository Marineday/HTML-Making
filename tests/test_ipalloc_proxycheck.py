"""ipalloc.proxycheck 검증.

직접 구현한 SOCKS5 클라이언트라 실제 소켓으로 종단 검증한다. 브리지 서버
테스트와 같은 방식이다 — 프로토콜 구현은 흉내로 검증하면 의미가 없다.
"""

from __future__ import annotations

import socket
import struct
import threading
import unittest

from ipalloc.proxycheck import (
    CheckResult,
    ProxyCheckError,
    check_group,
    compare_runs,
    fetch_through_socks5,
    socks5_connect,
    summarize,
)


class FakeSocks5Server:
    """테스트용 SOCKS5 서버.

    실제로 목적지에 연결하지 않고, CONNECT 가 오면 미리 정해둔 HTTP 응답을
    돌려준다. 출구 IP 를 테스트마다 다르게 줄 수 있다.
    """

    def __init__(self, *, egress_ip: str = "198.51.100.7", require_auth: bool = False, password: str = "pw", reply_code: int = 0x00) -> None:
        self.egress_ip = egress_ip
        self.require_auth = require_auth
        self.password = password
        self.reply_code = reply_code
        self.seen_usernames: list[str] = []
        self.seen_destinations: list[tuple[str, int]] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        # accept() 는 다른 스레드에서 소켓을 닫아도 깨어나지 않는다. 타임아웃을
        # 걸어 주기적으로 정지 플래그를 보게 해야 close() 가 즉시 끝난다.
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(5)
            try:
                self._handle(conn)
            except (OSError, IndexError, struct.error):
                pass
            finally:
                conn.close()

    def _recv(self, conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def _handle(self, conn: socket.socket) -> None:
        ver, nmethods = self._recv(conn, 2)
        methods = self._recv(conn, nmethods)
        if self.require_auth:
            if 0x02 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x02")
            self._recv(conn, 1)  # 서브협상 버전
            ulen = self._recv(conn, 1)[0]
            username = self._recv(conn, ulen).decode()
            plen = self._recv(conn, 1)[0]
            password = self._recv(conn, plen).decode()
            self.seen_usernames.append(username)
            if password != self.password:
                conn.sendall(b"\x01\x01")
                return
            conn.sendall(b"\x01\x00")
        else:
            conn.sendall(b"\x05\x00")

        self._recv(conn, 3)  # ver, cmd, rsv
        atyp = self._recv(conn, 1)[0]
        if atyp == 0x03:
            length = self._recv(conn, 1)[0]
            host = self._recv(conn, length).decode()
        else:
            host = socket.inet_ntoa(self._recv(conn, 4))
        port = struct.unpack("!H", self._recv(conn, 2))[0]
        self.seen_destinations.append((host, port))

        conn.sendall(b"\x05" + bytes([self.reply_code]) + b"\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 0))
        if self.reply_code != 0x00:
            return

        self._recv_request(conn)
        body = self.egress_ip.encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    def _recv_request(self, conn: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


class Socks5Test(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeSocks5Server()
        self.addCleanup(self.server.close)

    def test_fetch_without_auth(self) -> None:
        body = fetch_through_socks5("127.0.0.1", self.server.port, "http://example.com/ip", timeout=5)
        self.assertEqual(body, "198.51.100.7")
        self.assertEqual(self.server.seen_destinations[0], ("example.com", 80))

    def test_domain_is_passed_through_not_resolved_locally(self) -> None:
        # 목적지를 도메인으로 넘겨야 DNS 가 프록시 쪽에서 풀린다.
        fetch_through_socks5("127.0.0.1", self.server.port, "http://target.example.org:8080/x", timeout=5)
        self.assertEqual(self.server.seen_destinations[0], ("target.example.org", 8080))

    def test_rejects_https(self) -> None:
        with self.assertRaises(ProxyCheckError):
            fetch_through_socks5("127.0.0.1", self.server.port, "https://example.com/", timeout=5)

    def test_connection_refused_by_proxy(self) -> None:
        server = FakeSocks5Server(reply_code=0x05)
        self.addCleanup(server.close)
        with self.assertRaises(ProxyCheckError) as ctx:
            fetch_through_socks5("127.0.0.1", server.port, "http://example.com/", timeout=5)
        self.assertIn("연결 거부됨", str(ctx.exception))

    def test_socket_is_closed_on_failure(self) -> None:
        server = FakeSocks5Server(reply_code=0x03)
        self.addCleanup(server.close)
        with self.assertRaises(ProxyCheckError):
            socks5_connect("127.0.0.1", server.port, "example.com", 80, timeout=5)


class AuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeSocks5Server(require_auth=True, password="hunter2")
        self.addCleanup(self.server.close)

    def test_sends_username_and_password(self) -> None:
        body = fetch_through_socks5(
            "127.0.0.1", self.server.port, "http://example.com/", username="cust-session-abc123", password="hunter2", timeout=5
        )
        self.assertEqual(body, "198.51.100.7")
        self.assertEqual(self.server.seen_usernames, ["cust-session-abc123"])

    def test_wrong_password_is_reported(self) -> None:
        with self.assertRaises(ProxyCheckError) as ctx:
            fetch_through_socks5("127.0.0.1", self.server.port, "http://example.com/", username="u", password="wrong", timeout=5)
        self.assertIn("인증에 실패", str(ctx.exception))

    def test_missing_credentials_is_reported(self) -> None:
        with self.assertRaises(ProxyCheckError):
            fetch_through_socks5("127.0.0.1", self.server.port, "http://example.com/", timeout=5)


class CheckGroupTest(unittest.TestCase):
    def test_wraps_success(self) -> None:
        server = FakeSocks5Server(egress_ip="203.0.113.55")
        self.addCleanup(server.close)
        result = check_group("g001", "127.0.0.1", server.port, echo_url="http://example.com/", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.egress_ip, "203.0.113.55")
        self.assertIn("203.0.113.55", result.line())

    def test_wraps_failure_without_raising(self) -> None:
        # 묶음 하나가 죽어도 나머지 확인은 계속되어야 한다.
        result = check_group("g001", "127.0.0.1", 1, echo_url="http://example.com/", timeout=2)
        self.assertFalse(result.ok)
        self.assertIn("실패", result.line())


class SummarizeTest(unittest.TestCase):
    def test_flags_groups_sharing_an_exit(self) -> None:
        # sticky 세션이 묶음별로 갈리지 않으면 이 증상이 나온다.
        results = [
            CheckResult("g001", True, "198.51.100.1"),
            CheckResult("g002", True, "198.51.100.1"),
            CheckResult("g003", True, "198.51.100.9"),
        ]
        text = " ".join(summarize(results))
        self.assertIn("같은 출구 IP", text)
        self.assertIn("g001, g002", text)
        self.assertIn("고유 출구 IP 2개", text)

    def test_counts_failures(self) -> None:
        results = [CheckResult("g001", True, "198.51.100.1"), CheckResult("g002", False, error="timeout")]
        self.assertIn("확인 실패 1개", " ".join(summarize(results)))

    def test_all_failed(self) -> None:
        self.assertIn("확인 실패", " ".join(summarize([CheckResult("g001", False, error="x")])))


class CompareRunsTest(unittest.TestCase):
    def test_detects_rotation(self) -> None:
        before = [CheckResult("g001", True, "198.51.100.1"), CheckResult("g002", True, "198.51.100.2")]
        after = [CheckResult("g001", True, "198.51.100.1"), CheckResult("g002", True, "198.51.100.99")]
        text = " ".join(compare_runs(before, after))
        self.assertIn("유지 1개", text)
        self.assertIn("변경 1개", text)
        self.assertIn("198.51.100.2 → 198.51.100.99", text)
        self.assertIn("유효시간이 측정 간격보다 짧다", text)

    def test_all_stable(self) -> None:
        runs = [CheckResult("g001", True, "198.51.100.1")]
        text = " ".join(compare_runs(runs, runs))
        self.assertIn("변경 0개", text)
        self.assertNotIn("짧다", text)

    def test_nothing_comparable(self) -> None:
        self.assertIn("비교할 수 없다", " ".join(compare_runs([CheckResult("g001", False)], [CheckResult("g002", False)])))


if __name__ == "__main__":
    unittest.main()
