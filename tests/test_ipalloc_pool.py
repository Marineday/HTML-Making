"""ipalloc.pool 검증."""

from __future__ import annotations

import ipaddress
import unittest

from ipalloc.models import Endpoint
from ipalloc.pool import (
    PoolError,
    TunnelPlan,
    check_pool,
    dedupe_endpoints,
    endpoints_from_cidr,
    required_groups,
)


class CidrTest(unittest.TestCase):
    def test_excludes_network_and_broadcast(self) -> None:
        # /28 은 16개지만 실제로 쓸 수 있는 건 14개다. 이걸 16으로 세면
        # 마지막 묶음이 갈 곳을 잃는다.
        eps = endpoints_from_cidr("203.0.113.16/28")
        self.assertEqual(len(eps), 14)
        self.assertEqual(eps[0].ip, "203.0.113.17")
        self.assertEqual(eps[-1].ip, "203.0.113.30")

    def test_limit_and_metadata(self) -> None:
        eps = endpoints_from_cidr("203.0.113.0/26", region="sg", provider="acme", listen_port=51999, limit=5)
        self.assertEqual(len(eps), 5)
        self.assertEqual(eps[0].region, "sg")
        self.assertEqual(eps[0].provider, "acme")
        self.assertEqual(eps[0].listen_port, 51999)

    def test_rejects_oversized_and_bad_input(self) -> None:
        with self.assertRaises(PoolError):
            endpoints_from_cidr("203.0.0.0/8")
        with self.assertRaises(PoolError):
            endpoints_from_cidr("nonsense")
        with self.assertRaises(PoolError):
            endpoints_from_cidr("2001:db8::/120")

    def test_dedupe_preserves_order(self) -> None:
        eps = [Endpoint(ip="203.0.113.2"), Endpoint(ip="203.0.113.1"), Endpoint(ip="203.0.113.2")]
        self.assertEqual([e.ip for e in dedupe_endpoints(eps)], ["203.0.113.2", "203.0.113.1"])


class TunnelPlanTest(unittest.TestCase):
    def test_capacity_and_hosts(self) -> None:
        plan = TunnelPlan("10.77.0.0/16", 24)
        self.assertEqual(plan.capacity, 256)
        self.assertEqual(plan.hosts_per_group, 253)

    def test_subnet_for_is_stable(self) -> None:
        plan = TunnelPlan("10.77.0.0/16", 24)
        self.assertEqual(str(plan.subnet_for(0)), "10.77.0.0/24")
        self.assertEqual(str(plan.subnet_for(49)), "10.77.49.0/24")
        # subnets() 순회와 인덱스 접근이 어긋나면 배정표가 조용히 깨진다.
        listed = list(plan.subnets())
        self.assertEqual(str(listed[49]), str(plan.subnet_for(49)))

    def test_subnet_out_of_range(self) -> None:
        with self.assertRaises(PoolError):
            TunnelPlan("10.77.0.0/16", 24).subnet_for(256)

    def test_server_ip_and_members_skip_first_host(self) -> None:
        plan = TunnelPlan("10.77.0.0/16", 24)
        net = ipaddress.ip_network("10.77.0.0/24")
        self.assertEqual(plan.server_ip(net), "10.77.0.1")
        members = list(plan.member_ips(net))
        self.assertEqual(members[0], "10.77.0.2")
        self.assertEqual(len(members), 253)

    def test_rejects_public_tunnel_range(self) -> None:
        with self.assertRaises(PoolError):
            TunnelPlan("203.0.113.0/24", 28)

    def test_rejects_mismatched_prefix(self) -> None:
        with self.assertRaises(PoolError):
            TunnelPlan("10.77.0.0/16", 8)


class SizingTest(unittest.TestCase):
    def test_required_groups_rounds_up(self) -> None:
        self.assertEqual(required_groups(1000, 20), 50)
        self.assertEqual(required_groups(1001, 20), 51)
        self.assertEqual(required_groups(0, 20), 0)

    def test_check_pool_message_names_the_gap(self) -> None:
        with self.assertRaises(PoolError) as ctx:
            check_pool(1000, 20, 40)
        msg = str(ctx.exception)
        self.assertIn("50개가 필요", msg)
        self.assertIn("10개를 더", msg)

    def test_check_pool_passes_when_enough(self) -> None:
        check_pool(1000, 20, 50)


if __name__ == "__main__":
    unittest.main()
