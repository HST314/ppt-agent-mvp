from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ppt_agent.errors import RuntimeUnavailableError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


ROOT = Path(__file__).resolve().parents[1]


class ReleaseReadinessTests(unittest.TestCase):
    def test_disabled_rollout_is_visible_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(
                WorkspaceStore(root),
                feature_flags={"skill_runtime_v2": False, "technical_gate_v2": True},
            )
            status = service.release_status()
            self.assertFalse(status["write_enabled"])
            self.assertFalse(status["legacy_implementation_present"])
            self.assertEqual(status["rollback_mode"], "traffic_to_previous_release")
            self.assertEqual(service.initialize_runtime()["status"], "rollout_disabled")
            with self.assertRaises(RuntimeUnavailableError) as caught:
                service.require_runtime_ready()
            self.assertEqual(caught.exception.failed_check, "skill_runtime_v2")
            self.assertEqual(caught.exception.runtime_error_code, "release_feature_disabled")

    def test_technical_gate_switch_cannot_be_used_to_bypass_delivery_checks(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(
                WorkspaceStore(root),
                feature_flags={"skill_runtime_v2": True, "technical_gate_v2": False},
            )
            with self.assertRaises(RuntimeUnavailableError) as caught:
                service.require_release_write_enabled()
            self.assertEqual(caught.exception.failed_check, "technical_gate_v2")

    def test_release_matrix_has_all_required_automated_lanes(self):
        matrix_script = (ROOT / "scripts/verify_release_matrix.py").read_text(encoding="utf-8")
        real_model_script = ROOT / "scripts/verify_real_model_release.py"
        for lane in (
            "standard",
            "architecture",
            "browser",
            "generation",
            "offline",
            "real_model",
        ):
            self.assertIn(f'"{lane}"', matrix_script)
        self.assertTrue(real_model_script.is_file())
        workflow = (ROOT / ".github/workflows/release-matrix.yml").read_text(encoding="utf-8")
        self.assertIn("verify_release_matrix.py --profile full", workflow)
        self.assertIn("verify_release_matrix.py --profile real-model", workflow)

    def test_release_document_declares_flags_rollout_and_immutable_rollback(self):
        document = (ROOT / "docs/release-skill-runtime-v2.md").read_text(encoding="utf-8")
        for marker in (
            "skill_runtime_v2",
            "technical_gate_v2",
            "5%",
            "25%",
            "100%",
            "上一个不可变版本",
            "python3 scripts/verify_release_matrix.py --profile full",
            "python3 scripts/verify_release_matrix.py --profile real-model",
        ):
            self.assertIn(marker, document)


if __name__ == "__main__":
    unittest.main()
