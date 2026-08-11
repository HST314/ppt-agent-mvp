"""AC-08: repeated edits retain versions and exact confirmation gates."""

from .support import SampleJourney


class AC08SampleGateE2E(SampleJourney):
    def test_repeated_adjustment_and_confirmation_invalidation(self):
        first = self.ok("/v1/tasks/journey/samples/generate", {})
        slide_id = first["selection"]["slide_ids"][0]
        second = self.ok("/v1/tasks/journey/samples/modify", {"prompt": "当前页增加留白", "slide_id": slide_id})
        self.assertEqual(second["sample"]["metadata"]["scope"], "page")
        self.assertEqual(second["sample"]["metadata"]["scope_understanding"]["basis"], "prompt_semantics")
        third = self.ok("/v1/tasks/journey/samples/modify", {"prompt": "标题改成蓝色", "slide_id": slide_id, "element_id": "title"})
        self.assertEqual(third["sample"]["version"], 3)
        self.assertEqual(len(third["versions"]), 3)

        confirmed = self.ok("/v1/tasks/journey/samples/confirm", {})
        self.assertTrue(confirmed["state"]["sample_confirmed"])
        self.assertEqual(confirmed["confirmation"]["confirmed_sample_hash"], confirmed["sample"]["hash"])

        changed = self.ok("/v1/tasks/journey/samples/modify", {"prompt": "统一增加对比度"})
        self.assertFalse(changed["state"]["sample_confirmed"])
        self.assertIsNone(changed["confirmation"])
        status, raw = self.call("POST", "/v1/tasks/journey/actions", {"command_id": "premature-deck", "action": "advance"})
        self.assertTrue(status.startswith("409"), raw.decode())


if __name__ == "__main__":
    import unittest
    unittest.main()
