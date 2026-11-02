"""任务持久化、恢复与取消的单元测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_store import JobStore, first_product_id


class JobStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, source="live"):
        return self.store.create(
            product_url="https://detail.tmall.com/item.htm?id=123",
            product_id="123",
            mode="count",
            value=100,
            unit="reviews",
            scope={"mode": "count", "count": 100},
            source=source,
        )

    def test_create_and_get(self):
        record = self.create()
        self.assertEqual(record["status"], "queued")
        loaded = self.store.get(record["id"])
        self.assertEqual(loaded["product_id"], "123")
        self.assertTrue((self.root / "runtime" / "jobs" / f"{record['id']}.json").exists())

    def test_persists_across_instances(self):
        record = self.create()
        self.store.update(record["id"], status="awaiting_user", intervention={"kind": "platform_validate"})
        another = JobStore(self.root)
        loaded = another.get(record["id"])
        self.assertEqual(loaded["status"], "awaiting_user")
        self.assertEqual(loaded["intervention"]["kind"], "platform_validate")

    def test_checkpoint_roundtrip_and_list_excludes_checkpoint(self):
        record = self.create()
        self.store.save_checkpoint(record["id"], {"product_id": "123", "next_page": 3, "reviews": [{"review_id": "r1"}]})
        self.assertEqual(self.store.load_checkpoint(record["id"])["next_page"], 3)
        recent = self.store.list_recent(10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["id"], record["id"])

    def test_begin_resume_is_atomic(self):
        record = self.create()
        self.store.update(record["id"], status="awaiting_user")
        ok, _ = self.store.begin_resume(record["id"])
        self.assertTrue(ok)
        # 第二次点击必须被拒绝，避免重复执行
        ok2, message = self.store.begin_resume(record["id"])
        self.assertFalse(ok2)
        self.assertIn("恢复中", message)
        self.assertEqual(self.store.get(record["id"])["resume_count"], 1)

    def test_begin_resume_rejects_wrong_status(self):
        record = self.create()
        ok, message = self.store.begin_resume(record["id"])
        self.assertFalse(ok)
        self.assertIn("queued", message)

    def test_cancel_waiting_job(self):
        record = self.create()
        self.store.update(record["id"], status="awaiting_user")
        ok, _ = self.store.request_cancel(record["id"])
        self.assertTrue(ok)
        loaded = self.store.get(record["id"])
        self.assertEqual(loaded["status"], "cancelled")
        self.assertIsNone(loaded["intervention"])
        ok2, message = self.store.request_cancel(record["id"])
        self.assertFalse(ok2)
        self.assertIn("已结束", message)

    def test_cancel_during_collecting_keeps_status_for_worker(self):
        record = self.create()
        self.store.update(record["id"], status="collecting")
        ok, _ = self.store.request_cancel(record["id"])
        self.assertTrue(ok)
        loaded = self.store.get(record["id"])
        self.assertEqual(loaded["status"], "collecting")
        self.assertTrue(loaded["cancel_requested"])
        self.assertTrue(self.store.cancel_requested(record["id"]))

    def test_awaiting_since_and_clear(self):
        record = self.create()
        self.store.update(record["id"], status="awaiting_user")
        self.assertTrue(self.store.get(record["id"])["awaiting_since"])
        self.store.update(record["id"], status="collecting")
        self.assertEqual(self.store.get(record["id"])["awaiting_since"], "")

    def test_progress_patch_merges(self):
        record = self.create()
        self.store.patch_progress(record["id"], {"scanned": 10}, stage="读取评价分页")
        self.store.patch_progress(record["id"], {"matched": 2})
        loaded = self.store.get(record["id"])
        self.assertEqual(loaded["progress"]["scanned"], 10)
        self.assertEqual(loaded["progress"]["matched"], 2)
        self.assertEqual(loaded["stage"], "读取评价分页")

    def test_summarize_hides_checkpoint_data(self):
        record = self.create()
        payload = self.store.summarize(record)
        self.assertEqual(payload["status"], "queued")
        self.assertNotIn("checkpoint", payload)

    def test_summarize_exposes_resumable(self):
        record = self.create()
        self.assertFalse(self.store.summarize(record)["resumable"])
        self.store.update(record["id"], status="failed", resumable=True, resumable_reason="断线")
        payload = self.store.summarize(self.store.get(record["id"]))
        self.assertTrue(payload["resumable"])
        self.assertEqual(payload["resumable_reason"], "断线")
        self.assertTrue(payload["can_resume"])  # failed + resumable 也允许继续

    def test_find_active_scans_all_jobs(self):
        for _ in range(25):
            old = self.create()
            self.store.update(old["id"], status="cancelled")
        target = self.create()
        self.store.update(target["id"], status="awaiting_user")
        active = self.store.find_active()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], target["id"])

    def test_find_active_none(self):
        record = self.create()
        self.store.update(record["id"], status="completed")
        self.assertIsNone(self.store.find_active())

    def test_first_product_id(self):
        self.assertEqual(first_product_id("https://detail.tmall.com/item.htm?id=987"), "987")
        self.assertEqual(first_product_id("987"), "987")
        self.assertEqual(first_product_id("https://example.com/x"), "")


if __name__ == "__main__":
    unittest.main()
