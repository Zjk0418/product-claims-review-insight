import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from job_store import JobStore


class ResumeTabHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name))
        self.patch = patch.object(server, "STORE", self.store)
        self.patch.start()
        self.record = self.store.create(product_url="https://detail.tmall.com/item.htm?id=123456",
                                        product_id="123456", mode="count", value=100, unit="reviews", scope={})
        self.job_id = self.record["id"]
        self.checkpoint = {"product_id": "123456", "tab_id": "old-collection-tab", "tab_owned": True,
                           "next_page": 4, "scanned": 150, "seen": ["r1"], "reviews": [{"review_id": "r1"}]}
        self.store.save_checkpoint(self.job_id, self.checkpoint)
        self.store.update(self.job_id, status="resuming", tab_id="new-foreground-tab")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def load(self, **changes):
        record = {**self.store.get(self.job_id), **changes}
        return server._load_checkpoint(self.job_id, record)

    def test_resume_uses_latest_task_tab_and_preserves_collected_progress(self):
        loaded = self.load()
        self.assertEqual(loaded["tab_id"], "new-foreground-tab")
        self.assertFalse(loaded["tab_owned"])
        for key in ("product_id", "next_page", "scanned", "seen", "reviews"):
            self.assertEqual(loaded[key], self.checkpoint[key])
        # Do not rewrite collected data just because a UI action selected a tab.
        self.assertEqual(self.store.load_checkpoint(self.job_id), self.checkpoint)

    def test_same_tab_preserves_its_original_ownership_metadata(self):
        loaded = self.load(tab_id="old-collection-tab")
        self.assertEqual(loaded, self.checkpoint)
        self.assertTrue(loaded["tab_owned"])

    def test_empty_latest_tab_does_not_discard_the_checkpoint_tab(self):
        self.assertEqual(self.load(tab_id=""), self.checkpoint)

    def test_another_task_record_cannot_supply_a_replacement_tab(self):
        self.assertEqual(self.load(id="another-task"), self.checkpoint)

    def test_another_product_record_cannot_supply_a_replacement_tab(self):
        self.assertEqual(self.load(product_id="999999"), self.checkpoint)

    def test_new_collection_does_not_load_a_stale_checkpoint(self):
        with patch.object(self.store, "load_checkpoint") as read:
            loaded = self.load(status="queued", resume_count=0)
        self.assertIsNone(loaded)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
