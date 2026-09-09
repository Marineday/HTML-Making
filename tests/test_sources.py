import unittest

from hotdeal.config import SourceConfig
from hotdeal.sources.base import SourceError
from hotdeal.sources.html_list import HtmlListSource
from hotdeal.sources.rss import RssSource

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>피드</title>
  <item>
    <title><![CDATA[[G마켓] 토스페이 할인 (9,900원/무료)]]></title>
    <link>https://www.ppomppu.co.kr/zboard/view.php?id=1&amp;no=2</link>
    <guid>pp-1</guid>
    <pubDate>Wed, 09 Sep 2026 01:00:00 +0900</pubDate>
    <description>본문에는 12,000원</description>
  </item>
  <item><title>링크 없는 항목</title></item>
  <item><link>https://a.example/2</link></item>
</channel></rss>""".encode()

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>토스 8시특가 커피</title>
    <link href="https://a.example/1"/>
    <id>a1</id>
    <updated>2026-09-09T00:30:00Z</updated>
  </entry>
</feed>""".encode()

PAGE = """<html><body>
  <a href="/bbs/qb_saleinfo/views/100"><img src="thumb.png"></a>
  <a href="/bbs/qb_saleinfo/views/100">[쿠팡] 토스페이 7,900원 특가</a>
  <a href="/bbs/qb_saleinfo/views/101">[11번가] 이어폰 &amp; 케이스</a>
  <a href="/member/login">로그인</a>
</body></html>"""


def rss_source(**kwargs):
    return RssSource(SourceConfig(name="feed", type="rss", url="https://x/rss", **kwargs), client=None)


class RssTest(unittest.TestCase):
    def test_parses_rss_item(self):
        deals = rss_source().parse(RSS)
        self.assertEqual(len(deals), 1)  # link/title 없는 항목은 버린다
        deal = deals[0]
        self.assertEqual(deal.title, "[G마켓] 토스페이 할인 (9,900원/무료)")
        self.assertEqual(deal.url, "https://www.ppomppu.co.kr/zboard/view.php?id=1&no=2")
        self.assertEqual(deal.raw_id, "pp-1")
        self.assertEqual(deal.price_krw, 9900)  # 본문보다 제목을 우선한다
        self.assertIsNotNone(deal.posted_at)

    def test_parses_atom_entry(self):
        deals = rss_source().parse(ATOM)
        self.assertEqual(deals[0].url, "https://a.example/1")
        self.assertEqual(deals[0].raw_id, "a1")
        self.assertIsNotNone(deals[0].posted_at)

    def test_respects_max_items(self):
        self.assertEqual(len(rss_source(max_items=0).parse(RSS)), 0)

    def test_empty_feed_is_not_an_error(self):
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
        self.assertEqual(rss_source().parse(empty), [])

    def test_malformed_xml_raises_source_error(self):
        with self.assertRaises(SourceError):
            rss_source().parse(b"<rss><channel>")


def html_source(**kwargs):
    kwargs.setdefault("link_pattern", r"/bbs/qb_saleinfo/views/\d+")
    return HtmlListSource(
        SourceConfig(name="qz", type="html_list", url="https://quasarzone.com/bbs/qb_saleinfo",
                     base_url="https://quasarzone.com", **kwargs),
        client=None,
    )


class HtmlListTest(unittest.TestCase):
    def test_extracts_matching_links_only(self):
        deals = html_source().parse(PAGE)
        self.assertEqual(len(deals), 2)
        self.assertNotIn("로그인", [d.title for d in deals])

    def test_collapses_thumbnail_duplicate_of_same_post(self):
        # 목록 페이지는 같은 글을 썸네일과 제목으로 두 번 링크한다.
        urls = [d.url for d in html_source().parse(PAGE)]
        self.assertEqual(len(urls), len(set(urls)))

    def test_resolves_relative_urls_against_base(self):
        self.assertTrue(html_source().parse(PAGE)[0].url.startswith("https://quasarzone.com/"))

    def test_unescapes_entities_in_title(self):
        self.assertIn("&", html_source().parse(PAGE)[1].title)

    def test_extracts_price_from_anchor_text(self):
        self.assertEqual(html_source().parse(PAGE)[0].price_krw, 7900)

    def test_posted_at_is_none_so_age_filter_is_skipped(self):
        self.assertIsNone(html_source().parse(PAGE)[0].posted_at)

    def test_invalid_regex_raises_source_error(self):
        with self.assertRaises(SourceError):
            html_source(link_pattern="[unclosed").parse(PAGE)

    def test_no_match_returns_empty_not_error(self):
        self.assertEqual(html_source(link_pattern=r"nothing-matches").parse(PAGE), [])


if __name__ == "__main__":
    unittest.main()
