import unittest

from hotdeal.pricing import Dimension, parse_quantity, product_key, unit_price


def q(title):
    return parse_quantity(title)


class CountParsingTest(unittest.TestCase):
    def test_gaeib(self):
        self.assertEqual(q("농심 신라면 20개입").count, 20)

    def test_plain_count(self):
        self.assertEqual(q("햇반 210g 24개").count, 24)

    def test_various_korean_units(self):
        for title, expected in [
            ("마스크 100매", 100), ("생수 12병", 12), ("비타민 60정", 60),
            ("커피 30포", 30), ("휴지 30롤", 30), ("스팸 12캔", 12),
        ]:
            self.assertEqual(q(title).count, expected, title)

    def test_multiplier_and_unit_are_not_double_counted(self):
        # "190ml*30캔" 에서 *30 과 30캔 은 같은 숫자다. 900이 되면 안 된다.
        self.assertEqual(q("코카콜라 190ml*30캔").count, 30)
        self.assertEqual(q("생수 2L x 12병").count, 12)

    def test_bare_multiplier_counts(self):
        self.assertEqual(q("코카콜라 190ml*30").count, 30)

    def test_price_is_not_read_as_quantity(self):
        self.assertIsNone(q("에어팟 프로 289,000원").count)

    def test_percent_is_not_read_as_quantity(self):
        self.assertIsNone(q("전 품목 30% 할인").count)

    def test_time_token_is_not_read_as_quantity(self):
        self.assertIsNone(q("토스 8시특가 에어팟").count)

    def test_implausible_count_is_rejected(self):
        self.assertIsNone(q("볼트 99999개").count)


class MeasureParsingTest(unittest.TestCase):
    def test_volume_multiplied_by_count(self):
        quantity = q("코카콜라 190ml*30캔")
        self.assertEqual(quantity.volume_ml, 5700)

    def test_litre_converted_to_ml(self):
        self.assertEqual(q("다우니 2.5L").volume_ml, 2500)

    def test_weight_kg_converted_to_grams(self):
        self.assertEqual(q("사료 2kg").weight_g, 2000)

    def test_weight_multiplied_by_count(self):
        self.assertEqual(q("햇반 210g 24개").weight_g, 5040)

    def test_no_quantity_at_all(self):
        quantity = q("에어팟 프로 2세대")
        self.assertIsNone(quantity.dimension)
        self.assertEqual(quantity.describe(), "")


class DimensionTest(unittest.TestCase):
    def test_count_beats_volume_and_weight(self):
        self.assertIs(q("코카콜라 190ml*30캔").dimension, Dimension.COUNT)

    def test_volume_used_when_no_count(self):
        self.assertIs(q("다우니 2.5L").dimension, Dimension.VOLUME)

    def test_weight_used_when_no_count_or_volume(self):
        self.assertIs(q("사료 2kg").dimension, Dimension.WEIGHT)


class UnitPriceTest(unittest.TestCase):
    def test_per_item(self):
        price, dimension = unit_price(12900, q("신라면 20개입"))
        self.assertAlmostEqual(price, 645.0)
        self.assertIs(dimension, Dimension.COUNT)

    def test_per_100ml(self):
        price, dimension = unit_price(5000, q("섬유유연제 2.5L"))
        self.assertAlmostEqual(price, 200.0)  # 5000원 / 25단위(100ml)
        self.assertIs(dimension, Dimension.VOLUME)

    def test_per_100g(self):
        price, dimension = unit_price(20000, q("사료 2kg"))
        self.assertAlmostEqual(price, 1000.0)

    def test_returns_none_without_quantity(self):
        self.assertIsNone(unit_price(289000, q("에어팟 프로")))

    def test_returns_none_for_nonpositive_price(self):
        self.assertIsNone(unit_price(0, q("신라면 20개입")))


class ProductKeyTest(unittest.TestCase):
    def test_mall_name_is_stripped_so_same_product_matches_across_malls(self):
        # 이게 안 되면 몰마다 다른 상품으로 갈려 비교 표본이 절대 안 쌓인다.
        gmarket = product_key("[G마켓] 농심 신라면 20개입 (12,900원/무료)")
        coupang = product_key("[쿠팡] 신라면 20개입 7,900원")
        self.assertTrue(gmarket.matches(coupang))

    def test_quantity_tokens_are_excluded_from_identity(self):
        self.assertTrue(product_key("신라면 20개입").matches(product_key("신라면 10개입")))

    def test_promotional_words_are_excluded(self):
        self.assertTrue(product_key("역대급 초특가 신라면 무료배송").matches(product_key("신라면")))

    def test_different_products_do_not_match(self):
        self.assertFalse(product_key("신라면").matches(product_key("진라면")))

    def test_variant_is_treated_as_different_product(self):
        self.assertFalse(product_key("신라면").matches(product_key("신라면블랙")))

    def test_superset_matches_subset(self):
        self.assertTrue(product_key("농심 신라면 컵").matches(product_key("신라면")))

    def test_unusable_key_never_matches(self):
        self.assertFalse(product_key("30% 5,000원").usable)
        self.assertFalse(product_key("30%").matches(product_key("신라면")))


if __name__ == "__main__":
    unittest.main()
