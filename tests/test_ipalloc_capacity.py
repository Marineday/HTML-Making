"""ipalloc.capacity 검증.

"월 20분이면 용량은 문제가 아니다" 라는 전제를 숫자로 확인하고,
그 전제가 깨지는 지점(전원 동시 접속)도 함께 잡아두는 것이 목적이다.
"""

from __future__ import annotations

import unittest

from ipalloc.capacity import (
    BITRATE_MBPS,
    CapacityError,
    Workload,
    for_fleet,
    for_ip,
    poisson_quantile,
    verdict,
)


class PoissonTest(unittest.TestCase):
    def test_zero_mean(self) -> None:
        self.assertEqual(poisson_quantile(0.0, 1e-3), 0)

    def test_small_mean_gives_small_quantile(self) -> None:
        # 평균 0.0926 명이면 상위 0.1% 지점도 2명이다.
        self.assertEqual(poisson_quantile(0.0926, 1e-3), 2)

    def test_monotonic_in_mean(self) -> None:
        prev = -1
        for mean in (0.01, 0.1, 1.0, 5.0, 20.0, 100.0):
            q = poisson_quantile(mean, 1e-3)
            self.assertGreaterEqual(q, prev)
            prev = q

    def test_tighter_risk_gives_larger_quantile(self) -> None:
        self.assertGreaterEqual(poisson_quantile(1.0, 1e-6), poisson_quantile(1.0, 1e-2))

    def test_large_mean_uses_normal_approximation(self) -> None:
        # exp(-mean) 이 언더플로하는 구간에서 죽지 않아야 한다.
        q = poisson_quantile(10_000.0, 1e-3)
        self.assertGreater(q, 10_000)
        self.assertLess(q, 11_000)


class WorkloadTest(unittest.TestCase):
    def test_defaults_match_the_stated_requirement(self) -> None:
        w = Workload()
        self.assertEqual(w.users_per_ip, 20)
        self.assertEqual(w.minutes_per_user_per_month, 20.0)
        self.assertEqual(w.month_minutes, 43_200)

    def test_rejects_nonsense(self) -> None:
        for kwargs in (
            {"users_per_ip": 0},
            {"minutes_per_user_per_month": 0},
            {"stream_mbps": -1},
            {"peak_concentration": 0},
            {"peak_concentration": 1.5},
            {"risk": 0},
            {"minutes_per_user_per_month": 999_999},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(CapacityError):
                Workload(**kwargs)


class IpCapacityTest(unittest.TestCase):
    def test_the_stated_workload_is_tiny(self) -> None:
        cap = for_ip(Workload(users_per_ip=20, minutes_per_user_per_month=20.0, stream_mbps=5.0))
        # 20명 x 20분 = 400 분 / 43200 분
        self.assertAlmostEqual(cap.load_avg, 400 / 43_200, places=6)
        self.assertLess(cap.concurrency_typical, 5)
        # 400분 x 60초 x 5Mbps / 8 / 1000 = 15 GB
        self.assertAlmostEqual(cap.transfer_gb_month, 15.0, places=3)

    def test_worst_case_is_everyone_at_once(self) -> None:
        cap = for_ip(Workload(users_per_ip=20, stream_mbps=5.0))
        self.assertEqual(cap.concurrency_worst, 20)
        self.assertAlmostEqual(cap.mbps_worst, 100.0)

    def test_concurrency_never_exceeds_group_size(self) -> None:
        # 접속이 극단적으로 몰려도 IP 당 인원보다 많은 동시접속은 없다.
        cap = for_ip(Workload(users_per_ip=20, minutes_per_user_per_month=1000, peak_concentration=0.001))
        self.assertLessEqual(cap.concurrency_typical, 20)
        self.assertEqual(cap.concurrency_worst, 20)

    def test_at_least_one_stream_of_headroom(self) -> None:
        cap = for_ip(Workload(users_per_ip=1, minutes_per_user_per_month=1.0))
        self.assertGreaterEqual(cap.concurrency_typical, 1)
        self.assertGreater(cap.mbps_typical, 0)

    def test_4k_quadruples_transfer(self) -> None:
        hd = for_ip(Workload(stream_mbps=BITRATE_MBPS["1080p"]))
        uhd = for_ip(Workload(stream_mbps=BITRATE_MBPS["4k"]))
        self.assertAlmostEqual(uhd.transfer_gb_month / hd.transfer_gb_month, 16.0 / 5.0, places=6)


class FleetTest(unittest.TestCase):
    def test_thousand_users_fifty_ips(self) -> None:
        fleet = for_fleet(1000, 50, Workload(users_per_ip=20, stream_mbps=5.0))
        self.assertAlmostEqual(fleet.total_transfer_gb_month, 750.0, places=3)
        self.assertAlmostEqual(fleet.total_mbps_worst, 5000.0)

    def test_rejects_zero_ips(self) -> None:
        with self.assertRaises(CapacityError):
            for_fleet(1000, 0, Workload())


class VerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cap = for_ip(Workload(users_per_ip=20, stream_mbps=5.0))  # 최악 100 Mbps, 월 15 GB

    def test_gigabit_link_has_headroom(self) -> None:
        self.assertEqual(verdict(self.cap, link_mbps=1000), [])

    def test_judges_on_worst_case_not_average(self) -> None:
        # 평상시엔 10 Mbps 면 되지만 50 Mbps 회선은 전원 동시접속을 못 버틴다.
        problems = verdict(self.cap, link_mbps=50)
        self.assertEqual(len(problems), 1)
        self.assertIn("100 Mbps 가 필요", problems[0])
        self.assertIn("10명까지만", problems[0])

    def test_transfer_quota_exceeded(self) -> None:
        problems = verdict(self.cap, link_mbps=1000, transfer_quota_gb=10)
        self.assertTrue(any("넘는다" in p for p in problems))

    def test_transfer_quota_near_limit_warns(self) -> None:
        problems = verdict(self.cap, link_mbps=1000, transfer_quota_gb=17)
        self.assertTrue(any("80%" in p for p in problems))

    def test_transfer_quota_comfortable(self) -> None:
        self.assertEqual(verdict(self.cap, link_mbps=1000, transfer_quota_gb=1000), [])

    def test_rejects_bad_inputs(self) -> None:
        with self.assertRaises(CapacityError):
            verdict(self.cap, link_mbps=0)
        with self.assertRaises(CapacityError):
            verdict(self.cap, link_mbps=100, transfer_quota_gb=0)


if __name__ == "__main__":
    unittest.main()
