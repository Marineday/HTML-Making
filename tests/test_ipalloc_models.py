"""ipalloc.models 검증."""

from __future__ import annotations

import unittest

from ipalloc.models import Assignment, Endpoint, Group, ModelError, User, dedupe_users


class UserTest(unittest.TestCase):
    def test_id_is_trimmed(self) -> None:
        self.assertEqual(User(id="  u001  ").id, "u001")

    def test_rejects_unsafe_ids(self) -> None:
        # 파일명과 sed 치환에 그대로 들어가는 값이다. 막지 않으면 조용히 깨진다.
        for bad in ("u 001", "u/001", "../etc", "", "-u1", "u" * 64, "사용자"):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                User(id=bad)

    def test_accepts_dots_and_hyphens(self) -> None:
        for good in ("u001", "user.1", "user_1", "a-b-c", "9x"):
            with self.subTest(good=good):
                self.assertEqual(User(id=good).id, good)


class EndpointTest(unittest.TestCase):
    def test_rejects_private_and_cgnat(self) -> None:
        for bad in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "100.64.0.1", "127.0.0.1", "169.254.1.1"):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                Endpoint(ip=bad)

    def test_rejects_ipv6_and_garbage(self) -> None:
        for bad in ("2001:db8::1", "not-an-ip", "203.0.113.300"):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                Endpoint(ip=bad)

    def test_allows_documentation_range_but_flags_it(self) -> None:
        # 예제·테스트가 돌아가야 하므로 통과시키되, 운영 경고는 낼 수 있어야 한다.
        ep = Endpoint(ip="203.0.113.7")
        self.assertTrue(ep.is_documentation)
        self.assertFalse(Endpoint(ip="198.18.0.1").is_documentation)

    def test_listen_host_defaults_to_ip(self) -> None:
        self.assertEqual(Endpoint(ip="203.0.113.7").listen_host, "203.0.113.7")
        self.assertEqual(Endpoint(ip="203.0.113.7", listen_host="vpn.example.com").listen_host, "vpn.example.com")

    def test_rejects_bad_port(self) -> None:
        for bad in (0, -1, 65536):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                Endpoint(ip="203.0.113.7", listen_port=bad)


class GroupTest(unittest.TestCase):
    def _assignment(self, uid: str, tunnel_ip: str) -> Assignment:
        return Assignment(
            user_id=uid,
            group_id="g001",
            egress_ip="203.0.113.1",
            tunnel_ip=tunnel_ip,
            listen_host="203.0.113.1",
            listen_port=51820,
        )

    def test_vacancies_never_negative(self) -> None:
        g = Group(id="g001", endpoint=Endpoint(ip="203.0.113.1"), subnet="10.77.0.0/24")
        g.members.append(self._assignment("u1", "10.77.0.2"))
        g.members.append(self._assignment("u2", "10.77.0.3"))
        self.assertEqual(g.vacancies(20), 18)
        self.assertEqual(g.vacancies(1), 0)
        self.assertEqual(g.used_tunnel_ips(), {"10.77.0.2", "10.77.0.3"})


class DedupeTest(unittest.TestCase):
    def test_keeps_first_occurrence_and_order(self) -> None:
        users = [User(id="a"), User(id="b"), User(id="a", label="dup"), User(id="c")]
        self.assertEqual([u.id for u in dedupe_users(users)], ["a", "b", "c"])
        self.assertEqual(dedupe_users(users)[0].label, "")


if __name__ == "__main__":
    unittest.main()
