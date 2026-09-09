"""파이프라인 동작 검증. 네트워크 대신 가짜 소스/전송기를 끼워 넣는다."""

import unittest
from datetime import datetime, timezone

from hotdeal.config import from_dict
from hotdeal.models import Deal
from hotdeal.pipeline import Pipeline
from hotdeal.senders.base import SendError, Sender
from hotdeal.sources.base import Source, SourceError
from hotdeal.store import DealStore

BASE = {
    "sources": [{"name": "fake", "type": "rss", "url": "https://a.example/rss"}],
    "senders": [{"type": "console"}],
    "filters": {"max_age_minutes": None},
    "send_gap_seconds": 0,
}


class FakeSource(Source):
    def __init__(self, deals, name="fake", error=None):
        self.deals = deals
        self._name = name
        self.error = error
        self.calls = 0

    @property
    def name(self):
        return self._name

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.deals)


class FakeSender(Sender):
    style = "plain"

    def __init__(self, fail=False, name="fake-sender"):
        self.sent = []
        self.fail = fail
        self.batch_size = 1
        self._name = name

    @property
    def name(self):
        return self._name

    def send(self, text):
        if self.fail:
            raise SendError("의도된 실패")
        self.sent.append(text)


def make_pipeline(deals, *, senders=None, sources=None, **overrides):
    config = from_dict({**BASE, **overrides})
    config.db_path = ":memory:"
    pipeline = Pipeline(config, store=DealStore(":memory:"))
    pipeline.sources = sources if sources is not None else [FakeSource(deals)]
    pipeline.senders = senders if senders is not None else [FakeSender()]
    return pipeline


def deal(n, **kwargs):
    return Deal("fake", f"핫딜 {n}", f"https://a.example/{n}", posted_at=datetime.now(timezone.utc), **kwargs)


class SeedTest(unittest.TestCase):
    def test_first_run_sends_nothing_but_records_baseline(self):
        # 이게 없으면 첫 실행에 밀린 피드 전체가 방에 쏟아진다.
        sender = FakeSender()
        pipeline = make_pipeline([deal(i) for i in range(20)], senders=[sender])
        report = pipeline.run_once()
        self.assertTrue(report.seeded)
        self.assertEqual(sender.sent, [])
        self.assertEqual(pipeline.store.count(), 20)

    def test_second_run_sends_only_new_deals(self):
        sender = FakeSender()
        source = FakeSource([deal(1), deal(2)])
        pipeline = make_pipeline(None, sources=[source], senders=[sender])
        pipeline.run_once()  # seed

        source.deals = [deal(1), deal(2), deal(3)]
        report = pipeline.run_once()
        self.assertFalse(report.seeded)
        self.assertEqual(report.new_deals, 1)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("핫딜 3", sender.sent[0])

    def test_seed_can_be_disabled(self):
        sender = FakeSender()
        pipeline = make_pipeline([deal(1)], senders=[sender], seed_on_first_run=False)
        report = pipeline.run_once()
        self.assertFalse(report.seeded)
        self.assertEqual(len(sender.sent), 1)


class DedupTest(unittest.TestCase):
    def test_same_deal_not_sent_twice(self):
        sender = FakeSender()
        pipeline = make_pipeline([deal(1)], senders=[sender], seed_on_first_run=False)
        pipeline.run_once()
        pipeline.run_once()
        self.assertEqual(len(sender.sent), 1)

    def test_filtered_out_deals_are_marked_seen(self):
        # 매 주기 같은 딜을 재평가할 이유가 없다.
        pipeline = make_pipeline(
            [deal(1)], seed_on_first_run=False, filters={"include_keywords": ["없는키워드"], "max_age_minutes": None}
        )
        pipeline.run_once()
        self.assertEqual(pipeline.store.count(), 1)


class RateLimitTest(unittest.TestCase):
    def test_caps_sends_per_run_and_resumes_next_run(self):
        sender = FakeSender()
        source = FakeSource([deal(i) for i in range(5)])
        pipeline = make_pipeline(None, sources=[source], senders=[sender], seed_on_first_run=False, max_sends_per_run=2)

        report = pipeline.run_once()
        self.assertEqual(report.new_deals, 5)
        self.assertEqual(len(sender.sent), 2)

        pipeline.run_once()
        self.assertEqual(len(sender.sent), 4)  # 남은 것이 이어서 나간다
        pipeline.run_once()
        self.assertEqual(len(sender.sent), 5)
        pipeline.run_once()
        self.assertEqual(len(sender.sent), 5)  # 더 보낼 것 없음


class FailureIsolationTest(unittest.TestCase):
    def test_one_dead_source_does_not_stop_the_others(self):
        good = FakeSource([deal(1)], name="good")
        bad = FakeSource([], name="bad", error=SourceError("피드 다운"))
        sender = FakeSender()
        pipeline = make_pipeline(None, sources=[bad, good], senders=[sender], seed_on_first_run=False)

        report = pipeline.run_once()
        self.assertIn("bad", report.source_errors)
        self.assertEqual(len(sender.sent), 1)

    def test_unexpected_source_exception_is_contained(self):
        bad = FakeSource([], name="boom", error=RuntimeError("예상 못한 오류"))
        pipeline = make_pipeline(None, sources=[bad], senders=[FakeSender()], seed_on_first_run=False)
        report = pipeline.run_once()
        self.assertIn("boom", report.source_errors)

    def test_one_dead_sender_does_not_block_the_other(self):
        ok, broken = FakeSender(name="ok"), FakeSender(fail=True, name="broken")
        pipeline = make_pipeline([deal(1)], senders=[broken, ok], seed_on_first_run=False)
        report = pipeline.run_once()
        self.assertIn("broken", report.sender_errors)
        self.assertEqual(len(ok.sent), 1)

    def test_deal_is_retried_when_every_sender_fails(self):
        broken = FakeSender(fail=True)
        source = FakeSource([deal(1)])
        pipeline = make_pipeline(None, sources=[source], senders=[broken], seed_on_first_run=False)
        pipeline.run_once()
        self.assertEqual(pipeline.store.count(), 0)  # '봤음' 표시를 하지 않는다

        broken.fail = False
        pipeline.run_once()
        self.assertEqual(len(broken.sent), 1)


class DryRunTest(unittest.TestCase):
    def test_dry_run_sends_nothing_and_records_nothing(self):
        sender = FakeSender()
        pipeline = make_pipeline([deal(1)], senders=[sender], seed_on_first_run=False)
        report = pipeline.run_once(dry_run=True)
        self.assertEqual(sender.sent, [])
        self.assertEqual(pipeline.store.count(), 0)
        self.assertEqual(report.new_deals, 1)


class OrderingTest(unittest.TestCase):
    def test_oldest_deal_is_sent_first(self):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        newer = Deal("fake", "새 딜", "https://a.example/new", posted_at=now)
        older = Deal("fake", "옛 딜", "https://a.example/old", posted_at=now - timedelta(minutes=30))
        sender = FakeSender()
        pipeline = make_pipeline([newer, older], senders=[sender], seed_on_first_run=False)
        pipeline.run_once()
        self.assertIn("옛 딜", sender.sent[0])
        self.assertIn("새 딜", sender.sent[1])


if __name__ == "__main__":
    unittest.main()


class BatchingTest(unittest.TestCase):
    """카카오는 짧은 메시지를 연달아 쏘면 도배로 판정한다 → 묶어 보내야 한다."""

    def test_batch_size_reduces_message_count(self):
        sender = FakeSender()
        sender.batch_size = 3
        pipeline = make_pipeline([deal(i) for i in range(6)], senders=[sender], seed_on_first_run=False)
        report = pipeline.run_once()
        self.assertEqual(len(sender.sent), 2)  # 6건 → 2통
        self.assertEqual(report.sent_messages, 2)
        self.assertEqual(sender.sent[0].count("🔥"), 3)

    def test_each_sender_uses_its_own_batch_size(self):
        chatty, terse = FakeSender(name="chatty"), FakeSender(name="terse")
        chatty.batch_size = 1
        terse.batch_size = 4
        pipeline = make_pipeline([deal(i) for i in range(4)], senders=[chatty, terse], seed_on_first_run=False)
        pipeline.run_once()
        self.assertEqual(len(chatty.sent), 4)
        self.assertEqual(len(terse.sent), 1)

    def test_partial_failure_records_only_what_went_out(self):
        # 3건 묶음 2통 중 두 번째가 실패하면, 첫 묶음만 '봤음' 처리돼야 한다.
        class FlakySender(FakeSender):
            fail_from = 1  # 이 통수부터 실패

            def send(self, text):
                if len(self.sent) >= self.fail_from:
                    raise SendError("두 번째 묶음에서 실패")
                self.sent.append(text)

        sender = FlakySender()
        sender.batch_size = 3
        source = FakeSource([deal(i) for i in range(6)])
        pipeline = make_pipeline(None, sources=[source], senders=[sender], seed_on_first_run=False)
        report = pipeline.run_once()

        self.assertEqual(len(sender.sent), 1)
        self.assertIn("fake-sender", report.sender_errors)
        self.assertEqual(pipeline.store.count(), 3)  # 나간 3건만 기록

        # 못 나간 3건은 어댑터가 복구되면 다음 주기에 재시도된다.
        sender.fail_from = 99
        pipeline.run_once()
        self.assertEqual(len(sender.sent), 2)
        self.assertEqual(pipeline.store.count(), 6)
