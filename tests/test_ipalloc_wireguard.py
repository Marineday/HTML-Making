"""ipalloc.wireguard 검증.

가장 중요한 두 가지.
    1. 묶음마다 SNAT 규칙이 자기 출구 IP 를 가리킬 것 — 이게 틀리면 모든
       묶음이 호스트 기본 IP 로 나가고 배정표가 무의미해진다.
    2. 개인키가 파일에 들어가지 않을 것 — 키는 genkeys.sh 가 만든다.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from ipalloc.allocator import allocate
from ipalloc.models import User
from ipalloc.pool import TunnelPlan, endpoints_from_cidr
from ipalloc.wireguard import ExportOptions, export, render_client_conf, render_server_conf


def _alloc(count: int = 45, group_size: int = 20):
    return allocate(
        [User(id=f"u{i:03d}") for i in range(1, count + 1)],
        endpoints_from_cidr("203.0.113.0/28", region="sg"),
        group_size=group_size,
    )


class ServerConfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = _alloc()
        self.tunnel = TunnelPlan()

    def test_snat_points_at_this_groups_egress_ip(self) -> None:
        for group in self.alloc.used_groups:
            conf = render_server_conf(group, self.tunnel, ExportOptions())
            with self.subTest(group=group.id):
                self.assertIn(f"-s {group.subnet} -o eth0 -j SNAT --to-source {group.endpoint.ip}", conf)
                # PostUp 과 PostDown 이 짝을 이뤄야 재시작 시 규칙이 쌓이지 않는다.
                self.assertEqual(conf.count("SNAT --to-source"), 2)
                self.assertIn("PostDown = iptables -t nat -D POSTROUTING", conf)

    def test_interface_uses_the_subnets_first_host(self) -> None:
        conf = render_server_conf(self.alloc.used_groups[0], self.tunnel, ExportOptions())
        self.assertIn("Address = 10.77.0.1/24", conf)
        self.assertIn("ListenPort = 51820", conf)

    def test_one_peer_block_per_member_with_a_32_route(self) -> None:
        group = self.alloc.used_groups[0]
        conf = render_server_conf(group, self.tunnel, ExportOptions())
        self.assertEqual(conf.count("[Peer]"), group.size)
        for m in group.members:
            self.assertIn(f"AllowedIPs = {m.tunnel_ip}/32", conf)
            self.assertIn(f"__CLIENT_PUBKEY_{m.user_id}__", conf)

    def test_no_real_key_material(self) -> None:
        conf = render_server_conf(self.alloc.used_groups[0], self.tunnel, ExportOptions())
        self.assertIn("PrivateKey = __SERVER_PRIVKEY__", conf)
        # base64 키처럼 생긴 값이 들어 있으면 안 된다.
        self.assertIsNone(re.search(r"=\s*[A-Za-z0-9+/]{43}=", conf))

    def test_custom_wan_interface(self) -> None:
        conf = render_server_conf(self.alloc.used_groups[0], self.tunnel, ExportOptions(wan_interface="ens5"))
        self.assertIn("-o ens5 -j SNAT", conf)


class ClientConfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = _alloc()
        self.group = self.alloc.used_groups[0]
        self.member = self.group.members[0]

    def test_full_tunnel_is_the_default(self) -> None:
        conf = render_client_conf(self.member, self.group, ExportOptions())
        self.assertIn("AllowedIPs = 0.0.0.0/0", conf)

    def test_split_tunnel_is_supported(self) -> None:
        conf = render_client_conf(self.member, self.group, ExportOptions(allowed_ips="198.51.100.0/24"))
        self.assertIn("AllowedIPs = 198.51.100.0/24", conf)
        self.assertNotIn("0.0.0.0/0", conf)

    def test_address_is_a_host_route_not_the_whole_subnet(self) -> None:
        # /24 를 주면 클라이언트가 묶음 전체 대역을 자기 것으로 잡아 라우팅이 꼬인다.
        conf = render_client_conf(self.member, self.group, ExportOptions())
        self.assertIn(f"Address = {self.member.tunnel_ip}/32", conf)

    def test_endpoint_and_keepalive(self) -> None:
        conf = render_client_conf(self.member, self.group, ExportOptions())
        self.assertIn(f"Endpoint = {self.member.egress_ip}:51820", conf)
        self.assertIn("PersistentKeepalive = 25", conf)
        self.assertIn("MTU = 1420", conf)


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "wg"
        self.addCleanup(self._tmp.cleanup)
        self.alloc = _alloc()

    def test_file_layout(self) -> None:
        counts = export(self.alloc, self.out)
        self.assertEqual(counts, {"servers": 3, "clients": 45})
        self.assertTrue((self.out / "server" / "g001.conf").exists())
        self.assertTrue((self.out / "clients" / "g003" / "u045.conf").exists())
        self.assertTrue((self.out / "genkeys.sh").exists())
        self.assertEqual(len(list((self.out / "clients" / "g001").glob("*.conf"))), 20)

    def test_peers_csv_drives_the_key_script(self) -> None:
        export(self.alloc, self.out)
        lines = (self.out / "peers.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(lines[0], "user_id,group_id")
        self.assertEqual(len(lines), 46)
        self.assertEqual(lines[1], "u001,g001")
        # peers.csv 의 모든 줄에 대응하는 설정 파일이 실제로 있어야 한다.
        for line in lines[1:]:
            uid, gid = line.split(",")
            self.assertTrue((self.out / "clients" / gid / f"{uid}.conf").exists())

    def test_genkeys_is_executable_and_refuses_without_wg(self) -> None:
        export(self.alloc, self.out)
        script = self.out / "genkeys.sh"
        self.assertTrue(script.stat().st_mode & 0o111)
        body = script.read_text(encoding="utf-8")
        self.assertIn("command -v wg", body)
        self.assertIn("umask 077", body)

    def test_every_placeholder_is_covered_by_the_script(self) -> None:
        # 스크립트가 치환하지 못하는 자리표시자가 있으면 설정이 조용히 망가진다.
        export(self.alloc, self.out)
        placeholders: set[str] = set()
        for conf in self.out.rglob("*.conf"):
            placeholders.update(re.findall(r"__[A-Z][A-Z0-9_]*[A-Za-z0-9]*__", conf.read_text(encoding="utf-8")))
        script = (self.out / "genkeys.sh").read_text(encoding="utf-8")
        for ph in placeholders:
            generic = re.sub(r"(__CLIENT_(?:PUBKEY|PSK)_)[^_]+__", r"\1${uid}__", ph)
            with self.subTest(placeholder=ph):
                self.assertIn(generic, script)


if __name__ == "__main__":
    unittest.main()
