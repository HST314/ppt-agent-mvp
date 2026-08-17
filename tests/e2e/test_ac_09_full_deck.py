"""AC-09: confirmed samples expand into a complete, independently playable deck."""

from pathlib import Path

from unittest.mock import patch

from .support import SampleJourney
from ppt_agent.errors import GatewayUnknownResult


class UnknownInspector:
    def inspect(self, outline, html):
        raise GatewayUnknownResult("inspection result unknown")


class AC09FullDeckE2E(SampleJourney):
    def test_full_deck_matches_outline_and_preserves_confirmed_sample_pages(self):
        sample=self.ok("/v1/tasks/journey/samples/generate",{})
        self.ok("/v1/tasks/journey/samples/confirm",{})
        deck=self.ok("/v1/tasks/journey/deck/generate",{})
        self.assertEqual(deck["state"]["stage"],"deck")
        self.assertEqual(deck["deck"]["metadata"]["inspection_status"],"pending")
        self.assertEqual(list(deck["deck"]["metadata"]["page_hashes"]),["slide-1","slide-2","slide-3"])
        self.assertEqual(deck["deck"]["html"].count('data-slide-id="'),3)
        self.assertTrue(all(deck["deck"]["metadata"]["sample_pages_preserved"].values()))
        self.assertEqual(deck["deck"]["metadata"]["sample_hash"],sample["sample"]["hash"])
        self.assertIn("<!doctype html>",deck["deck"]["html"])
        status,page=self.call("GET","/tasks/journey/deck")
        self.assertTrue(status.startswith("200")); self.assertIn('type="module"',page.decode())
        module=Path("frontend/static/js/stages/deck.js").read_text()
        self.assertIn("previewFrame",module); self.assertIn("修改类型",module)
        self.assertIn("版本时间线",module); self.assertIn("rollbackDeck",module)

    def test_render_failure_is_atomic_before_deck_stage_transition(self):
        self.ok("/v1/tasks/journey/samples/generate",{})
        self.ok("/v1/tasks/journey/samples/confirm",{})
        before_state=self.get_json("/v1/tasks/journey")
        before_versions=self.get_json("/v1/tasks/journey/deck")["versions"]
        before_events=self.get_json("/v1/tasks/journey/events")["events"]
        with patch("ppt_agent.service.render",side_effect=RuntimeError("fault injection")):
            status,_=self.call("POST","/v1/tasks/journey/deck/generate",{})
        self.assertTrue(status.startswith("500"))
        self.assertEqual(self.get_json("/v1/tasks/journey"),before_state)
        self.assertEqual(self.get_json("/v1/tasks/journey/deck")["versions"],before_versions)
        self.assertEqual(self.get_json("/v1/tasks/journey/events")["events"],before_events)

    def test_inspection_unknown_keeps_published_candidate_available(self):
        self.ok("/v1/tasks/journey/samples/generate",{})
        self.ok("/v1/tasks/journey/samples/confirm",{})
        self.app.service.inspector=UnknownInspector()
        candidate=self.ok("/v1/tasks/journey/deck/generate",{})
        status,_=self.call("POST","/v1/tasks/journey/inspection/run",{})
        self.assertTrue(status.startswith("503"))
        self.assertEqual(self.get_json("/v1/tasks/journey/deck")["deck"]["hash"],candidate["deck"]["hash"])
        self.assertFalse(self.app.service.versions("journey","inspection"))
