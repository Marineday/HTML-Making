"""ipalloc.cost 검증.

핵심은 손익분기 GB 단가다. "레지덴셜이 싸다" 는 직관이 맞는지 틀리는지는
IP 개수 대 트래픽 비율이 결정한다.
"""

from __future__ import annotations

import unittest

from ipalloc.cost import (
    CostError,
    PriceBook,
    breakeven_per_gb,
    compare,
    crossover_ip_count,
    estimate,
    scale,
)


class PriceBookTest(unittest.TestCase):
    def test_rejects_negative_prices(self) -> None:
        for kwargs in ({"ip_monthly": -1}, {"host_monthly": -1}, {"egress_per_gb": -0.1}, {"per_gb": -1}, {"hosts": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CostError):
                PriceBook(**kwargs)

    def test_zero_is_allowed(self) -> None:
        # 전송량이 포함된 VPS 는 egress 단가가 0 이다. 쓰지 않는 항목은 0 으로 둔다.
        book = PriceBook(ip_monthly=2.0, egress_per_gb=0.0, per_gb=0.0)
        self.assertEqual(book.egress_per_gb, 0.0)
        self.assertEqual(book.hosts, 1)


class EstimateTest(unittest.TestCase):
    def test_datacenter_breakdown(self) -> None:
        e = estimate(
            "데이터센터",
            ip_count=50,
            transfer_gb=750,
            prices=PriceBook(ip_monthly=3.6, host_monthly=20, hosts=2, egress_per_gb=0.09),
        )
        self.assertAlmostEqual(e.ip_cost, 180.0)
        self.assertAlmostEqual(e.host_cost, 40.0)
        self.assertAlmostEqual(e.traffic_cost, 67.5)
        self.assertAlmostEqual(e.total, 287.5)
        self.assertAlmostEqual(e.fixed, 220.0)

    def test_residential_is_traffic_dominated(self) -> None:
        e = estimate(
            "레지덴셜",
            ip_count=50,
            transfer_gb=750,
            prices=PriceBook(host_monthly=20, hosts=2, per_gb=2.0),
        )
        self.assertAlmostEqual(e.ip_cost, 0.0)
        self.assertAlmostEqual(e.traffic_cost, 1500.0)
        self.assertAlmostEqual(e.total, 1540.0)

    def test_static_isp_charges_per_ip(self) -> None:
        e = estimate("ISP", ip_count=50, transfer_gb=750, prices=PriceBook(ip_monthly=5.0))
        self.assertAlmostEqual(e.ip_cost, 250.0)
        self.assertAlmostEqual(e.traffic_cost, 0.0)

    def test_egress_and_per_gb_stack(self) -> None:
        # 클라우드 호스트에서 레지덴셜로 내보내면 양쪽 전송 단가를 다 낸다.
        e = estimate("둘 다", ip_count=1, transfer_gb=100, prices=PriceBook(egress_per_gb=0.09, per_gb=2.0))
        self.assertAlmostEqual(e.traffic_cost, 209.0)

    def test_rejects_negative_inputs(self) -> None:
        with self.assertRaises(CostError):
            estimate("x", ip_count=-1, transfer_gb=10, prices=PriceBook())
        with self.assertRaises(CostError):
            estimate("x", ip_count=1, transfer_gb=-10, prices=PriceBook())


class BreakevenTest(unittest.TestCase):
    def test_the_stated_workload(self) -> None:
        # 데이터센터 287.50 / 레지덴셜 고정비 40 / 750 GB
        # -> (287.5 - 40) / 750 = 0.33
        self.assertAlmostEqual(breakeven_per_gb(287.5, 40.0, 750.0), 0.33, places=6)

    def test_none_when_fixed_cost_already_loses(self) -> None:
        # 고정비만으로 상대 총액을 넘으면 GB 단가가 0이어도 이길 수 없다.
        self.assertIsNone(breakeven_per_gb(100.0, 120.0, 750.0))
        self.assertIsNone(breakeven_per_gb(100.0, 100.0, 750.0))

    def test_more_traffic_makes_per_gb_harder(self) -> None:
        # 트래픽이 늘수록 종량제가 맞춰야 하는 단가는 낮아진다.
        low = breakeven_per_gb(287.5, 40.0, 750.0)
        high = breakeven_per_gb(287.5, 40.0, 7500.0)
        assert low is not None and high is not None
        self.assertLess(high, low)

    def test_rejects_zero_transfer(self) -> None:
        with self.assertRaises(CostError):
            breakeven_per_gb(100.0, 10.0, 0.0)


class ScaleTest(unittest.TestCase):
    """IP 개수를 늘렸을 때 두 과금 모델이 어떻게 갈리는지."""

    def setUp(self) -> None:
        # 사용자 1000명 · 1인당 100MB → 전체 월 100GB
        self.transfer_gb = 100.0
        self.fixed = PriceBook(ip_monthly=3.6, host_monthly=20, hosts=2, egress_per_gb=0.09)
        self.metered = PriceBook(host_monthly=20, hosts=2, per_gb=2.0)

    def rows(self, *per_ip: int):
        return scale(1000, list(per_ip), transfer_gb=self.transfer_gb, fixed_prices=self.fixed, metered_prices=self.metered)

    def test_metered_total_is_flat_across_ip_counts(self) -> None:
        # 요구사항의 핵심: 종량제는 IP 를 몇 개 받든 총액이 같다.
        rows = self.rows(5, 10, 20, 50, 100)
        totals = {round(r.metered_total, 6) for r in rows}
        self.assertEqual(len(totals), 1)
        self.assertAlmostEqual(rows[0].metered_total, 240.0, places=2)

    def test_fixed_total_grows_with_ip_count(self) -> None:
        rows = self.rows(5, 10, 20, 50)
        totals = [r.fixed_total for r in rows]
        self.assertEqual(totals, sorted(totals, reverse=True))  # per_ip 오름차순 = IP 감소 = 비용 감소

    def test_ip_count_rounds_up(self) -> None:
        rows = self.rows(3)
        self.assertEqual(rows[0].ip_count, 334)

    def test_rows_are_sorted_and_deduped(self) -> None:
        rows = self.rows(20, 5, 20, 10)
        self.assertEqual([r.per_ip for r in rows], [5, 10, 20])

    def test_cheaper_label_flips_at_the_crossover(self) -> None:
        rows = self.rows(5, 20)
        self.assertEqual(rows[0].cheaper, "종량")  # IP 200개
        self.assertEqual(rows[1].cheaper, "IP월정액")  # IP 50개

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(CostError):
            scale(0, [20], transfer_gb=100, fixed_prices=self.fixed, metered_prices=self.metered)
        with self.assertRaises(CostError):
            scale(1000, [], transfer_gb=100, fixed_prices=self.fixed, metered_prices=self.metered)
        with self.assertRaises(CostError):
            scale(1000, [0], transfer_gb=100, fixed_prices=self.fixed, metered_prices=self.metered)


class CrossoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixed = PriceBook(ip_monthly=3.6, host_monthly=20, hosts=2, egress_per_gb=0.09)
        self.metered = PriceBook(host_monthly=20, hosts=2, per_gb=2.0)

    def test_matches_the_scale_table(self) -> None:
        n = crossover_ip_count(self.fixed, self.metered, transfer_gb=100.0)
        self.assertEqual(n, 54)
        # 교차점 바로 아래/위에서 실제로 뒤집히는지 확인한다.
        below = scale(n - 1, [1], transfer_gb=100.0, fixed_prices=self.fixed, metered_prices=self.metered)[0]
        at = scale(n, [1], transfer_gb=100.0, fixed_prices=self.fixed, metered_prices=self.metered)[0]
        self.assertLess(below.fixed_total, below.metered_total)
        self.assertLess(at.metered_total, at.fixed_total)

    def test_none_when_ip_prices_match(self) -> None:
        same = PriceBook(ip_monthly=3.6, per_gb=2.0)
        self.assertIsNone(crossover_ip_count(self.fixed, same, transfer_gb=100.0))

    def test_one_when_metered_wins_even_on_fixed_cost(self) -> None:
        # 종량 단가가 아주 싸면 IP 1개부터 이긴다.
        cheap = PriceBook(host_monthly=20, hosts=2, per_gb=0.01)
        self.assertEqual(crossover_ip_count(self.fixed, cheap, transfer_gb=100.0), 1)


class CompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dc = estimate("데이터센터", ip_count=50, transfer_gb=750, prices=PriceBook(ip_monthly=3.6, host_monthly=20, hosts=2, egress_per_gb=0.09))

    def test_reports_residential_as_more_expensive(self) -> None:
        res = estimate("레지덴셜", ip_count=50, transfer_gb=750, prices=PriceBook(host_monthly=20, hosts=2, per_gb=2.0))
        lines = compare(self.dc, res)
        self.assertEqual(len(lines), 1)
        self.assertIn("초과", lines[0])
        self.assertIn("5.4배", lines[0])

    def test_reports_residential_as_cheaper_when_it_is(self) -> None:
        # GB 단가가 손익분기 아래면 뒤집힌다.
        res = estimate("레지덴셜", ip_count=50, transfer_gb=750, prices=PriceBook(host_monthly=20, hosts=2, per_gb=0.10))
        self.assertIn("저렴", compare(self.dc, res)[0])

    def test_zero_baseline_is_reported_not_divided(self) -> None:
        empty = estimate("빈값", ip_count=0, transfer_gb=0, prices=PriceBook())
        self.assertIn("비교할 수 없다", compare(empty, self.dc)[0])


if __name__ == "__main__":
    unittest.main()
