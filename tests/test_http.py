import gzip
import time
import unittest
import zlib

from hotdeal.http import DEFAULT_USER_AGENT, HttpClient, _decode_body, detect_charset


class CharsetTest(unittest.TestCase):
    """한국 커뮤니티 상당수가 아직 EUC-KR 이다. UTF-8 로 고정하면 제목이
    통째로 깨져 필터도 중복제거도 무의미해진다."""

    def test_header_charset_wins(self):
        body = "토스페이".encode("cp949")
        self.assertEqual(detect_charset(body, "euc-kr"), "cp949")

    def test_meta_charset_is_used_when_header_absent(self):
        body = "<html><head><meta charset='euc-kr'></head><body>토스</body></html>".encode("cp949")
        self.assertEqual(detect_charset(body), "cp949")

    def test_euc_kr_is_upgraded_to_cp949_superset(self):
        # euc-kr 로 선언해 놓고 확장 문자를 쓰는 페이지가 많다.
        self.assertEqual(detect_charset(b"x", "EUC-KR"), "cp949")

    def test_utf8_detected_without_declaration(self):
        self.assertEqual(detect_charset("토스페이".encode("utf-8")), "utf-8")

    def test_cp949_detected_without_declaration(self):
        self.assertEqual(detect_charset("토스페이".encode("cp949")), "cp949")

    def test_unknown_codec_in_header_is_ignored(self):
        self.assertEqual(detect_charset("토스".encode("utf-8"), "not-a-real-codec"), "utf-8")

    def test_korean_survives_round_trip(self):
        original = "[G마켓] 토스페이 5천원 할인"
        body = f"<html><meta charset='euc-kr'><a>{original}</a></html>".encode("cp949")
        self.assertIn(original, body.decode(detect_charset(body)))


class DecodeBodyTest(unittest.TestCase):
    def test_gzip(self):
        self.assertEqual(_decode_body(gzip.compress(b"hi"), "gzip"), b"hi")

    def test_deflate_zlib_wrapped(self):
        self.assertEqual(_decode_body(zlib.compress(b"hi"), "deflate"), b"hi")

    def test_raw_deflate(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(b"hi") + compressor.flush()
        self.assertEqual(_decode_body(raw, "deflate"), b"hi")

    def test_identity(self):
        self.assertEqual(_decode_body(b"hi", ""), b"hi")


class ThrottleTest(unittest.TestCase):
    def test_same_host_requests_are_spaced_out(self):
        # 공격적으로 때리면 차단당한다. 이게 봇이 죽는 가장 흔한 원인이다.
        client = HttpClient(min_interval_per_host=0.3)
        started = time.monotonic()
        client._throttle("a.example")
        client._throttle("a.example")
        self.assertGreaterEqual(time.monotonic() - started, 0.28)

    def test_different_hosts_are_not_blocked_by_each_other(self):
        client = HttpClient(min_interval_per_host=0.3)
        started = time.monotonic()
        client._throttle("a.example")
        client._throttle("b.example")
        self.assertLess(time.monotonic() - started, 0.2)

    def test_default_user_agent_is_a_real_string(self):
        # slots=True 데이터클래스에서 클래스 속성으로 기본값을 읽으면
        # 문자열이 아니라 슬롯 디스크립터가 나온다. 모듈 상수를 써야 한다.
        self.assertIsInstance(DEFAULT_USER_AGENT, str)
        self.assertIsInstance(HttpClient().user_agent, str)


if __name__ == "__main__":
    unittest.main()
