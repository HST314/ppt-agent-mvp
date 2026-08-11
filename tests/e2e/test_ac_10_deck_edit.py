"""AC-10: deck edits expose scope, relationships, versions, comparison data and rollback."""

from .support import SampleJourney


class AC10DeckEditE2E(SampleJourney):
    def setUp(self):
        super().setUp(); self.ok("/v1/tasks/journey/samples/generate",{}); self.ok("/v1/tasks/journey/samples/confirm",{})
        self.initial=self.ok("/v1/tasks/journey/deck/generate",{})

    def test_visual_content_scope_isolation_and_non_destructive_rollback(self):
        base=self.initial["deck"]; hashes=base["metadata"]["page_hashes"]
        visual=self.ok("/v1/tasks/journey/deck/modify",{"prompt":"本页使用更紧凑布局","change_type":"visual","scope":"page","slide_ids":["slide-2"]})["deck"]
        self.assertEqual(visual["metadata"]["affected"],["slide-2"])
        self.assertEqual(visual["metadata"]["unchanged"],["slide-1","slide-3"])
        self.assertEqual(visual["metadata"]["page_hashes"]["slide-1"],hashes["slide-1"])
        before_failure=len(self.get_json("/v1/tasks/journey/deck")["versions"])
        status,_=self.call("POST","/v1/tasks/journey/deck/modify",{"prompt":"本页调整","change_type":"visual","scope":"page","slide_ids":["missing"]})
        self.assertTrue(status.startswith("400")); self.assertEqual(len(self.get_json("/v1/tasks/journey/deck")["versions"]),before_failure)
        content=self.ok("/v1/tasks/journey/deck/modify",{"prompt":"补充客户收益","change_type":"content","scope":"page","slide_ids":["slide-3"]})["deck"]
        self.assertNotEqual(content["outline_hash"],visual["outline_hash"])
        self.assertTrue(content["metadata"]["outline_consistent"])
        before=len(self.get_json("/v1/tasks/journey/deck")["versions"])
        rolled=self.ok("/v1/tasks/journey/deck/rollback",{"hash":base["hash"]})["deck"]
        self.assertEqual(len(self.get_json("/v1/tasks/journey/deck")["versions"]),before+1)
        self.assertEqual(rolled["metadata"]["rollback_from"],base["hash"])
        self.assertFalse(rolled["metadata"]["outline_consistent"])
        self.assertEqual(rolled["metadata"]["regenerate_required"],["slide-1","slide-2","slide-3"])
