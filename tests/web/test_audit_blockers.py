"""Source and adapter gates for the three step-two audit blockers."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


ROOT = Path(__file__).resolve().parents[2]


class AuditBlockerTests(unittest.TestCase):
    def test_retired_adapter_contains_no_page_implementation(self):
        source = (ROOT / "ppt_agent/api.py").read_text()
        for token in ("<html", "<style", "<script", "def home_page", "def outline_page", "def sample_page"):
            self.assertNotIn(token, source)
        self.assertIn("create_app", source)

    def test_every_historical_deep_link_serves_the_same_external_module_shell(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("deep-link")
            with TestClient(create_app(service)) as client:
                expected = client.get("/tasks/deep-link").content
                for suffix in ("outline", "samples", "deck", "inspection", "delivery"):
                    response = client.get(f"/tasks/deep-link/{suffix}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, expected)
                    self.assertIn(b'type="module"', response.content)

    def test_sse_recovery_is_bounded_and_resumes_from_cursor(self):
        source = (ROOT / "frontend/static/js/job-tracker.js").read_text()
        for token in ("maxStreamFailures", "maxRecoveryAttempts", "scheduleRecovery", "track.recoveryAttempts >= this.maxRecoveryAttempts"):
            self.assertIn(token, source)
        self.assertIn("events?after=${track.seq}", source)
        self.assertIn('onTransport?.("polling"', source)
        self.assertIn('onTransport?.("sse"', source)

    def test_refresh_recovery_persists_and_clears_job_intent_mapping(self):
        store = (ROOT / "frontend/static/js/store.js").read_text()
        app = (ROOT / "frontend/static/js/app.js").read_text()
        for token in ("bindJobIntent", "storageKeyForJob", "storedJobIntents", "JOB_INTENT_PREFIX"):
            self.assertIn(token, store)
        self.assertIn("bindJobIntent(job, intent.storageKey)", app)
        self.assertIn("reconcileStoredIntents", app)
        self.assertIn("clearIdempotencyKey(recoveredStorageKey, finished.job_id)", app)


if __name__ == "__main__":
    unittest.main()
