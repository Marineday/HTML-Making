"""ipalloc.allocator 검증.

이 파일의 핵심은 증분 배정이다. "이미 배정된 사람은 움직이지 않는다" 가
깨지면 배정표 전체가 쓸모없어진다.
"""

from __future__ import annotations

import unittest

from ipalloc.allocator import AllocationError, allocate, group_id_for, index_of, rebuild, verify
from ipalloc.models import Assignment, User
from ipalloc.pool import TunnelPlan, endpoints_from_cidr


def users(n: int, start: int = 1) -> list[User]:
    return [User(id=f"u{i:04d}") for i in range(start, start + n)]


class GroupIdTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        self.assertEqual(group_id_for(0), "g001")
        self.assertEqual(group_id_for(49), "g050")
        self.assertEqual(index_of("g050"), 49)

    def test_rejects_garbage(self) -> None:
        for bad in ("group1", "g", "", "gabc", "g000"):
            with self.subTest(bad=bad), self.assertRaises(AllocationError):
                index_of(bad)


class BasicAllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoints = endpoints_from_cidr("203.0.113.0/26")

    def test_thousand_users_into_fifty_groups(self) -> None:
        alloc = allocate(users(1000), self.endpoints, group_size=20)
        self.assertEqual(len(alloc.assignments), 1000)
        self.assertEqual(len(alloc.used_groups), 50)
        self.assertTrue(all(g.size == 20 for g in alloc.used_groups))
        self.assertEqual(verify(alloc), [])

    def test_is_deterministic(self) -> None:
        a = allocate(users(137), self.endpoints, group_size=20)
        b = allocate(users(137), self.endpoints, group_size=20)
        self.assertEqual([x.as_row() for x in a.assignments], [x.as_row() for x in b.assignments])

    def test_last_group_is_partial(self) -> None:
        alloc = allocate(users(45), self.endpoints, group_size=20)
        self.assertEqual([g.size for g in alloc.used_groups], [20, 20, 5])
        self.assertEqual(alloc.used_groups[-1].vacancies(20), 15)

    def test_tunnel_ips_are_unique_and_in_subnet(self) -> None:
        alloc = allocate(users(60), self.endpoints, group_size=20)
        tunnel_ips = [a.tunnel_ip for a in alloc.assignments]
        self.assertEqual(len(tunnel_ips), len(set(tunnel_ips)))
        self.assertTrue(alloc.assignments[0].tunnel_ip.startswith("10.77.0."))
        self.assertTrue(alloc.assignments[20].tunnel_ip.startswith("10.77.1."))

    def test_duplicate_users_do_not_eat_slots(self) -> None:
        dup = users(20) + [User(id="u0001"), User(id="u0002")]
        alloc = allocate(dup, self.endpoints, group_size=20)
        self.assertEqual(len(alloc.assignments), 20)
        self.assertEqual(len(alloc.used_groups), 1)

    def test_rejects_group_size_larger_than_subnet(self) -> None:
        with self.assertRaises(AllocationError):
            allocate(users(5), self.endpoints, group_size=300, tunnel=TunnelPlan("10.77.0.0/16", 24))

    def test_empty_pool(self) -> None:
        with self.assertRaises(AllocationError):
            allocate(users(5), [], group_size=20)

    def test_not_enough_ips_reports_unassigned(self) -> None:
        # /29 는 호스트 6개 → 6 x 20 = 120 자리. 50명은 넉넉히 들어간다.
        alloc = allocate(users(50), endpoints_from_cidr("203.0.113.0/29"), group_size=20)
        self.assertEqual(alloc.unassigned, [])
        # /30 은 호스트 2개 → 40 자리뿐이다. 나머지 10명은 미배정으로 보고해야 한다.
        tight = allocate(users(50), endpoints_from_cidr("203.0.113.0/30"), group_size=20)
        self.assertEqual(len(tight.assignments), 40)
        self.assertEqual(len(tight.unassigned), 10)
        self.assertTrue(any("출구 IP" in p for p in verify(tight)))


class IncrementalTest(unittest.TestCase):
    """기존 배정표를 주고 다시 돌렸을 때의 동작."""

    def setUp(self) -> None:
        self.endpoints = endpoints_from_cidr("203.0.113.0/26")
        self.first = allocate(users(100), self.endpoints, group_size=20)
        self.before = {a.user_id: (a.group_id, a.egress_ip, a.tunnel_ip) for a in self.first.assignments}

    def test_rerun_with_same_roster_changes_nothing(self) -> None:
        again = allocate(users(100), self.endpoints, group_size=20, existing=self.first.assignments)
        after = {a.user_id: (a.group_id, a.egress_ip, a.tunnel_ip) for a in again.assignments}
        self.assertEqual(self.before, after)
        self.assertEqual(again.kept, 100)
        self.assertEqual(again.added, 0)

    def test_new_users_fill_vacancies_without_moving_anyone(self) -> None:
        roster = users(100) + users(10, start=101)
        again = allocate(roster, self.endpoints, group_size=20, existing=self.first.assignments)
        after = {a.user_id: (a.group_id, a.egress_ip, a.tunnel_ip) for a in again.assignments}
        for uid, was in self.before.items():
            self.assertEqual(after[uid], was, f"{uid} 가 움직였다")
        self.assertEqual(again.added, 10)
        # 100명이 5묶음을 꽉 채웠으므로 신규 10명은 6번째 묶음으로 간다.
        self.assertEqual(after["u0101"][0], "g006")

    def test_departure_frees_a_slot_that_a_newcomer_reuses(self) -> None:
        roster = [u for u in users(100) if u.id != "u0005"] + [User(id="u9999")]
        again = allocate(roster, self.endpoints, group_size=20, existing=self.first.assignments)
        self.assertEqual(again.dropped, ["u0005"])
        after = {a.user_id: a for a in again.assignments}
        self.assertNotIn("u0005", after)
        # 빈자리는 낮은 묶음부터 채운다 — u0005 가 있던 g001 이다.
        self.assertEqual(after["u9999"].group_id, "g001")
        self.assertEqual(after["u9999"].tunnel_ip, self.before["u0005"][2])
        for uid, was in self.before.items():
            if uid == "u0005":
                continue
            self.assertEqual((after[uid].group_id, after[uid].egress_ip, after[uid].tunnel_ip), was)

    def test_removed_endpoint_remaps_only_its_members(self) -> None:
        # g001 의 출구 IP 를 풀에서 뺀다. 그 묶음 사람들만 옮겨져야 한다.
        shrunk = [ep for ep in self.endpoints if ep.ip != "203.0.113.1"]
        again = allocate(users(100), shrunk, group_size=20, existing=self.first.assignments)
        after = {a.user_id: a for a in again.assignments}
        moved = [uid for uid, was in self.before.items() if after[uid].egress_ip != was[1]]
        self.assertEqual(len(moved), 20)
        self.assertTrue(all(self.before[uid][0] == "g001" for uid in moved))
        self.assertEqual(verify(again), [])

    def test_remapped_users_are_not_counted_as_new(self) -> None:
        # 재배정된 사람은 설정을 다시 받아야 하고, 신규는 처음 받는다. 섞이면
        # 누구에게 파일을 보내야 하는지 알 수 없다.
        shrunk = [ep for ep in self.endpoints if ep.ip != "203.0.113.1"]
        again = allocate(users(100) + [User(id="u9999")], shrunk, group_size=20, existing=self.first.assignments)
        self.assertEqual(len(again.moved), 20)
        self.assertEqual(again.added, 1)
        self.assertEqual(again.kept, 80)
        self.assertNotIn("u9999", again.moved)
        self.assertIn("이동 20", again.summary())

    def test_group_id_stays_bound_to_its_egress_ip(self) -> None:
        # 풀의 순서를 뒤집어도 이미 쓰이던 IP 의 묶음 ID 는 그대로여야 한다.
        reversed_pool = list(reversed(self.endpoints))
        again = allocate(users(100), reversed_pool, group_size=20, existing=self.first.assignments)
        after = {a.user_id: a for a in again.assignments}
        for uid, (gid, ip, _) in self.before.items():
            self.assertEqual(after[uid].group_id, gid)
            self.assertEqual(after[uid].egress_ip, ip)

    def test_corrupt_existing_table_is_rejected(self) -> None:
        bad = list(self.first.assignments)
        bad.append(
            Assignment(
                user_id="uX",
                group_id="g002",
                egress_ip="203.0.113.1",  # g001 이 쓰는 IP 인데 g002 라고 되어 있다
                tunnel_ip="10.77.1.9",
                listen_host="203.0.113.1",
                listen_port=51820,
            )
        )
        with self.assertRaises(AllocationError):
            allocate(users(100) + [User(id="uX")], self.endpoints, group_size=20, existing=bad)


class VerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alloc = allocate(users(40), endpoints_from_cidr("203.0.113.0/28"), group_size=20)

    def test_clean_allocation_has_no_problems(self) -> None:
        self.assertEqual(verify(self.alloc), [])

    def test_catches_over_capacity(self) -> None:
        g = self.alloc.groups[0]
        g.members.append(
            Assignment(
                user_id="uX",
                group_id=g.id,
                egress_ip=g.endpoint.ip,
                tunnel_ip="10.77.0.99",
                listen_host=g.endpoint.ip,
                listen_port=51820,
            )
        )
        self.assertTrue(any("정원 초과" in p for p in verify(self.alloc)))

    def test_catches_duplicate_user_across_groups(self) -> None:
        first = self.alloc.groups[0].members[0]
        g2 = self.alloc.groups[1]
        g2.members.append(
            Assignment(
                user_id=first.user_id,
                group_id=g2.id,
                egress_ip=g2.endpoint.ip,
                tunnel_ip="10.77.1.99",
                listen_host=g2.endpoint.ip,
                listen_port=51820,
            )
        )
        self.assertTrue(any("중복 배정" in p for p in verify(self.alloc)))

    def test_catches_tunnel_ip_outside_subnet(self) -> None:
        g = self.alloc.groups[0]
        g.members[0] = Assignment(
            user_id=g.members[0].user_id,
            group_id=g.id,
            egress_ip=g.endpoint.ip,
            tunnel_ip="10.99.99.99",
            listen_host=g.endpoint.ip,
            listen_port=51820,
        )
        self.assertTrue(any("서브넷" in p for p in verify(self.alloc)))


class RebuildTest(unittest.TestCase):
    def test_round_trip_matches_original(self) -> None:
        alloc = allocate(users(100), endpoints_from_cidr("203.0.113.0/26"), group_size=20)
        again = rebuild(alloc.assignments, group_size=20)
        self.assertEqual([a.as_row() for a in alloc.assignments], [a.as_row() for a in again.assignments])
        self.assertEqual([g.subnet for g in alloc.used_groups], [g.subnet for g in again.used_groups])
        self.assertEqual(verify(again), [])


if __name__ == "__main__":
    unittest.main()
