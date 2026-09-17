"""ipalloc.proxychain 검증.

이 경로에서 조용히 망가지기 쉬운 것 세 가지를 중점적으로 본다.
    1. 묶음별 iptables REDIRECT 포트가 서로 겹치지 않을 것
    2. QUIC(UDP 443) 이 reject 될 것 — 안 그러면 영상이 안 나온다
    3. 생성물에 평문 비밀번호가 없을 것
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from ipalloc.allocator import allocate
from ipalloc.models import User
from ipalloc.pool import TunnelPlan, endpoints_from_cidr
from ipalloc.proxy import ProxyEndpoint, session_id, sticky_username
from ipalloc.proxychain import (
    ChainOptions,
    export,
    render_fill_secrets_sh,
    render_iptables_sh,
    render_redsocks_conf,
    render_wg_server,
)


def _alloc(count: int = 45, group_size: int = 20):
    return allocate(
        [User(id=f"u{i:03d}") for i in range(1, count + 1)],
        endpoints_from_cidr("203.0.113.0/28"),
        group_size=group_size,
    )


def _proxies(n: int) -> list[ProxyEndpoint]:
    return [
        ProxyEndpoint(host=f"gw{i}.proxy.example.com", port=8000 + i, username="customer", password="${PROXY_PW}", region="kr")
        for i in range(1, n + 1)
    ]


class ChainOptionsTest(unittest.TestCase):
    def test_requires_an_entry_host(self) -> None:
        with self.assertRaises(ValueError):
            ChainOptions(entry_host="  ")

    def test_ports_walk_upward(self) -> None:
        opts = ChainOptions(entry_host="vpn.example.com", redsocks_base_port=12000)
        self.assertEqual(opts.redsocks_port(0), 12000)
        self.assertEqual(opts.redsocks_port(49), 12049)

    def test_rejects_port_overflow(self) -> None:
        opts = ChainOptions(entry_host="vpn.example.com", redsocks_base_port=60000)
        with self.assertRaises(ValueError):
            opts.redsocks_port(6000)


class WgServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = _alloc()
        self.opts = ChainOptions(entry_host="vpn.example.com", entry_port=51820)

    def test_single_interface_holds_every_peer(self) -> None:
        conf = render_wg_server(self.alloc, TunnelPlan(), self.opts)
        self.assertEqual(conf.count("[Interface]"), 1)
        self.assertEqual(conf.count("[Peer]"), 45)
        self.assertIn("Address = 10.77.0.1/16", conf)

    def test_no_snat_because_the_proxy_decides_the_exit(self) -> None:
        conf = render_wg_server(self.alloc, TunnelPlan(), self.opts)
        self.assertNotIn("SNAT", conf)

    def test_every_peer_gets_a_host_route(self) -> None:
        conf = render_wg_server(self.alloc, TunnelPlan(), self.opts)
        for m in self.alloc.assignments:
            self.assertIn(f"AllowedIPs = {m.tunnel_ip}/32", conf)


class RedsocksConfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = _alloc()
        self.group = self.alloc.used_groups[0]
        self.proxy = _proxies(1)[0]

    def test_password_is_a_placeholder(self) -> None:
        conf = render_redsocks_conf(self.group, 12000, self.proxy)
        self.assertIn(f'password = "__PROXY_PASSWORD_{self.group.id}__"', conf)
        self.assertNotIn("${PROXY_PW}", conf)

    def test_login_carries_the_groups_sticky_session(self) -> None:
        conf = render_redsocks_conf(self.group, 12000, self.proxy)
        expected = sticky_username(self.proxy, self.group.id)
        self.assertIn(f'login = "{expected}";', conf)
        self.assertIn(session_id(self.group.id), conf)

    def test_salt_changes_the_session(self) -> None:
        a = render_redsocks_conf(self.group, 12000, self.proxy)
        b = render_redsocks_conf(self.group, 12000, self.proxy, salt="v2")
        self.assertNotEqual(a, b)

    def test_upstream_and_local_port(self) -> None:
        conf = render_redsocks_conf(self.group, 12345, self.proxy)
        self.assertIn("local_port = 12345;", conf)
        self.assertIn("ip = gw1.proxy.example.com;", conf)
        self.assertIn("port = 8001;", conf)
        self.assertIn("type = socks5;", conf)

    def test_no_auth_proxy_omits_credentials(self) -> None:
        anon = ProxyEndpoint(host="open.example.com", port=1080)
        conf = render_redsocks_conf(self.group, 12000, anon)
        self.assertNotIn("login", conf)
        self.assertNotIn("password", conf)


class IptablesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = _alloc()
        self.script = render_iptables_sh(self.alloc, ChainOptions(entry_host="vpn.example.com"))

    def test_one_redirect_per_group_with_distinct_ports(self) -> None:
        ports = re.findall(r"--to-ports (\d+)", self.script)
        self.assertEqual(len(ports), len(self.alloc.used_groups))
        self.assertEqual(len(set(ports)), len(ports))

    def test_each_group_subnet_maps_to_its_own_port(self) -> None:
        for index, group in enumerate(self.alloc.used_groups):
            self.assertIn(f"-s {group.subnet} -p tcp -j REDIRECT --to-ports {12000 + index}", self.script)

    def test_quic_is_rejected_not_dropped(self) -> None:
        # drop 하면 클라이언트가 타임아웃을 기다리느라 재생이 더 늦어진다.
        self.assertIn("--dport 443 -j REJECT", self.script)
        self.assertNotIn("--dport 443 -j DROP", self.script)

    def test_dns_bypasses_the_proxy(self) -> None:
        self.assertIn("--dport 53 -j RETURN", self.script)

    def test_private_destinations_bypass_the_proxy(self) -> None:
        for net in ("10.0.0.0/8", "192.168.0.0/16", "127.0.0.0/8"):
            self.assertIn(f"-d {net} -j RETURN", self.script)

    def test_supports_teardown(self) -> None:
        self.assertIn('ACTION="${1:-up}"', self.script)
        self.assertIn('elif [ "$ACTION" = "down" ]', self.script)

    def test_teardown_continues_past_missing_rules(self) -> None:
        # set -e 아래에서 규칙 하나가 없다고 멈추면 절반만 지워진 상태가 남는다.
        self.assertIn('iptables "$@" 2>/dev/null || true', self.script)

    def test_up_clears_existing_rules_first(self) -> None:
        # 두 번 적용하면 REDIRECT 가 중복으로 쌓인다.
        self.assertIn('sh "$0" down >/dev/null 2>&1 || true', self.script)


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "proxy"
        self.addCleanup(self._tmp.cleanup)
        self.alloc = _alloc()
        self.opts = ChainOptions(entry_host="vpn.example.com")

    def test_file_layout(self) -> None:
        counts = export(self.alloc, _proxies(3), self.out, opts=self.opts)
        self.assertEqual(counts, {"groups": 3, "clients": 45})
        for name in ("wg0.conf", "peers.csv", "genkeys.sh", "fill-secrets.sh", "iptables.sh"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertEqual(len(list((self.out / "redsocks").glob("*.conf"))), 3)
        self.assertEqual(len(list((self.out / "clients" / "g001").glob("*.conf"))), 20)

    def test_every_client_points_at_the_single_entry_host(self) -> None:
        # 진입 IP 를 묶음 수만큼 사지 않아도 되는 것이 이 구성의 요점이다.
        export(self.alloc, _proxies(3), self.out, opts=self.opts)
        for conf in (self.out / "clients").rglob("*.conf"):
            self.assertIn("Endpoint = vpn.example.com:51820", conf.read_text(encoding="utf-8"))

    def test_client_conf_names_the_proxy_not_a_stale_egress_ip(self) -> None:
        # 배정표의 egress_ip 는 이 경로에서 출구를 정하지 않는다. 그 값을 그대로
        # 적어두면 운영자가 잘못된 IP 를 허용목록에 넣는다.
        export(self.alloc, _proxies(3), self.out, opts=self.opts)
        body = (self.out / "clients" / "g001" / "u001.conf").read_text(encoding="utf-8")
        self.assertIn("gw1.proxy.example.com:8001 경유", body)
        self.assertNotIn("203.0.113.1", body)

    def test_no_secret_material_anywhere_in_the_output(self) -> None:
        export(self.alloc, _proxies(3), self.out, opts=self.opts)
        for path in self.out.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("${PROXY_PW}", text)
                self.assertIsNone(re.search(r"=\s*[A-Za-z0-9+/]{43}=", text))

    def test_refuses_when_there_are_too_few_proxies(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            export(self.alloc, _proxies(2), self.out, opts=self.opts)
        self.assertIn("프록시가 부족하다", str(ctx.exception))

    def test_scripts_are_executable(self) -> None:
        export(self.alloc, _proxies(3), self.out, opts=self.opts)
        for name in ("genkeys.sh", "fill-secrets.sh", "iptables.sh"):
            self.assertTrue((self.out / name).stat().st_mode & 0o111, name)


class FillSecretsTest(unittest.TestCase):
    def test_reads_env_and_refuses_when_missing(self) -> None:
        script = render_fill_secrets_sh(_alloc())
        self.assertIn("IPALLOC_PROXY_PASSWORD", script)
        self.assertIn("__PROXY_PASSWORD_", script)
        self.assertIn("exit 1", script)


if __name__ == "__main__":
    unittest.main()
