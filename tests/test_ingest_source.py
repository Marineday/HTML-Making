import json
import unittest

from hotdeal.config import SourceConfig
from hotdeal.models import Deal
from hotdeal.sources.base import SourceError
from hotdeal.sources.ingest import IngestSource


def source(**kwargs):
    kwargs.setdefault("token", "t")
    return IngestSource(
        SourceConfig(name="토스핫딜", type="ingest", url="http://127.0.0.1:8080", **kwargs), client=None
    )


def body(deals, ok=True, error=None):
    payload = {"ok": ok, "deals": deals}
    if error:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False).encode()


class IngestSourceTest(unittest.TestCase):
    def test_parses_deal(self):
        deals = source().parse(body([{"title": "신라면 20개입 12,900원", "price_krw": 12900, "url": ""}]))
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].price_krw, 12900)
        self.assertEqual(deals[0].source, "토스핫딜")

    def test_missing_url_gets_a_stable_pseudo_url(self):
        # 토스 앱 카드에는 웹 링크가 없다. 중복제거 키는 있어야 한다.
        first = source().parse(body([{"title": "신라면 20개입 12,900원"}]))[0]
        second = source().parse(body([{"title": "신라면 20개입 12,900원"}]))[0]
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertFalse(first.has_web_link)

    def test_real_url_is_kept(self):
        deal = source().parse(body([{"title": "딜 1,000원", "url": "https://shop.example/1"}]))[0]
        self.assertTrue(deal.has_web_link)
        self.assertEqual(deal.url, "https://shop.example/1")

    def test_price_falls_back_to_title_parsing(self):
        deal = source().parse(body([{"title": "신라면 20개입 12,900원"}]))[0]
        self.assertEqual(deal.price_krw, 12900)

    def test_invalid_price_falls_back_to_title(self):
        deal = source().parse(body([{"title": "신라면 12,900원", "price_krw": "이상한값"}]))[0]
        self.assertEqual(deal.price_krw, 12900)

    def test_entry_without_title_is_skipped(self):
        self.assertEqual(source().parse(body([{"price_krw": 100}])), [])

    def test_respects_max_items(self):
        deals = [{"title": f"딜{index} 1,000원"} for index in range(10)]
        self.assertEqual(len(source(max_items=3).parse(body(deals))), 3)

    def test_bridge_error_becomes_source_error(self):
        with self.assertRaisesRegex(SourceError, "브리지 오류"):
            source().parse(body([], ok=False, error="토큰이 올바르지 않습니다"))

    def test_malformed_json_becomes_source_error(self):
        with self.assertRaises(SourceError):
            source().parse(b"{not json")

    def test_posted_at_is_set_so_freshness_filter_works(self):
        self.assertIsNotNone(source().parse(body([{"title": "딜 1,000원"}]))[0].posted_at)


if __name__ == "__main__":
    unittest.main()
