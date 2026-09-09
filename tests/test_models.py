import unittest
from datetime import datetime, timedelta, timezone

from hotdeal.models import Deal, normalize_url, parse_price_krw, title_key


class NormalizeUrlTest(unittest.TestCase):
    def test_strips_tracking_params_and_fragment(self):
        self.assertEqual(
            normalize_url("https://A.com/p/1?utm_source=x&id=2&fbclid=y#top"),
            "https://a.com/p/1?id=2",
        )

    def test_sorts_query_so_param_order_does_not_split_deals(self):
        self.assertEqual(normalize_url("https://a.com/p?b=2&a=1"), normalize_url("https://a.com/p?a=1&b=2"))

    def test_trailing_slash_is_insignificant(self):
        self.assertEqual(normalize_url("https://a.com/p/"), normalize_url("https://a.com/p"))

    def test_non_url_string_passes_through(self):
        self.assertEqual(normalize_url("  not a url  "), "not a url")


class PriceTest(unittest.TestCase):
    def test_extracts_won_amount(self):
        self.assertEqual(parse_price_krw("[G마켓] 마우스 (12,900원/무료)"), 12900)

    def test_extracts_won_symbol(self):
        self.assertEqual(parse_price_krw("특가 ₩39,000"), 39000)

    def test_returns_none_without_price(self):
        self.assertIsNone(parse_price_krw("무선 이어폰 특가"))

    def test_rejects_implausible_amounts(self):
        self.assertIsNone(parse_price_krw("10원"))


class TitleKeyTest(unittest.TestCase):
    def test_spacing_and_punctuation_do_not_matter(self):
        self.assertEqual(title_key("[G마켓] 무선 마우스!!"), title_key("(G마켓)무선마우스"))


class DealTest(unittest.TestCase):
    def test_rejects_empty_title(self):
        with self.assertRaises(ValueError):
            Deal(source="s", title="   ", url="https://a")

    def test_rejects_empty_url(self):
        with self.assertRaises(ValueError):
            Deal(source="s", title="t", url="")

    def test_fingerprint_ignores_tracking_params(self):
        a = Deal("s", "t", "https://a.com/1?utm_source=x")
        b = Deal("s", "t", "https://a.com/1")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_age_uses_utc_for_naive_datetimes(self):
        deal = Deal("s", "t", "https://a", posted_at=datetime.utcnow() - timedelta(minutes=10))
        self.assertAlmostEqual(deal.age_seconds, 600, delta=30)

    def test_age_is_none_without_timestamp(self):
        self.assertIsNone(Deal("s", "t", "https://a").age_seconds)

    def test_aware_timestamp(self):
        deal = Deal("s", "t", "https://a", posted_at=datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertAlmostEqual(deal.age_seconds, 3600, delta=30)


if __name__ == "__main__":
    unittest.main()
