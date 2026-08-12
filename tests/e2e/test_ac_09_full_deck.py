"""AC-09: confirmed samples expand into a complete, independently playable deck."""

from unittest.mock import patch

from .support import SampleJourney


class AC09FullDeckE2E(SampleJourney):
    def test_full_deck_matches_outline_and_preserves_confirmed_sample_pages(self):
        sample=self.ok("/v1/tasks/journey/samples/generate",{})
        self.ok("/v1/tasks/journey/samples/confirm",{})
        deck=self.ok("/v1/tasks/journey/deck/generate",{})
        self.assertEqual(deck["state"]["stage"],"review")
        self.assertEqual(list(deck["deck"]["metadata"]["page_hashes"]),["slide-1","slide-2","slide-3"])
        self.assertEqual(deck["deck"]["html"].count('data-slide-id="'),3)
        self.assertTrue(all(deck["deck"]["metadata"]["sample_pages_preserved"].values()))
        self.assertEqual(deck["deck"]["metadata"]["sample_hash"],sample["sample"]["hash"])
        self.assertIn("<!doctype html>",deck["deck"]["html"])
        status,page=self.call("GET","/tasks/journey/deck")
        rendered=page.decode(); self.assertTrue(status.startswith("200"))
        self.assertIn('<iframe id="previewFrame" sandbox=""',rendered)
        self.assertIn("修改类型",rendered); self.assertIn("版本时间线",rendered); self.assertIn("非破坏回退",rendered)

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
