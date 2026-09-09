import unittest
from datetime import datetime, timedelta, timezone

from hotdeal.models import Deal
from hotdeal.price_store import Grade, PriceStore, PricingSettings

NOW = datetime.now(timezone.utc)


def deal(title, price, days_ago=0, url=None):
    return Deal(
        source="test",
        title=title,
        url=url or f"https://a.example/{abs(hash((title, price))) % 10**8}",
        posted_at=NOW - timedelta(days=days_ago),
        price_krw=price,
    )


# 개당 900원 근처의 신라면 20개입 이력 6건
HISTORY = [
    ("[G마켓] 농심 신라면 20개입 (18,000원/무료)", 18000, 25),
    ("[쿠팡] 신라면 20개입 17,800원", 17800, 20),
    ("[11번가] 농심 신라면 20개입 (19,000원)", 19000, 15),
    ("[옥션] 신라면 20개입 18,400원", 18400, 10),
    ("[네이버] 농심 신라면 20개입 (17,900원)", 17900, 5),
    ("[티몬] 신라면 10개입 9,200원", 9200, 3),
]


def seeded_store(**settings):
    store = PriceStore(":memory:", PricingSettings(**settings))
    for title, price, days_ago in HISTORY:
        store.record(deal(title, price, days_ago))
    return store


class RecordTest(unittest.TestCase):
    def test_records_deal_with_parseable_unit_price(self):
        store = PriceStore(":memory:")
        self.assertTrue(store.record(deal("신라면 20개입 12,900원", 12900)))
        self.assertEqual(store.count(), 1)

    def test_same_deal_recorded_once(self):
        # 매 폴링마다 같은 딜이 다시 들어온다. 중복 기록되면 중앙값이 왜곡된다.
        store = PriceStore(":memory:")
        item = deal("신라면 20개입", 12900)
        self.assertTrue(store.record(item))
        self.assertFalse(store.record(item))
        self.assertEqual(store.count(), 1)

    def test_skips_deal_without_quantity(self):
        store = PriceStore(":memory:")
        self.assertFalse(store.record(deal("에어팟 프로", 289000)))
        self.assertEqual(store.count(), 0)

    def test_skips_deal_without_price(self):
        store = PriceStore(":memory:")
        item = Deal("t", "신라면 20개입", "https://a.example/1", posted_at=NOW)
        self.assertFalse(store.record(item))


class GradeTest(unittest.TestCase):
    def test_super_deal_far_below_median(self):
        store = seeded_store(min_samples=5, super_deal_ratio=0.75)
        verdict = store.evaluate(deal("[쿠팡] 농심 신라면 20개입 12,900원", 12900))
        self.assertIs(verdict.grade, Grade.SUPER)
        self.assertAlmostEqual(verdict.unit_price, 645.0)
        self.assertGreaterEqual(verdict.sample_size, 5)

    def test_good_deal(self):
        store = seeded_store(min_samples=5, super_deal_ratio=0.60, good_deal_ratio=0.85)
        verdict = store.evaluate(deal("[G마켓] 신라면 20개입 15,000원", 15000))
        self.assertIs(verdict.grade, Grade.GOOD)

    def test_normal_when_above_median(self):
        store = seeded_store(min_samples=5)
        self.assertIs(store.evaluate(deal("[쿠팡] 신라면 20개입 22,000원", 22000)).grade, Grade.NORMAL)

    def test_below_average(self):
        store = seeded_store(min_samples=5)
        self.assertIs(store.evaluate(deal("[옥션] 신라면 20개입 17,900원", 17900)).grade, Grade.BELOW_AVERAGE)

    def test_insufficient_samples_never_claims_a_grade(self):
        # 표본이 적을 때 등급을 매기면 오판이 그대로 방에 나간다.
        store = seeded_store(min_samples=20)
        verdict = store.evaluate(deal("[쿠팡] 신라면 20개입 9,900원", 9900))
        self.assertIs(verdict.grade, Grade.INSUFFICIENT)
        self.assertIsNone(verdict.baseline)

    def test_unknown_product_is_insufficient_not_super(self):
        store = seeded_store(min_samples=5)
        self.assertIs(store.evaluate(deal("[쿠팡] 처음보는상품 30개입 990원", 990)).grade, Grade.INSUFFICIENT)

    def test_unknown_when_quantity_cannot_be_parsed(self):
        store = seeded_store(min_samples=5)
        verdict = store.evaluate(deal("에어팟 프로 2세대", 289000))
        self.assertIs(verdict.grade, Grade.UNKNOWN)
        self.assertEqual(verdict.describe(), "")

    def test_disabled_pricing_returns_unknown(self):
        store = seeded_store(enabled=False)
        self.assertIs(store.evaluate(deal("신라면 20개입 12,900원", 12900)).grade, Grade.UNKNOWN)


class RobustnessTest(unittest.TestCase):
    def test_median_survives_a_misparsed_outlier(self):
        # 제목 파싱은 반드시 일부 틀린다. 평균이었다면 여기서 판정이 무너진다.
        store = seeded_store(min_samples=5)
        store.record(deal("[사기몰] 신라면 20개입 2,000,000원", 2_000_000, 1))
        verdict = store.evaluate(deal("[쿠팡] 농심 신라면 20개입 12,900원", 12900))
        self.assertIn(verdict.grade, (Grade.SUPER, Grade.GOOD))
        self.assertLess(verdict.baseline, 2000)

    def test_window_excludes_old_records(self):
        store = PriceStore(":memory:", PricingSettings(window_days=7, min_samples=3))
        for index in range(5):
            store.record(deal(f"신라면 20개입 {18000 + index}원", 18000 + index, days_ago=60, url=f"https://a.example/old{index}"))
        verdict = store.evaluate(deal("신라면 20개입 12,900원", 12900))
        self.assertIs(verdict.grade, Grade.INSUFFICIENT)  # 창 밖의 기록은 세지 않는다

    def test_current_deal_is_excluded_from_its_own_baseline(self):
        store = seeded_store(min_samples=5)
        item = deal("[쿠팡] 신라면 20개입 12,900원", 12900)
        store.record(item)
        verdict = store.evaluate(item)
        self.assertEqual(verdict.sample_size, len(HISTORY))

    def test_all_time_low_flag(self):
        store = seeded_store(min_samples=5)
        self.assertTrue(store.evaluate(deal("[쿠팡] 신라면 20개입 5,000원", 5000)).is_all_time_low)
        self.assertFalse(store.evaluate(deal("[쿠팡] 신라면 20개입 30,000원", 30000)).is_all_time_low)

    def test_different_dimensions_are_never_compared(self):
        # 개수 기준 이력과 용량 기준 딜을 섞어 비교하면 숫자가 완전히 엉킨다.
        store = PriceStore(":memory:", PricingSettings(min_samples=3))
        for index in range(5):
            store.record(deal(f"섬유유연제 {index + 1}00개입", 10000, url=f"https://a.example/c{index}"))
        verdict = store.evaluate(deal("섬유유연제 2.5L 5,000원", 5000))
        self.assertIs(verdict.grade, Grade.INSUFFICIENT)


class DescribeTest(unittest.TestCase):
    def test_describe_shows_the_evidence(self):
        store = seeded_store(min_samples=5)
        text = store.evaluate(deal("[쿠팡] 농심 신라면 20개입 12,900원", 12900)).describe()
        self.assertIn("645원/개", text)
        self.assertIn("중앙값", text)
        self.assertIn("표본", text)

    def test_describe_is_empty_when_unknown(self):
        self.assertEqual(seeded_store().evaluate(deal("에어팟", 289000)).describe(), "")


class PruneTest(unittest.TestCase):
    def test_prune_removes_old_history_and_its_tokens(self):
        store = seeded_store()
        self.assertEqual(store.prune(0), len(HISTORY))
        self.assertEqual(store.count(), 0)
        leftover = store._conn.execute("SELECT COUNT(*) FROM price_tokens").fetchone()[0]
        self.assertEqual(leftover, 0)


if __name__ == "__main__":
    unittest.main()
