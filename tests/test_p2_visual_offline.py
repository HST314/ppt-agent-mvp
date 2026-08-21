import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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


class P2VisualOfflineTests(unittest.TestCase):
    def service(self, root):
        store = WorkspaceStore(root)
        service = TaskService(store, inspector=PassingModelInspector(), browser_inspector=VisualBrowserInspector())
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

    def test_offline_player_deduplicates_motion_and_meets_static_budgets(self):
        deck = '<!doctype html><html><head></head><body><section class="slide"></section><script src="./assets/motion.min.js"></script></body></html>'
        player = offline_player(deck)
        profile = offline_performance(player, offline_assets())

        self.assertEqual(player.count("assets/motion.min.js"), 1)
        self.assertTrue(profile["passed"], profile)
        self.assertEqual(profile["measurements"]["motion_script_references"], 1)
        self.assertLessEqual(profile["measurements"]["player_javascript_bytes"], 4096)
        self.assertEqual(profile["optimizations"]["slide_state_updates"], "O(1) previous/current mutation")


if __name__ == "__main__":
    unittest.main()
