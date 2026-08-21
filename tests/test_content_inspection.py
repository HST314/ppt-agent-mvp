import unittest

from ppt_agent.content_inspection import inspect_content_quality


def deck(body: str) -> str:
    return f'<!doctype html><html><body><section class="slide" id="slide-1" data-slide-id="slide-1">{body}</section></body></html>'


class ContentInspectionTests(unittest.TestCase):
    def test_visible_placeholders_block_but_non_visible_source_text_is_ignored(self):
        result = inspect_content_quality(deck(
            '<style>.note::after{content:"XXX"}</style>'
            '<script>const pending = "TBD"</script>'
            '<p hidden>XX%</p>'
            '<p data-element-id="metric">覆盖 XX 条业务线，日均处理 XXX 次对话</p>'
        ), {"goal": "汇报试点"})

        self.assertFalse(result["passed"])
        self.assertEqual(len(result["issues"]), 2)
        self.assertTrue(all(item["severity"] == "blocker" for item in result["issues"]))
        self.assertTrue(all(item["source"] == "semantic_deterministic" for item in result["issues"]))
        self.assertTrue(all(item["element_id"] == "metric" for item in result["issues"]))

    def test_default_fields_block_and_explicit_missing_data_is_a_warning(self):
        result = inspect_content_quality(deck(
            '<p>汇报日期 · 汇报部门</p><p>下一阶段转化率：数据待确认</p>'
        ), {"topic": "经营汇报"})

        by_code = {item["code"]: item for item in result["issues"]}
        self.assertEqual(by_code["unbound_default_field"]["severity"], "blocker")
        self.assertEqual(by_code["unconfirmed_fact"]["severity"], "warning")

    def test_frozen_source_binding_accepts_known_facts_and_flags_unbound_kpis(self):
        result = inspect_content_quality(deck(
            '<p>已确认预算 80 万元</p><p>目标转化率提升 35%</p><p>共分 4 项推进</p>'
        ), {"known_facts": {"budget": "80万元"}})

        self.assertEqual(len(result["issues"]), 2)
        by_evidence = {item["evidence"]: item for item in result["issues"]}
        percentage = next(item for evidence, item in by_evidence.items() if "35%" in evidence)
        structural = next(item for evidence, item in by_evidence.items() if "4 项" in evidence)
        self.assertEqual(percentage["severity"], "blocker")
        self.assertEqual(structural["severity"], "warning")
        self.assertFalse(any("80" in item["evidence"] for item in result["issues"]))

    def test_reported_p0_claims_and_single_x_date_placeholder_are_blockers(self):
        result = inspect_content_quality(deck(
            '<div data-element-id="body">'
            '<p>已经完成 12 周试点，SLA <strong>99.5%</strong></p>'
            '<p>扩容后业务量覆盖 <strong>3×</strong>，预期季度节省 100 万+</p>'
            '<p>会后 3 个工作日内输出项目章程</p>'
            '<p>响应时间下降 42%</p>'
            '<p>2025 年 X 月 X 日</p>'
            '</div>'
        ), {"known_facts": {"response_time": "响应时间下降42%"}})

        blockers = [item for item in result["issues"] if item["severity"] == "blocker"]
        evidence = "\n".join(item["evidence"] for item in blockers)
        for token in ("12 周", "99.5%", "3×", "100 万+", "3 个工作日", "2025 年 X 月 X 日"):
            self.assertIn(token, evidence)
        self.assertFalse(any("42%" in item["evidence"] for item in result["issues"]))
        self.assertTrue(all(item["element_id"] == "body" for item in blockers))

    def test_disclosed_unknown_metric_warns_without_hiding_other_unbound_claims(self):
        result = inspect_content_quality(deck(
            '<p>试点 SLA 99.5%（数据待确认）</p><p>预计节省 100 万+</p>'
        ), {"topic": "试点"})

        codes = [(item["code"], item["severity"], item["evidence"]) for item in result["issues"]]
        self.assertTrue(any(code == "unconfirmed_fact" and severity == "warning" for code, severity, _ in codes))
        self.assertFalse(any("99.5%" in evidence and code.startswith("unverified") for code, _, evidence in codes))
        self.assertTrue(any("100 万+" in evidence and severity == "blocker" for _, severity, evidence in codes))


if __name__ == "__main__":
    unittest.main()
