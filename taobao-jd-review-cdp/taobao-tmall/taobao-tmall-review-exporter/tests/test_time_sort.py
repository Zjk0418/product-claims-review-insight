import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "tmall_rate_auto.py"


def load_module():
    spec = importlib.util.spec_from_file_location("tmall_rate_auto", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TimeSortTests(unittest.TestCase):
    def test_runtime_is_isolated_inside_taobao_project(self):
        module = load_module()
        project_root = SCRIPT.parents[2]
        self.assertEqual(module.PROJECT_ROOT, project_root)
        self.assertEqual(module.DEFAULT_OUTPUT_ROOT, project_root / "runtime" / "reviews")

    def test_feedbackdate_order_is_sent_to_mtop(self):
        module = load_module()
        payload = json.loads(
            module.build_data(
                item_id="942697712178",
                page_no=1,
                page_size=20,
                order_type="feedbackdate",
            )
        )
        self.assertEqual(payload["orderType"], "feedbackdate")


if __name__ == "__main__":
    unittest.main()
