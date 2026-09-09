"""브리지 서버를 실제 소켓에 띄우고 HTTP 로 검증한다."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from bridge.server import Queue, QueueFull, make_handler

TOKEN = "test-token-0123456789"


def pull_url(base, room, max_items=None):
    """한글 방 이름은 반드시 퍼센트 인코딩해야 한다 — 안드로이드 스크립트도
    encodeURIComponent 로 같은 처리를 한다."""
    query = {"room": room}
    if max_items is not None:
        query["max"] = str(max_items)
    return f"{base}/pull?" + urllib.parse.urlencode(query)


def request(url, *, method="GET", body=None, token=TOKEN):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header("X-Bot-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class BridgeServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.queue = Queue(":memory:", lease_seconds=1)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.queue, TOKEN))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_needs_no_token(self):
        status, body = request(f"{self.base}/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_enqueue_requires_token(self):
        status, body = request(f"{self.base}/enqueue", method="POST", body={"room": "r", "text": "t"}, token=None)
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_wrong_token_rejected(self):
        status, _ = request(pull_url(self.base, "r"), token="wrong-token-value")
        self.assertEqual(status, 401)

    def test_enqueue_then_pull_then_ack(self):
        room = "방A"
        status, body = request(f"{self.base}/enqueue", method="POST", body={"room": room, "text": "핫딜1"})
        self.assertEqual(status, 200)
        message_id = body["id"]

        status, body = request(pull_url(self.base, room, 5))
        self.assertEqual([m["text"] for m in body["messages"]], ["핫딜1"])

        # 임대 중이므로 즉시 다시 pull 해도 안 나온다 (중복 전송 방지).
        _, body = request(pull_url(self.base, room, 5))
        self.assertEqual(body["messages"], [])

        status, body = request(f"{self.base}/ack", method="POST", body={"ids": [message_id]})
        self.assertEqual(body["acked"], 1)

        # ack 후에는 임대가 만료돼도 재전달되지 않는다.
        time.sleep(1.2)
        _, body = request(pull_url(self.base, room, 5))
        self.assertEqual(body["messages"], [])

    def test_unacked_message_is_redelivered_after_lease_expiry(self):
        room = "방B"
        request(f"{self.base}/enqueue", method="POST", body={"room": room, "text": "유실되면안됨"})
        _, first = request(pull_url(self.base, room))
        self.assertEqual(len(first["messages"]), 1)

        time.sleep(1.2)  # lease_seconds=1
        _, second = request(pull_url(self.base, room))
        self.assertEqual([m["text"] for m in second["messages"]], ["유실되면안됨"])

    def test_rooms_are_isolated(self):
        request(f"{self.base}/enqueue", method="POST", body={"room": "방C", "text": "C용"})
        _, body = request(pull_url(self.base, "방D"))
        self.assertEqual(body["messages"], [])

    def test_pull_requires_room(self):
        status, body = request(f"{self.base}/pull")
        self.assertEqual(status, 400)
        self.assertIn("room", body["error"])

    def test_enqueue_rejects_empty_text(self):
        status, _ = request(f"{self.base}/enqueue", method="POST", body={"room": "r", "text": "   "})
        self.assertEqual(status, 400)

    def test_ack_rejects_non_integer_ids(self):
        status, _ = request(f"{self.base}/ack", method="POST", body={"ids": ["abc"]})
        self.assertEqual(status, 400)

    def test_malformed_json_is_400_not_500(self):
        req = urllib.request.Request(f"{self.base}/enqueue", data=b"{not json", method="POST")
        req.add_header("X-Bot-Token", TOKEN)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_path_is_404(self):
        status, _ = request(f"{self.base}/nope")
        self.assertEqual(status, 404)

    def test_fifo_order(self):
        room = "방E"
        for i in range(3):
            request(f"{self.base}/enqueue", method="POST", body={"room": room, "text": f"m{i}"})
        _, body = request(pull_url(self.base, room, 10))
        self.assertEqual([m["text"] for m in body["messages"]], ["m0", "m1", "m2"])


class QueueDepthTest(unittest.TestCase):
    def test_queue_refuses_to_grow_without_bound(self):
        # 안드로이드 기기가 죽어 있는 동안 무한히 쌓이면, 살아난 순간 한꺼번에
        # 쏟아져 도배 차단을 맞는다. 그래서 상한에서 거절한다.
        from bridge.server import MAX_QUEUE_DEPTH

        queue = Queue(":memory:")
        for i in range(MAX_QUEUE_DEPTH):
            queue.enqueue("r", f"m{i}")
        with self.assertRaises(QueueFull):
            queue.enqueue("r", "넘침")


if __name__ == "__main__":
    unittest.main()


class InboxTest(unittest.TestCase):
    """기기가 밀어넣은 딜을 파이프라인이 받아가는 통로."""

    @classmethod
    def setUpClass(cls):
        cls.queue = Queue(":memory:")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.queue, TOKEN))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ingest_then_read_inbox(self):
        status, body = request(
            f"{self.base}/ingest",
            method="POST",
            body={"source": "토스핫딜", "deals": [{"title": "신라면 20개입 12,900원", "price_krw": 12900}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

        _, body = request(f"{self.base}/inbox?limit=10")
        titles = [deal["title"] for deal in body["deals"]]
        self.assertIn("신라면 20개입 12,900원", titles)

    def test_inbox_is_not_consumed_by_reading(self):
        # 파이프라인이 죽어도 딜이 유실되면 안 된다. 중복은 파이프라인이 거른다.
        request(f"{self.base}/ingest", method="POST",
                body={"source": "s", "deals": [{"title": "안지워지는딜 1,000원"}]})
        first = request(f"{self.base}/inbox?limit=50")[1]["deals"]
        second = request(f"{self.base}/inbox?limit=50")[1]["deals"]
        self.assertEqual(len(first), len(second))

    def test_same_deal_ingested_twice_stored_once(self):
        payload = {"source": "중복", "deals": [{"title": "같은딜 5,000원", "url": "https://a.example/1"}]}
        self.assertEqual(request(f"{self.base}/ingest", method="POST", body=payload)[1]["accepted"], 1)
        self.assertEqual(request(f"{self.base}/ingest", method="POST", body=payload)[1]["accepted"], 0)

    def test_ingest_requires_token(self):
        status, _ = request(f"{self.base}/ingest", method="POST",
                            body={"source": "s", "deals": [{"title": "x"}]}, token=None)
        self.assertEqual(status, 401)

    def test_ingest_requires_source(self):
        status, body = request(f"{self.base}/ingest", method="POST", body={"deals": [{"title": "x 1,000원"}]})
        self.assertEqual(status, 400)
        self.assertIn("source", body["error"])

    def test_ingest_rejects_non_list_deals(self):
        status, _ = request(f"{self.base}/ingest", method="POST", body={"source": "s", "deals": "nope"})
        self.assertEqual(status, 400)

    def test_ingest_drops_entries_without_title(self):
        status, body = request(
            f"{self.base}/ingest", method="POST",
            body={"source": "부분", "deals": [{"title": "정상 1,000원"}, {"price_krw": 500}, "문자열"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["received"], 1)

    def test_ingest_rejects_batch_that_is_too_large(self):
        from bridge.server import MAX_INGEST_DEALS

        deals = [{"title": f"딜{index} 1,000원"} for index in range(MAX_INGEST_DEALS + 1)]
        status, _ = request(f"{self.base}/ingest", method="POST", body={"source": "s", "deals": deals})
        self.assertEqual(status, 400)

    def test_inbox_requires_token(self):
        self.assertEqual(request(f"{self.base}/inbox", token=None)[0], 401)

    def test_health_reports_inbox_size(self):
        _, body = request(f"{self.base}/health", token=None)
        self.assertIn("inbox", body)


class InboxRollingBufferTest(unittest.TestCase):
    def test_buffer_does_not_grow_without_bound(self):
        from bridge.server import MAX_INBOX_ROWS

        queue = Queue(":memory:")
        for index in range(MAX_INBOX_ROWS + 50):
            queue.ingest("s", [{"title": f"딜 {index}", "url": f"https://a.example/{index}"}])
        self.assertLessEqual(queue.stats()["inbox"], MAX_INBOX_ROWS)

    def test_newest_deals_survive_the_trim(self):
        from bridge.server import MAX_INBOX_ROWS

        queue = Queue(":memory:")
        for index in range(MAX_INBOX_ROWS + 10):
            queue.ingest("s", [{"title": f"딜 {index}", "url": f"https://a.example/{index}"}])
        titles = [deal["title"] for deal in queue.inbox(5)]
        self.assertIn(f"딜 {MAX_INBOX_ROWS + 9}", titles)
