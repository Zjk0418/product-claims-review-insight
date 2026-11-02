"""分析进度聚合的单元测试：阶段合并、百分比换算、单调不回退。"""

import unittest

import server


class StageProgressTest(unittest.TestCase):
    def make_job(self) -> server.AnalysisJob:
        return server.AnalysisJob(id="job-1", product_id="123")

    def test_running_stage_uses_sub_progress(self):
        job = self.make_job()
        server.record_stage(job, {"step": "gaps", "status": "running", "done": 5, "total": 10})
        self.assertEqual(job.step, "gaps")
        self.assertEqual(job.stage, "宣传—体验落差判断")
        self.assertGreater(job.progress, 85)
        self.assertLess(job.progress, 99)

    def test_progress_never_moves_backwards(self):
        job = self.make_job()
        server.record_stage(job, {"step": "gaps", "status": "running", "done": 8, "total": 10})
        high = job.progress
        # 并发批次回传可能乱序，进度不能被旧的计数拉回去
        server.record_stage(job, {"step": "gaps", "status": "running", "done": 2, "total": 10})
        self.assertEqual(job.progress, high)

    def test_finished_stage_records_detail_and_reaches_span_end(self):
        job = self.make_job()
        server.record_stage(job, {"step": "extract", "status": "ok", "issue_count": 42})
        row = job.steps[0]
        self.assertEqual(row["label"], "LLM 抽取负面问题")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["detail"], "42 个问题")
        self.assertEqual(job.progress, server.STAGE_SPAN["extract"][1])

    def test_running_callback_cannot_downgrade_finished_stage(self):
        job = self.make_job()
        server.record_stage(job, {"step": "claims", "status": "ok", "claim_count": 31})
        server.record_stage(job, {"step": "claims", "status": "running", "done": 1, "total": 9})
        self.assertEqual(job.steps[0]["status"], "ok")
        self.assertEqual(job.steps[0]["detail"], "31 条主张")

    def test_skipped_stage_is_marked_and_explained(self):
        job = self.make_job()
        server.record_stage(job, {"step": "claims", "status": "blocked", "reason": "missing_llm_config"})
        row = job.steps[0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["detail"], "未配置模型，已跳过")

    def test_unknown_steps_do_not_reset_progress(self):
        job = self.make_job()
        server.record_stage(job, {"step": "gaps", "status": "ok", "gap_count": 7})
        before = job.progress
        server.record_stage(job, {"status": "running"})  # 缺 step，应被忽略
        self.assertEqual(job.progress, before)
        self.assertEqual(job.steps[0]["detail"], "7 条判断")

    def test_stage_plan_and_spans_cover_the_same_stages(self):
        self.assertEqual(list(server.STAGE_LABELS), list(server.STAGE_SPAN))
        spans = list(server.STAGE_SPAN.values())
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], 99)


if __name__ == "__main__":
    unittest.main()
