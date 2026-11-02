"""Durable product snapshots preserve evidence and remain outside app bundles."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from product_cache import ProductCache, ProductCacheError, default_cache_directory


class ProductCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.product_id = "123456789012"
        self.source = self.base / "source"
        self.analysis = self.source / "analysis" / self.product_id
        self.analysis.mkdir(parents=True)
        self.export = self.source / f"tmall_{self.product_id}_original.json"
        self.export_payload = {
            "product_id": self.product_id, "product": {"title": "缓存回归商品"},
            "reviews": [{"review_id": "r1", "date": "2026-10-20", "sku_id": "A",
                         "content": "穿了两天后袖口起球，表面出现明显小毛球。"}],
            "collection_info": {"finished_at": "2026-10-29T12:00:00+08:00", "scanned_count": 300, "matched_count": 33},
        }
        self.write(self.export, self.export_payload)
        self.summary = {"product_id": self.product_id, "analysis_status": "completed",
                        "created_at": "2026-10-29T13:00:00+08:00", "input": {"digest": "original-digest"},
                        "valid_review_count": 22, "issue_count": 15, "cluster_count": 7, "claim_count": 6}
        self.write(self.analysis / "analysis_summary.json", self.summary)
        for name in ("statistics.json", "cleaned_reviews.json", "issue_clusters.json", "issues_flat.json"):
            self.write(self.analysis / name, {"reviews": self.export_payload["reviews"]})
        self.write(self.analysis / "extracted_issues.json", {"reviews": [{"review_id": "r1", "status": "ok", "errors": [], "issues": []}]})
        self.cache = ProductCache(self.base / "private-cache")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, **kwargs):
        return self.cache.save(self.product_id, [self.export], self.analysis, **kwargs)

    def restore(self, **kwargs):
        return self.cache.restore(self.product_id, self.base / "restored/exports", self.base / "restored/analysis", **kwargs)

    def test_roundtrip_survives_new_instance_and_keeps_source_bytes_and_dates(self):
        manifest = self.save()
        self.cache = ProductCache(self.cache.root)
        restored = self.restore()
        self.assertEqual(restored["snapshot_id"], manifest["snapshot_id"])
        self.assertEqual(Path(restored["export_files"][0]).read_bytes(), self.export.read_bytes())
        target = Path(restored["analysis_dir"])
        self.assertEqual((target / "analysis_summary.json").read_bytes(), (self.analysis / "analysis_summary.json").read_bytes())
        self.assertEqual(restored["analysis_created_at"], self.summary["created_at"])
        self.assertEqual(restored["collection_finished_at"], self.export_payload["collection_info"]["finished_at"])

    def test_manifest_keeps_real_counts_digest_and_analysis_status(self):
        manifest = self.save()
        for key, value in {"valid_review_count": 22, "issue_count": 15, "cluster_count": 7,
                           "claim_count": 6, "scanned_count": 300, "matched_count": 33,
                           "input_digest": "original-digest", "has_analysis": True}.items():
            self.assertEqual(manifest[key], value)
        self.assertNotIn("source_path", json.dumps(manifest))

    def test_identical_save_deduplicates_without_rejuvenating_cache_timestamp(self):
        first = self.save()
        second = self.save()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["stored_at"], second["stored_at"])
        self.assertEqual(len(list((self.cache.root / self.product_id / "snapshots").iterdir())), 1)

    def test_changed_snapshot_is_atomic_and_old_snapshot_is_retained(self):
        first = self.save()
        self.summary["valid_review_count"] = 23
        self.write(self.analysis / "analysis_summary.json", self.summary)
        second = self.save()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(self.cache.get(self.product_id)["valid_review_count"], 23)
        self.assertTrue((self.cache.root / self.product_id / "snapshots" / first["snapshot_id"]).is_dir())

    def test_failed_new_analysis_never_replaces_complete_snapshot(self):
        first = self.save()
        self.summary["analysis_status"] = "blocked_missing_llm"
        self.write(self.analysis / "analysis_summary.json", self.summary)
        second = self.save()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(self.cache.get(self.product_id)["analysis_status"], "completed")

    def test_collected_only_snapshot_is_allowed_but_not_over_complete_result(self):
        cache = ProductCache(self.base / "collection-cache")
        collected = cache.save(self.product_id, [self.export], None)
        self.assertFalse(collected["has_analysis"])
        first = self.save()
        second = self.cache.save(self.product_id, [self.export], None)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_only_allowed_intermediates_and_caches_are_saved(self):
        (self.analysis / "llm_cache.jsonl").write_text('{"key":"hash","status":"ok","issues":[]}\n', encoding="utf-8")
        self.write(self.analysis / "embedding_cache.json", {"embedding": [1, 2]})
        (self.analysis / "report.pdf").write_bytes(b"%PDF-private-generated")
        self.write(self.analysis / "api-settings.json", {"api_key": "dummy-must-not-be-read"})
        self.write(self.analysis / "arbitrary.json", {"extra": "not-whitelisted"})
        manifest = self.save()
        names = {item["name"] for item in manifest["files"]}
        self.assertIn("llm_cache.jsonl", names)
        self.assertIn("embedding_cache.json", names)
        self.assertFalse(names.intersection({"report.pdf", "api-settings.json", "arbitrary.json"}))

    def test_credentials_rejected_without_creating_snapshot(self):
        payload = copy.deepcopy(self.export_payload)
        payload["headers"] = {"Authorization": "dummy-secret"}
        self.write(self.export, payload)
        with self.assertRaises(ProductCacheError):
            self.save()
        self.assertFalse(self.cache.root.exists())

    def test_credentials_in_jsonl_cannot_replace_existing_good_snapshot(self):
        first = self.save()
        (self.analysis / "llm_cache.jsonl").write_text('{"api_key":"dummy-secret"}\n', encoding="utf-8")
        with self.assertRaises(ProductCacheError):
            self.save()
        self.assertEqual(self.cache.get(self.product_id)["snapshot_id"], first["snapshot_id"])

    def test_token_usage_counts_are_not_mistaken_for_credentials(self):
        (self.analysis / "llm_calls.jsonl").write_text('{"usage":{"prompt_tokens":20,"cached_tokens":10}}\n', encoding="utf-8")
        self.assertTrue(self.save()["has_analysis"])

    def test_wrong_product_export_and_analysis_are_rejected(self):
        payload = copy.deepcopy(self.export_payload)
        payload["product_id"] = "999"
        self.write(self.export, payload)
        with self.assertRaises(ProductCacheError):
            self.save()
        self.write(self.export, self.export_payload)
        self.summary["product_id"] = "999"
        self.write(self.analysis / "analysis_summary.json", self.summary)
        with self.assertRaises(ProductCacheError):
            self.save()

    def test_missing_evidence_dates_are_not_filled_from_cache_time(self):
        self.export_payload["reviews"][0].pop("date")
        self.write(self.export, self.export_payload)
        self.save()
        restored = self.restore()
        payload = json.loads(Path(restored["export_files"][0]).read_text(encoding="utf-8"))
        self.assertNotIn("date", payload["reviews"][0])

    def test_restore_checks_all_integrity_before_creating_targets(self):
        manifest = self.save()
        source = self.cache.root / self.product_id / "snapshots" / manifest["snapshot_id"] / "files/analysis/statistics.json"
        source.write_text('{"changed":true}', encoding="utf-8")
        self.assertIsNone(self.cache.get(self.product_id))
        self.assertIsNone(self.restore())
        self.assertFalse((self.base / "restored").exists())

    def test_manifest_path_traversal_is_not_followed(self):
        manifest = self.save()
        path = self.cache.root / self.product_id / "snapshots" / manifest["snapshot_id"] / "manifest.json"
        modified = json.loads(path.read_text(encoding="utf-8"))
        modified["files"][0]["name"] = "../../outside.json"
        self.write(path, modified)
        self.assertIsNone(self.restore())
        self.assertFalse((self.base / "restored").exists())

    def test_snapshot_pointer_cannot_escape_its_product(self):
        self.save()
        self.write(self.cache.root / self.product_id / "current.json", {"version": "product-cache-v1", "product_id": self.product_id, "snapshot": "../../outside"})
        self.assertIsNone(self.cache.get(self.product_id))

    def test_existing_different_session_data_is_not_overwritten_by_default(self):
        self.save()
        target = self.base / "restored/exports" / self.export.name
        target.parent.mkdir(parents=True)
        target.write_text("new session data", encoding="utf-8")
        with self.assertRaises(ProductCacheError):
            self.restore()
        self.assertEqual(target.read_text(encoding="utf-8"), "new session data")
        self.assertFalse((self.base / "restored/analysis").exists())
        self.assertIsNotNone(self.restore(overwrite=True))

    def test_same_byte_existing_targets_can_be_restored_repeatedly(self):
        self.save()
        self.restore()
        self.assertIsNotNone(self.restore())

    def test_images_and_ocr_cache_use_product_specific_directory(self):
        product_dir = self.source / "products" / self.product_id
        image = product_dir / "images/main_01.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image")
        self.write(image.parent / "ocr_cache.json", {"image-hash": ["商品文字"]})
        (image.parent / "browser_cookie.json").write_text('{"cookie":"ignored-private"}', encoding="utf-8")
        self.save(product_dir=product_dir)
        destination = self.base / "restored/products" / self.product_id
        self.restore(product_dir=destination)
        self.assertEqual((destination / "images/main_01.png").read_bytes(), image.read_bytes())
        self.assertFalse((destination / "images/browser_cookie.json").exists())

    def test_invalid_product_ids_and_source_directories_do_not_enter_cache(self):
        for value in ("../outside", "abc", "123/other", "１23"):
            with self.assertRaises(ProductCacheError):
                self.cache.save(value, [self.export], self.analysis)
        with self.assertRaises(ProductCacheError):
            self.cache.save(self.product_id, [self.export], self.source / "analysis")

    def test_unowned_nonempty_cache_directory_is_not_adopted(self):
        self.cache.root.mkdir()
        (self.cache.root / "user-file.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ProductCacheError):
            self.save()
        self.assertEqual((self.cache.root / "user-file.txt").read_text(encoding="utf-8"), "keep")

    def test_missing_cache_is_read_only_and_lists_empty(self):
        self.assertIsNone(self.restore())
        self.assertFalse(self.cache.has(self.product_id))
        self.assertEqual(self.cache.list(), [])
        self.assertFalse(self.cache.root.exists())

    def test_history_list_keeps_latest_product_metadata_and_cannot_mutate_store(self):
        self.save()
        other = "998877"
        payload = copy.deepcopy(self.export_payload)
        payload["product_id"] = other
        payload["product"]["title"] = "另一件历史商品"
        other_export = self.source / f"tmall_{other}.json"
        self.write(other_export, payload)
        self.cache.save(other, [other_export], None)
        records = self.cache.list()
        self.assertEqual([item["product_id"] for item in records], [other, self.product_id])
        self.assertEqual(records[0]["product_title"], "另一件历史商品")
        records[1]["files"][0]["name"] = "changed-in-caller.json"
        self.assertNotIn("changed-in-caller.json", {f["name"] for f in self.cache.get(self.product_id)["files"]})

    def test_size_limit_is_enforced_before_any_cache_write(self):
        with patch("product_cache.MAX_FILE_BYTES", 8):
            with self.assertRaises(ProductCacheError):
                self.save()
        self.assertFalse(self.cache.root.exists())

    def test_source_link_is_rejected(self):
        link = self.source / "linked.json"
        try:
            link.symlink_to(self.export)
        except OSError:
            self.skipTest("Creating symlinks requires Windows privilege")
        with self.assertRaises(ProductCacheError):
            self.cache.save(self.product_id, [link], self.analysis)

    def test_default_directory_is_user_scoped(self):
        if os.name == "nt":
            with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "user-local")}):
                self.assertEqual(default_cache_directory(), self.base / "user-local/ReviewInsight/product-cache")


if __name__ == "__main__":
    unittest.main()
