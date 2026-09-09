import unittest
from datetime import datetime, timedelta, timezone

from hotdeal.config import FilterConfig
from hotdeal.filters import DealFilter
from hotdeal.models import Deal


def deal(title, **kwargs):
    return Deal(source="t", title=title, url="https://a.example/1", **kwargs)


class KeywordTest(unittest.TestCase):
    def setUp(self):
        self.filter = DealFilter(
            FilterConfig(include_keywords=["토스", "토스페이"], exclude_keywords=["토스트", "토스터"], max_age_minutes=None)
        )

    def test_matches_include_keyword(self):
        self.assertTrue(self.filter.check(deal("[스타벅스] 토스페이 5천원 할인")).passed)

    def test_ignores_spacing_between_keyword_characters(self):
        self.assertTrue(self.filter.check(deal("토스 페이 할인 이벤트")).passed)

    def test_exclude_beats_include_for_korean_substring_traps(self):
        # "토스터기"는 "토스"를 포함하므로 exclude 없이는 오탐이 난다.
        result = self.filter.check(deal("[쿠팡] 발뮤다 토스터 특가"))
        self.assertFalse(result.passed)
        self.assertIn("제외 키워드", result.reason)

    def test_drops_unrelated_deal(self):
        self.assertFalse(self.filter.check(deal("[11번가] 무선 이어폰")).passed)

    def test_case_insensitive_for_latin(self):
        latin = DealFilter(FilterConfig(include_keywords=["tosspay"], max_age_minutes=None))
        self.assertTrue(latin.check(deal("TossPay 할인")).passed)

    def test_require_all_keywords(self):
        strict = DealFilter(FilterConfig(include_keywords=["토스", "무료배송"], require_all_keywords=True, max_age_minutes=None))
        self.assertFalse(strict.check(deal("토스페이 할인")).passed)
        self.assertTrue(strict.check(deal("토스페이 할인 무료배송")).passed)

    def test_empty_include_list_passes_everything(self):
        wide = DealFilter(FilterConfig(max_age_minutes=None))
        self.assertTrue(wide.check(deal("아무거나")).passed)


class PriceAndAgeTest(unittest.TestCase):
    def test_price_bounds(self):
        f = DealFilter(FilterConfig(min_price_krw=10000, max_price_krw=50000, max_age_minutes=None))
        self.assertFalse(f.check(deal("싼거", price_krw=5000)).passed)
        self.assertTrue(f.check(deal("적당", price_krw=30000)).passed)
        self.assertFalse(f.check(deal("비싼거", price_krw=90000)).passed)

    def test_unknown_price_is_not_rejected(self):
        f = DealFilter(FilterConfig(min_price_krw=10000, max_age_minutes=None))
        self.assertTrue(f.check(deal("가격모름")).passed)

    def test_rejects_stale_deal(self):
        f = DealFilter(FilterConfig(max_age_minutes=60))
        old = deal("옛날딜", posted_at=datetime.now(timezone.utc) - timedelta(hours=5))
        self.assertFalse(f.check(old).passed)

    def test_keeps_deal_without_timestamp(self):
        # pubDate 없는 소스를 통째로 죽이지 않기 위한 의도적 동작.
        f = DealFilter(FilterConfig(max_age_minutes=60))
        self.assertTrue(f.check(deal("시각없음")).passed)


class ApplyTest(unittest.TestCase):
    def test_apply_keeps_order(self):
        f = DealFilter(FilterConfig(include_keywords=["토스"], max_age_minutes=None))
        deals = [deal("토스1"), deal("무관"), deal("토스2")]
        self.assertEqual([d.title for d in f.apply(deals)], ["토스1", "토스2"])


if __name__ == "__main__":
    unittest.main()
