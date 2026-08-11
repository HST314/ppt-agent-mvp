"""AC-09: confirmed samples expand into a complete, independently playable deck."""

from .support import SampleJourney


class AC09FullDeckE2E(SampleJourney):
    def test_full_deck_matches_outline_and_preserves_confirmed_sample_pages(self):
        sample=self.ok("/v1/tasks/journey/samples/generate",{})
        self.ok("/v1/tasks/journey/samples/confirm",{})
        deck=self.ok("/v1/tasks/journey/deck/generate",{})
        self.assertEqual(deck["state"]["stage"],"deck")
        self.assertEqual(list(deck["deck"]["metadata"]["page_hashes"]),["slide-1","slide-2","slide-3"])
        self.assertEqual(deck["deck"]["html"].count('data-slide-id="'),3)
        self.assertTrue(all(deck["deck"]["metadata"]["sample_pages_preserved"].values()))
        self.assertEqual(deck["deck"]["metadata"]["sample_hash"],sample["sample"]["hash"])
        self.assertIn("<!doctype html>",deck["deck"]["html"])
        status,page=self.call("GET","/tasks/journey/deck")
        rendered=page.decode(); self.assertTrue(status.startswith("200"))
        self.assertIn('<iframe id="previewFrame" sandbox=""',rendered)
        self.assertIn("修改类型",rendered); self.assertIn("版本时间线",rendered); self.assertIn("非破坏回退",rendered)
