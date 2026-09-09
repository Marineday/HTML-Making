"""토스 UI 덤프 파서. 실기기 없이 픽스처로 검증한다."""

import unittest
from pathlib import Path

from tools.toss_capture import (
    CapturedDeal,
    build_tree,
    card_to_deal,
    extract_deals,
    find_cards,
    parse_bounds,
)

FIXTURE = Path(__file__).parent / "fixtures" / "toss_hotdeal_dump.xml"


def load():
    return FIXTURE.read_bytes()


def wrap(nodes_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        f'<node index="0" text="" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1080,2340]">'
        f"{nodes_xml}</node></hierarchy>"
    ).encode("utf-8")


class BoundsTest(unittest.TestCase):
    def test_parses_bounds(self):
        self.assertEqual(parse_bounds("[42,220][1038,520]"), (42, 220, 1038, 520))

    def test_missing_bounds_is_zero(self):
        self.assertEqual(parse_bounds(""), (0, 0, 0, 0))

    def test_negative_coordinates(self):
        self.assertEqual(parse_bounds("[-10,0][100,50]"), (-10, 0, 100, 50))


class TreeTest(unittest.TestCase):
    def test_builds_nested_tree(self):
        root = build_tree(load())
        self.assertTrue(root.children)

    def test_collects_descendant_texts_in_screen_order(self):
        root = build_tree(load())
        texts = root.texts()
        self.assertLess(texts.index("농심 신라면 20개입"), texts.index("코카콜라 제로 190ml 30캔"))

    def test_malformed_xml_raises(self):
        with self.assertRaises(ValueError):
            build_tree(b"<hierarchy><node")


class CardDetectionTest(unittest.TestCase):
    def test_finds_only_innermost_cards(self):
        # 바깥 컨테이너까지 카드로 잡으면 화면 전체가 딜 하나가 되어버린다.
        cards = find_cards(build_tree(load()))
        self.assertEqual(len(cards), 4)  # 딜 3개 + 하단 네비게이션

    def test_single_text_element_is_not_a_card(self):
        # 헤더의 "전체보기" 는 clickable 이지만 텍스트가 하나뿐이다.
        titles = [deal.title for deal in extract_deals(load(), price_only=False)]
        self.assertNotIn("전체보기", titles)


class DealExtractionTest(unittest.TestCase):
    def test_extracts_deals_with_prices(self):
        deals = extract_deals(load())
        self.assertEqual(len(deals), 2)

    def test_picks_discounted_price_not_original(self):
        # 카드에 18,000원(원가)과 12,900원(할인가)이 함께 있다.
        deal = next(d for d in extract_deals(load()) if "신라면" in d.title)
        self.assertEqual(deal.price_krw, 12900)

    def test_price_is_appended_to_title_for_downstream_unit_pricing(self):
        deal = next(d for d in extract_deals(load()) if "신라면" in d.title)
        self.assertIn("12,900원", deal.title)
        self.assertIn("20개입", deal.title)

    def test_navigation_bar_is_not_a_deal(self):
        titles = [deal.title for deal in extract_deals(load(), price_only=False)]
        self.assertFalse(any(title in ("홈", "혜택", "전체") for title in titles))

    def test_priceless_card_excluded_by_default(self):
        titles = [deal.title for deal in extract_deals(load())]
        self.assertFalse(any("다우니" in title for title in titles))

    def test_priceless_card_included_when_requested(self):
        titles = [deal.title for deal in extract_deals(load(), price_only=False)]
        self.assertTrue(any("다우니" in title for title in titles))

    def test_duplicate_titles_collapsed(self):
        card = '<node index="0" text="" class="android.view.ViewGroup" clickable="true" bounds="[0,{y}][100,{y2}]">' \
               '<node text="같은상품" clickable="false" bounds="[0,{y}][50,{y2}]"/>' \
               '<node text="1,000원" clickable="false" bounds="[50,{y}][100,{y2}]"/></node>'
        xml = wrap(card.format(y=100, y2=200) + card.format(y=300, y2=400))
        self.assertEqual(len(extract_deals(xml)), 1)

    def test_empty_screen_yields_nothing(self):
        self.assertEqual(extract_deals(wrap("")), [])


class CardToDealTest(unittest.TestCase):
    def _card(self, *texts):
        nodes = "".join(
            f'<node text="{text}" clickable="false" bounds="[0,{index * 10}][100,{index * 10 + 10}]"/>'
            for index, text in enumerate(texts)
        )
        xml = wrap(f'<node index="0" class="android.view.ViewGroup" clickable="true" bounds="[0,0][100,100]">{nodes}</node>')
        return find_cards(build_tree(xml))[0]

    def test_longest_non_chrome_text_becomes_the_title(self):
        deal = card_to_deal(self._card("무료배송", "삼다수 2L 12병", "8,900원"))
        self.assertIn("삼다수 2L 12병", deal.title)

    def test_percentage_is_not_a_title(self):
        deal = card_to_deal(self._card("30%", "신라면 20개입", "12,900원"))
        self.assertTrue(deal.title.startswith("신라면"))

    def test_card_with_only_chrome_words_is_dropped(self):
        self.assertIsNone(card_to_deal(self._card("홈", "혜택", "전체")))

    def test_absurd_price_ignored(self):
        deal = card_to_deal(self._card("이상한상품", "999,999,999,999원"))
        self.assertIsNone(deal.price_krw)

    def test_payload_shape_matches_ingest_contract(self):
        payload = CapturedDeal(title="신라면 20개입 12,900원", price_krw=12900).to_payload()
        self.assertEqual(set(payload), {"title", "price_krw", "url"})


if __name__ == "__main__":
    unittest.main()
