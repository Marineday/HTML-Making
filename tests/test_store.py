import unittest

from hotdeal.models import Deal
from hotdeal.store import DealStore


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = DealStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_new_deal_then_seen(self):
        deal = Deal("pp", "마우스 12,900원", "https://a.example/1")
        self.assertTrue(self.store.is_new(deal))
        self.store.mark_seen(deal)
        self.assertFalse(self.store.is_new(deal))

    def test_tracking_params_do_not_defeat_dedup(self):
        self.store.mark_seen(Deal("pp", "마우스", "https://a.example/1"))
        self.assertFalse(self.store.is_new(Deal("pp", "마우스", "https://a.example/1?utm_source=kakao")))

    def test_same_deal_from_another_community_is_deduped_by_title(self):
        self.store.mark_seen(Deal("뽐뿌", "[G마켓] 무선 마우스 12,900원", "https://a.example/1"))
        cross = Deal("루리웹", "[G마켓] 무선마우스 12,900원", "https://b.example/9")
        self.assertFalse(self.store.is_new(cross))
        self.assertTrue(self.store.is_new(cross, cross_source_title_dedup=False))

    def test_different_deals_both_new(self):
        self.store.mark_seen(Deal("pp", "마우스", "https://a.example/1"))
        self.assertTrue(self.store.is_new(Deal("pp", "키보드", "https://a.example/2")))

    def test_mark_many_is_idempotent(self):
        deals = [Deal("pp", "a", "https://a.example/1"), Deal("pp", "b", "https://a.example/2")]
        self.store.mark_many(deals)
        self.store.mark_many(deals)
        self.assertEqual(self.store.count(), 2)

    def test_prune_removes_everything_older_than_zero_days(self):
        self.store.mark_seen(Deal("pp", "a", "https://a.example/1"))
        self.assertEqual(self.store.prune(0), 1)
        self.assertEqual(self.store.count(), 0)

    def test_prune_keeps_recent(self):
        self.store.mark_seen(Deal("pp", "a", "https://a.example/1"))
        self.assertEqual(self.store.prune(30), 0)

    def test_state_survives_reopen(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        deal = Deal("pp", "a", "https://a.example/1")
        with DealStore(path) as store:
            store.mark_seen(deal)
        with DealStore(path) as store:
            self.assertFalse(store.is_new(deal))


if __name__ == "__main__":
    unittest.main()
