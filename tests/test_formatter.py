import unittest

from hotdeal.formatter import MAX_TITLE_CHARS, format_batch, format_deal
from hotdeal.models import Deal

DEAL = Deal("뽐뿌", "[G마켓] 토스페이 <할인> & 특가", "https://a.example/1", price_krw=9900)


class FormatterTest(unittest.TestCase):
    def test_plain_has_no_markup(self):
        text = format_deal(DEAL, "plain")
        self.assertIn("https://a.example/1", text)
        self.assertIn("9,900원", text)
        self.assertNotIn("<b>", text)
        self.assertNotIn("**", text)

    def test_html_escapes_special_characters(self):
        text = format_deal(DEAL, "html")
        self.assertIn("&lt;할인&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("<b>", text)

    def test_markdown_uses_bold(self):
        self.assertIn("**", format_deal(DEAL, "markdown"))

    def test_long_title_is_clipped_for_kakao(self):
        long_deal = Deal("s", "가" * 300, "https://a.example/1")
        first_line = format_deal(long_deal, "plain").splitlines()[0]
        self.assertLessEqual(len(first_line), MAX_TITLE_CHARS + 2)
        self.assertTrue(first_line.endswith("…"))

    def test_price_line_omitted_when_unknown(self):
        self.assertNotIn("💰", format_deal(Deal("s", "t", "https://a.example/1"), "plain"))

    def test_batch_joins_with_blank_line(self):
        text = format_batch([DEAL, DEAL], "plain")
        self.assertEqual(text.count("🔥"), 2)
        self.assertIn("\n\n", text)


if __name__ == "__main__":
    unittest.main()
