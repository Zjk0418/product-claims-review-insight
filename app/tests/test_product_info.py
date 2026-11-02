"""商品信息采集的纯函数自检：缩略图过滤与 OCR 覆盖参数。

背景（2026-10-28）：详情页里的宣传文案图（“宽松版型 阔腿显瘦”“甄选优质面料”）
之前被两类问题挡住——详情图只下载 10 张、OCR 只跑前 12 张且主图排在前面，
导致文案图根本没进 OCR；缩略图（_60x60）倒是混进来出噪声。
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import product  # noqa: E402


class ThumbnailFilterTest(unittest.TestCase):
    def test_small_resized_images_are_thumbnails(self):
        self.assertTrue(product._is_thumbnail("https://img.alicdn.com/x.jpg_60x60.jpg"))
        self.assertTrue(product._is_thumbnail("https://img.alicdn.com/x.jpg_100x100q90.jpg"))
        self.assertTrue(product._is_thumbnail("https://img.alicdn.com/x.png_80x80.png"))

    def test_full_size_images_are_kept(self):
        self.assertFalse(product._is_thumbnail("https://img.alicdn.com/x.jpg"))
        self.assertFalse(product._is_thumbnail("https://img.alicdn.com/x.jpg_790x10000.jpg"))
        self.assertFalse(product._is_thumbnail("https://img.alicdn.com/x.jpg_760x760.jpg"))
        self.assertFalse(product._is_thumbnail("https://img.alicdn.com/x.png_430x430q90.png"))

    def test_empty_url_is_not_thumbnail(self):
        self.assertFalse(product._is_thumbnail(""))
        self.assertFalse(product._is_thumbnail(None))


class OcrCoverageTest(unittest.TestCase):
    def test_defaults_cover_detail_images(self):
        signature = inspect.signature(product.collect_product_info)
        self.assertEqual(signature.parameters["max_detail_images"].default, 30)
        self.assertEqual(signature.parameters["max_ocr_images"].default, 40)
        self.assertGreaterEqual(
            signature.parameters["max_ocr_images"].default,
            signature.parameters["max_detail_images"].default + 5,
            "OCR 名额必须覆盖全部详情图（主图排在后面）",
        )


class OcrCacheTest(unittest.TestCase):
    """OCR 缓存行为：空结果也缓存、相同内容只识别一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_single = product._ocr_single
        self.calls: list[Path] = []

    def tearDown(self):
        product._ocr_single = self._orig_single

    def _track(self, result):
        def wrapper(path):
            self.calls.append(Path(path))
            return result
        product._ocr_single = wrapper

    def _image(self, name: str, content: bytes) -> Path:
        path = Path(self.tmp.name) / name
        path.write_bytes(content)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_empty_result_is_cached_and_not_recomputed(self):
        path = self._image("a.jpg", b"fake-bytes-a")
        self._track(None)
        cache: dict = {}
        out1, _ = product.ocr_images([path], cache=cache)
        self.assertEqual(out1, {})
        self.assertTrue(next(iter(cache.values())).get("empty"))
        out2, _ = product.ocr_images([path], cache=cache)
        self.assertEqual(out2, {})
        self.assertEqual(len(self.calls), 1, "第二次应命中缓存，不再进引擎")

    def test_identical_content_is_ocr_once(self):
        first = self._image("a.jpg", b"same-bytes")
        second = self._image("b.jpg", b"same-bytes")
        self._track({"text": "宽松版型", "confidence": 0.9, "lines": [{"text": "宽松版型", "score": 0.9}]})
        cache: dict = {}
        out, _ = product.ocr_images([first, second], cache=cache)
        self.assertEqual(out[str(first)]["text"], out[str(second)]["text"])
        self.assertEqual(len(self.calls), 1, "相同内容只识别一次")

    def test_changed_content_is_reocr(self):
        first = self._image("a.jpg", b"old-bytes")
        cache: dict = {}
        self._track(None)
        product.ocr_images([first], cache=cache)
        # 同一路径但内容变化：哈希不同，必须重新识别
        first.write_bytes(b"new-bytes")
        self.calls.clear()
        self._track({"text": "甄选优质面料", "confidence": 0.9,
                     "lines": [{"text": "甄选优质面料", "score": 0.9}]})
        out, _ = product.ocr_images([first], cache=cache)
        self.assertEqual(out[str(first)]["text"], "甄选优质面料")
        self.assertEqual(len(self.calls), 1)


class FinishDetailImagesTest(unittest.TestCase):
    """后台预取线程的核心逻辑：下载→OCR→缓存→回填 ocr_text。"""

    def setUp(self):
        self.item_id = "_test_finish"
        self.orig = (product.download_images, product.ocr_images)
        self.addCleanup(self._restore)

    def _restore(self):
        product.download_images, product.ocr_images = self.orig
        shutil.rmtree(product.ROOT / "runtime" / "products" / self.item_id, ignore_errors=True)

    def test_attaches_ocr_and_writes_cache(self):
        entry = {
            "url": "https://img.alicdn.com/x.jpg",
            "local_path": "runtime/products/_test_finish/images/detail_01.jpg",
            "kind": "detail", "ocr_text": "", "ocr_confidence": None,
        }
        def fake_download(urls, target_dir, kind, limit, **kwargs):
            Path(target_dir).mkdir(parents=True, exist_ok=True)
            return [dict(entry)]
        product.download_images = fake_download
        abs_path = str(product.ROOT / entry["local_path"])

        def fake_ocr(paths, cache=None, **kwargs):
            self.assertEqual(len(paths), 1)
            data = {abs_path: {"text": "宽松版型 阔腿显瘦", "confidence": 0.95,
                               "lines": [{"text": "宽松版型 阔腿显瘦", "score": 0.95}]}}
            if cache is not None:
                # 文件是伪造的，哈希随便取一个稳定值即可
                cache["fake-digest"] = data[abs_path]
            return data, "ok"

        product.ocr_images = fake_ocr
        result = product.finish_detail_images(self.item_id, ["https://img.alicdn.com/x.jpg"], "详情文案")
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail_text"], "详情文案")
        self.assertEqual(result["detail_files"][0]["ocr_text"], "宽松版型 阔腿显瘦")
        cache_path = product.ROOT / "runtime" / "products" / self.item_id / "images" / "ocr_cache.json"
        self.assertTrue(cache_path.exists())

    def test_cancel_between_stages_aborts(self):
        product.download_images = lambda *a, **k: []
        result = product.finish_detail_images(
            self.item_id, ["https://img.alicdn.com/x.jpg"], "", cancel_check=lambda: True,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "cancelled")


if __name__ == "__main__":
    unittest.main()
