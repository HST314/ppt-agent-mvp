import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.offline import offline_assets, offline_performance, offline_player
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingModelInspector:
    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "issues": [], "model": "fixture", "browser_received": browser_evidence is not None}


class VisualBrowserInspector:
    def inspect(self, _html, expected_slide_ids, *, visual_quality=False):
        base = {
            "available": True,
            "passed": True,
            "engine": "chromium",
            "engine_version": "139.0.7258.5",
            "viewport": {"width": 1280, "height": 720},
            "elapsed_ms": 12.5,
            "issues": [],
            "slides": [{"slide_id": slide_id} for slide_id in expected_slide_ids],
        }
        if not visual_quality:
            return base
        payloads = []
        screenshots = []
        for index, slide_id in enumerate(expected_slide_ids):
            content = b"RIFF" + (20 + index).to_bytes(4, "little") + b"WEBP" + bytes([index + 1]) * (20 + index)
            digest = hashlib.sha256(content).hexdigest()
            payloads.append({"slide_id": slide_id, "content": content})
            screenshots.append({"slide_id": slide_id, "sha256": digest, "byte_size": len(content), "media_type": "image/webp", "width": 1280, "height": 720})
        return {
            **base,
            "visual_quality": {
                "schema_version": "1.0",
                "score": 91.5,
                "grade": "excellent",
                "advisory": True,
                "composition_score": 92.0,
                "layout_diversity_score": 90.0,
                "theme_rhythm_score": 92.0,
                "slides": [{"slide_id": slide_id, "score": 92.0} for slide_id in expected_slide_ids],
                "screenshots": screenshots,
            },
            "_visual_screenshots": payloads,
        }


class WarningVisualBrowserInspector(VisualBrowserInspector):
    def inspect(self, html, expected_slide_ids, *, visual_quality=False):
        result = super().inspect(html, expected_slide_ids, visual_quality=visual_quality)
        result["passed"] = False
        result["issues"] = [{
            "issue_id": "visual-warning",
            "severity": "warning",
            "level": "deck",
            "code": "repetitive_layout",
            "message": "整稿页面构图重复度偏高",
            "slide_id": "",
            "element_id": "",
            "evidence": "advisory visual score",
            "suggestion": "人工复核",
        }]
        return result


class MissingVisualScreenshotsInspector(VisualBrowserInspector):
    def inspect(self, html, expected_slide_ids, *, visual_quality=False):
        result = super().inspect(html, expected_slide_ids, visual_quality=visual_quality)
        if visual_quality:
            result["visual_quality"]["screenshots"] = []
            result["_visual_screenshots"] = []
        return result


class SharedVisualScreenshotInspector(VisualBrowserInspector):
    def inspect(self, html, expected_slide_ids, *, visual_quality=False):
        result = super().inspect(html, expected_slide_ids, visual_quality=visual_quality)
        if visual_quality:
            content = b"RIFF" + (24).to_bytes(4, "little") + b"WEBP" + b"same-rendered-content-000"
            screenshot_hash = hashlib.sha256(content).hexdigest()
            result["visual_quality"]["screenshots"] = [{
                "slide_id": slide_id,
                "sha256": screenshot_hash,
                "byte_size": len(content),
                "media_type": "image/webp",
                "width": 1280,
                "height": 720,
            } for slide_id in expected_slide_ids]
            result["_visual_screenshots"] = [{"slide_id": slide_id, "content": content} for slide_id in expected_slide_ids]
        return result


class P2VisualOfflineTests(unittest.TestCase):
    def service(self, root, browser_inspector=None):
        store = WorkspaceStore(root)
        service = TaskService(store, inspector=PassingModelInspector(), browser_inspector=browser_inspector or VisualBrowserInspector())
        service.create("visual", "manual")
        service.import_input("visual", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        service.generate_narrative("visual"); service.confirm_narrative("visual")
        service.generate_outline("visual"); service.confirm_outline("visual")
        service.generate_sample("visual"); service.confirm_sample("visual")
        service.generate_deck("visual")
        return service, store

    def test_screenshot_artifacts_are_content_addressed_audited_and_delivered(self):
        with tempfile.TemporaryDirectory() as root:
            service, store = self.service(root)
            inspected = service.run_inspection("visual", 0)
            trace = inspected["evidence_trace"]
            self.assertTrue(trace["valid"], trace["errors"])
            self.assertEqual(len(trace["screenshot_hashes"]), 3)
            quality = inspected["report"]["metadata"]["browser_evidence"]["visual_quality"]
            self.assertEqual(quality["score"], 91.5)
            self.assertEqual(quality["screenshot_count"], 3)

            deck = service.deck_view("visual")["deck"]
            delivery = service.confirm_delivery("visual", deck["hash"])["delivery"]
            delivery_root = store.delivery_root("visual", delivery["delivery_id"])
            result = json.loads((delivery_root / "result.json").read_bytes())
            self.assertEqual(result["visual_screenshot_hashes"], trace["screenshot_hashes"])
            self.assertEqual(result["visual_quality"]["score"], 91.5)
            self.assertTrue(result["offline_performance"]["passed"])
            self.assertEqual(json.loads((delivery_root / "visual-quality.json").read_bytes())["score"], 91.5)
            for screenshot_hash in trace["screenshot_hashes"]:
                path = delivery_root / "visual-screenshots" / f"{screenshot_hash}.webp"
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), screenshot_hash)

    def test_tampered_screenshot_invalidates_evidence_and_delivery_gate(self):
        with tempfile.TemporaryDirectory() as root:
            service, _store = self.service(root)
            inspected = service.run_inspection("visual", 0)
            screenshot_hash = inspected["evidence_trace"]["screenshot_hashes"][0]
            artifact = Path(root) / "visual" / "artifacts" / screenshot_hash
            artifact.write_bytes(artifact.read_bytes() + b"tampered")

            view = service.inspection_view("visual")
            self.assertFalse(view["evidence_trace"]["valid"])
            self.assertFalse(view["delivery_allowed"])
            self.assertTrue(any("视觉质量截图" in item for item in view["evidence_trace"]["errors"]))

    def test_visual_warnings_remain_pending_without_failing_hard_gate(self):
        with tempfile.TemporaryDirectory() as root:
            service, _store = self.service(root, WarningVisualBrowserInspector())
            inspected = service.run_inspection("visual", 0)

            self.assertTrue(inspected["report"]["passed"])
            self.assertTrue(inspected["report"]["metadata"]["quality_checks"]["technical_browser"]["passed"])
            self.assertFalse(inspected["blocking_issues"])
            self.assertEqual([item["severity"] for item in inspected["unresolved"]], ["warning"])
            self.assertTrue(inspected["delivery_allowed"])

    def test_empty_visual_screenshot_lists_fail_before_inspection_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            service, store = self.service(root, MissingVisualScreenshotsInspector())

            with self.assertRaisesRegex(ValidationError, "完整覆盖"):
                service.run_inspection("visual", 0)

            self.assertEqual(store.versions("visual", "inspection"), [])
            self.assertEqual(store.versions("visual", "inspection-evidence"), [])
            self.assertEqual(store.versions("visual", "inspection-screenshot"), [])

    def test_identical_screenshot_bytes_are_shared_by_multiple_page_references(self):
        with tempfile.TemporaryDirectory() as root:
            service, store = self.service(root, SharedVisualScreenshotInspector())
            inspected = service.run_inspection("visual", 0)

            self.assertTrue(inspected["evidence_trace"]["valid"])
            self.assertEqual(len(inspected["evidence_trace"]["screenshot_hashes"]), 1)
            self.assertEqual(len(store.versions("visual", "inspection-screenshot")), 1)
            browser_hash = inspected["report"]["evidence_artifacts"]["technical_browser"]
            browser_document = json.loads(service.version("visual", browser_hash))
            references = browser_document["payload"]["visual_quality"]["screenshots"]
            self.assertEqual([item["slide_id"] for item in references], ["slide-1", "slide-2", "slide-3"])
            self.assertEqual([item["slide_index"] for item in references], [0, 1, 2])
            self.assertEqual(len({item["sha256"] for item in references}), 1)
            self.assertTrue(all(item["deck_hash"] == inspected["report"]["deck_hash"] for item in references))

            deck = service.deck_view("visual")["deck"]
            delivered = service.confirm_delivery("visual", deck["hash"])["delivery"]
            delivery_root = store.delivery_root("visual", delivered["delivery_id"])
            self.assertEqual(len(list((delivery_root / "visual-screenshots").glob("*.webp"))), 1)

    def test_offline_player_deduplicates_motion_and_meets_static_budgets(self):
        deck = '<!doctype html><html><head></head><body><section class="slide"></section><script src="./assets/motion.min.js"></script></body></html>'
        player = offline_player(deck)
        profile = offline_performance(player, offline_assets())

        self.assertEqual(player.count("assets/motion.min.js"), 1)
        self.assertTrue(profile["passed"], profile)
        self.assertEqual(profile["measurements"]["motion_script_references"], 1)
        self.assertLessEqual(profile["measurements"]["player_javascript_bytes"], 4096)
        self.assertEqual(profile["optimizations"]["slide_state_updates"], "O(1) previous/current mutation")

    def test_query_and_fragment_runtime_duplicates_fail_counting_and_delivery_gate(self):
        deck = (
            '<!doctype html><html><head><script src="./assets/motion.min.js?v=one"></script>'
            '<script src="assets/motion.min.js#two"></script>'
            '<script src="assets/offline-player.js?injected=1"></script></head>'
            '<body><section class="slide"></section></body></html>'
        )
        player = offline_player(deck)
        profile = offline_performance(player, offline_assets())
        self.assertFalse(profile["passed"])
        self.assertEqual(profile["measurements"]["motion_script_references"], 2)
        self.assertEqual(profile["measurements"]["player_script_references"], 2)
        self.assertFalse(profile["checks"]["single_motion_runtime"])
        self.assertFalse(profile["checks"]["single_player_runtime"])

        with tempfile.TemporaryDirectory() as root:
            service, store = self.service(root)
            service.run_inspection("visual", 0)
            current = service.deck_view("visual")["deck"]
            original_player = offline_player

            def inject_runtime_duplicates(html):
                value = original_player(html)
                return value.replace(
                    "</body>",
                    '<script src="assets/motion.min.js?duplicate=1"></script>'
                    '<script src="assets/offline-player.js#duplicate"></script></body>',
                )

            with mock.patch("ppt_agent.service.offline_player", side_effect=inject_runtime_duplicates):
                with self.assertRaisesRegex(ConflictError, "性能预算"):
                    service.confirm_delivery("visual", current["hash"])
            self.assertEqual(store.versions("visual", "delivery"), [])


if __name__ == "__main__":
    unittest.main()
