import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.p2 import canonical
from ppt_agent.p4 import recommend
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class BlockingInspector:
    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {
            "passed": False,
            "model": "blocking-model",
            "issues": [{
                "issue_id": "shared-overflow",
                "severity": "blocker",
                "level": "element",
                "code": "content_out_of_bounds",
                "message": "标题越界",
                "slide_id": "slide-1",
                "element_id": "title",
                "evidence": "模型复核 Chromium 测量为 12px",
                "suggestion": "缩短标题",
            }],
        }


class PassingInspector:
    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "model": "passing-model", "issues": []}


class DuplicateBrowserEvidence:
    enforce_on_generation = False

    def inspect(self, _html, _slide_ids):
        return {
            "available": True,
            "passed": False,
            "engine": "chromium",
            "engine_version": "139.0.7258.5",
            "viewport": {"width": 1280, "height": 720},
            "issues": [{
                "issue_id": "browser-overflow-raw",
                "severity": "blocker",
                "level": "element",
                "code": "content_out_of_bounds",
                "message": "标题越界",
                "slide_id": "slide-1",
                "element_id": "title",
                "evidence": "getBoundingClientRect: 12px",
                "suggestion": "缩短标题",
            }],
        }


class DeckLevelInspector:
    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {
            "passed": False,
            "model": "deck-model",
            "issues": [{
                "issue_id": "model-deck-consistency",
                "severity": "warning",
                "level": "deck",
                "code": "deck_consistency",
                "message": "整稿标题层级不一致",
                "slide_id": "",
                "element_id": "",
                "evidence": "模型发现标题层级差异",
                "suggestion": "统一标题层级",
            }],
        }


class DeckLevelBrowserEvidence:
    enforce_on_generation = False

    def inspect(self, _html, _slide_ids):
        return {
            "available": True,
            "passed": False,
            "engine": "chromium",
            "engine_version": "139.0.7258.5",
            "viewport": {"width": 1280, "height": 720},
            "issues": [{
                "issue_id": "browser-deck-consistency",
                "severity": "warning",
                "level": "deck",
                "code": "deck_consistency",
                "message": "整稿标题层级不一致",
                "slide_id": "",
                "element_id": "",
                "evidence": "浏览器测量发现标题层级差异",
                "suggestion": "统一标题层级",
            }],
        }


def prepare(service, task_id="task", slide_count=4):
    service.create(task_id)
    service.import_input(task_id, {"goal": "发布", "audience": "客户", "topic": "方案", "页数": slide_count})
    service.generate_narrative(task_id)
    service.confirm_narrative(task_id)
    service.generate_outline(task_id)
    service.confirm_outline(task_id)


def prepare_deck(service, task_id="task", slide_count=4):
    prepare(service, task_id, slide_count)
    service.generate_sample(task_id)
    service.confirm_sample(task_id)
    service.generate_deck(task_id)


class RepresentativeSampleTests(unittest.TestCase):
    def test_cover_and_high_information_page_win_over_longest_page_only(self):
        markdown = """# 大纲

## [slide-1] 封面
- 简洁标题

## [slide-2] 架构
- 数据 35%\n- 里程碑 4 个\n- 决策与风险\n- resources://diagram.png

## [slide-3] 普通说明
- 这是一段很长但没有结构角色的普通说明文字，用于证明算法不会继续只按字符长度选择页面。

## [slide-4] 收尾
- 谢谢
"""
        selected, reasons = recommend(markdown, 2)
        self.assertEqual(selected, ["slide-1", "slide-2"])
        self.assertIn("代表开场页", reasons["slide-1"])
        self.assertIn("density=", reasons["slide-2"])

    def test_workflow_persists_strategy_and_rejects_tampered_contract_before_selection(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            prepare(service, slide_count=6)
            selection = service.select_samples("task")["selection"]
            self.assertEqual(selection["slide_ids"][0], "slide-1")
            self.assertNotEqual(selection["slide_ids"][-1], "slide-6")
            self.assertEqual(selection["metadata"]["strategy"], "representative-diversity-v1")

            service.create("tampered")
            service.import_input("tampered", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 4})
            service.generate_narrative("tampered"); service.confirm_narrative("tampered")
            service.generate_outline("tampered"); service.confirm_outline("tampered")
            contract = service.design_contract_view("tampered")
            path = Path(root) / "tampered" / "artifacts" / contract["hash"]
            value = json.loads(path.read_bytes())
            value["slide_ids"].pop()
            path.write_bytes(canonical(value))
            with self.assertRaises(ValidationError):
                service.select_samples("tampered")
            self.assertFalse(service.versions("tampered", "sample-selection"))


class StateAndEvidenceIntegrityTests(unittest.TestCase):
    def test_blocker_state_is_derived_across_disposition_staleness_reinspection_and_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            inspected = service.run_inspection("task", 0)
            self.assertFalse(inspected["state"]["blockers_resolved"])
            self.assertEqual(len(inspected["blocking_issues"]), 1)

            disposed = service.dispose_issue("task", "shared-overflow", "waive", "业务接受", "user")
            self.assertTrue(disposed["state"]["blockers_resolved"])
            self.assertFalse(disposed["blocking_issues"])

            service.modify_deck("task", "统一背景色", scope="global")
            stale = service.inspection_view("task")
            self.assertTrue(stale["report"]["stale"])
            self.assertFalse(stale["state"]["blockers_resolved"])

            service.inspector = PassingInspector()
            service.browser_inspector = None
            fresh = service.run_inspection("task", 0)
            self.assertTrue(fresh["state"]["blockers_resolved"])
            self.assertTrue(fresh["evidence_trace"]["valid"])
            finalized = service.finalize_deck("task", fresh["deck"]["hash"])
            delivered = service.publish_delivery("task")
            self.assertTrue(finalized["state"]["blockers_resolved"])
            self.assertEqual(delivered["state"]["status"], "completed")
            self.assertTrue(delivered["state"]["blockers_resolved"])
            delivery_root = service.store.delivery_root("task", delivered["delivery"]["delivery_id"])
            result = json.loads((delivery_root / "result.json").read_bytes())
            self.assertTrue(result["inspection_evidence_hashes"])
            for evidence_hash in result["inspection_evidence_hashes"]:
                self.assertTrue((delivery_root / "inspection-evidence" / f"{evidence_hash}.json").is_file())

    def test_different_source_ids_merge_by_server_semantics_and_keep_both_origins(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            view = service.run_inspection("task", 0)
            matches = [item for item in view["report"]["issues"] if item["code"] == "content_out_of_bounds"]
            self.assertEqual(len(matches), 1)
            issue = matches[0]
            self.assertRegex(issue["issue_id"], r"^inspection-[0-9a-f]{24}$")
            self.assertEqual(issue["source"], "technical_browser")
            self.assertEqual(set(issue["sources"]), {"semantic_model", "technical_browser"})
            self.assertEqual(len(issue["evidence_refs"]), 2)
            self.assertEqual(
                {(item["source"], item["issue_id"]) for item in issue["source_issues"]},
                {("semantic_model", "shared-overflow"), ("technical_browser", "browser-overflow-raw")},
            )
            self.assertTrue(view["evidence_trace"]["valid"])
            self.assertEqual(view["evidence_trace"]["reference_count"], 2)
            disposed = service.dispose_issue("task", "browser-overflow-raw", "waive", "接受", "user")
            self.assertEqual(disposed["dispositions"][-1]["issue_id"], issue["issue_id"])

    def test_deck_level_findings_with_different_source_ids_share_one_server_identity(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=DeckLevelInspector(), browser_inspector=DeckLevelBrowserEvidence())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            issues = [item for item in report["issues"] if item["code"] == "deck_consistency"]
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["level"], "deck")
            self.assertEqual(issues[0]["slide_id"], "")
            self.assertEqual(issues[0]["element_id"], "")
            self.assertEqual({item["issue_id"] for item in issues[0]["source_issues"]}, {"model-deck-consistency", "browser-deck-consistency"})

    def test_content_addressed_report_still_rejects_issue_payload_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            forged = {key: value for key, value in report.items() if key not in {"hash", "metadata", "stale"}}
            forged["issues"][0]["message"] = "伪造但仍是合法 JSON 的报告内容"
            forged_hash = service.store.put_version("task", "inspection", canonical(forged), report["metadata"])
            with self.assertRaisesRegex(ConflictError, "双向绑定"):
                service._assert_inspection_evidence("task", {**forged, "hash": forged_hash, "metadata": report["metadata"], "stale": False})

    def test_tampered_report_fails_before_disposition_or_finalization_fact(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            path = Path(root) / "task" / "artifacts" / report["hash"]
            value = json.loads(path.read_bytes()); value["issues"] = []; value["passed"] = True
            path.write_bytes(canonical(value))
            view = service.inspection_view("task")
            self.assertFalse(view["evidence_trace"]["valid"])
            self.assertFalse(view["state"]["blockers_resolved"])
            with self.assertRaisesRegex(ConflictError, "报告"):
                service.dispose_issue("task", "shared-overflow", "waive", "接受", "user")
            with self.assertRaisesRegex(ConflictError, "报告"):
                service.finalize_deck("task", service.deck_view("task")["deck"]["hash"])
            self.assertFalse(service.versions("task", "issue-disposition"))
            self.assertFalse(service.versions("task", "final-deck"))

    def test_missing_report_fails_before_delivery_fact_and_package(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=PassingInspector())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            service.finalize_deck("task", service.deck_view("task")["deck"]["hash"])
            (Path(root) / "task" / "artifacts" / report["hash"]).unlink()
            with self.assertRaisesRegex(ConflictError, "报告"):
                service.publish_delivery("task")
            self.assertFalse(service.versions("task", "delivery"))
            self.assertFalse((Path(root) / "task" / "deliveries").exists())

    def test_tampered_evidence_fails_before_disposition_or_finalization_fact(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            evidence_hash = report["issues"][0]["evidence_refs"][0].split("//", 1)[1]
            path = Path(root) / "task" / "artifacts" / evidence_hash
            value = json.loads(path.read_bytes()); value["payload"]["tampered"] = True
            path.write_bytes(canonical(value))
            with self.assertRaisesRegex(ConflictError, "evidence"):
                service.dispose_issue("task", "shared-overflow", "waive", "接受", "user")
            with self.assertRaisesRegex(ConflictError, "evidence"):
                service.finalize_deck("task", service.deck_view("task")["deck"]["hash"], allow_risk=True, risk_rationale="接受")
            self.assertFalse(service.versions("task", "issue-disposition"))
            self.assertFalse(service.versions("task", "final-deck"))

    def test_missing_evidence_fails_before_delivery_fact_and_package(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), inspector=BlockingInspector(), browser_inspector=DuplicateBrowserEvidence())
            prepare_deck(service)
            report = service.run_inspection("task", 0)["report"]
            service.dispose_issue("task", "shared-overflow", "waive", "接受", "user")
            service.finalize_deck("task", service.deck_view("task")["deck"]["hash"])
            evidence_hash = report["issues"][0]["evidence_refs"][0].split("//", 1)[1]
            (Path(root) / "task" / "artifacts" / evidence_hash).unlink()
            with self.assertRaisesRegex(ConflictError, "evidence"):
                service.publish_delivery("task")
            self.assertFalse(service.versions("task", "delivery"))
            self.assertFalse((Path(root) / "task" / "delivery").exists())


if __name__ == "__main__":
    unittest.main()
