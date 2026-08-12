import hashlib, json, tempfile, unittest

from ppt_agent.errors import ConflictError, GateError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    def inspect(self, outline, html): return {"passed": True, "issues": [], "model": "fixture"}


class DeliveryJourney(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=WorkspaceStore(self.tmp.name); self.svc=TaskService(self.store,inspector=PassingInspector()); self.svc.create("task","manual")
        self.svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
        self.svc.generate_narrative("task"); self.svc.confirm_narrative("task"); self.svc.generate_outline("task"); self.svc.confirm_outline("task")
        self.svc.generate_sample("task"); self.svc.confirm_sample("task"); self.svc.generate_deck("task"); self.svc.run_inspection("task",0)
    def tearDown(self): self.tmp.cleanup()

    def test_ac15_explicit_confirmation_is_only_completion_path(self):
        deck=self.svc.deck_view("task")["deck"]
        self.assertNotEqual(self.svc.get("task")["status"],"completed")
        with self.assertRaises(ConflictError): self.svc.confirm_delivery("task","0"*64)
        result=self.svc.confirm_delivery("task",deck["hash"])
        self.assertEqual(result["state"]["status"],"completed"); self.assertEqual(result["delivery"]["confirmed_by"],"user")

    def test_ac16_bundle_is_complete_runnable_and_hash_verified(self):
        deck=self.svc.deck_view("task")["deck"]; delivery=self.svc.confirm_delivery("task",deck["hash"])["delivery"]
        root=self.store.delivery_root("task",delivery["delivery_id"]); manifest=json.loads((root/"manifest.json").read_text())
        expected={"deck.html","index.html","assets/offline-player.js","assets/motion.min.js","assets/THIRD_PARTY_NOTICES.txt","narrative.md","outline.md","resource-manifest.json","result.json"}
        self.assertTrue(expected.issubset(manifest["files"]))
        for name,want in manifest["files"].items(): self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),want)
        self.assertIn("<html",(root/"deck.html").read_text().lower())
        player=(root/"index.html").read_text()
        self.assertIn('src="assets/offline-player.js"',player)
        self.assertIn('src="assets/motion.min.js"',player)
        result=json.loads((root/"result.json").read_text())
        self.assertEqual(result["version"],delivery["delivery_id"])
        self.assertEqual(result["status"],{"stage":"delivery","status":"completed"})
        self.assertTrue(result["description"])

    def test_ac17_delivery_is_immutable_and_new_candidate_requires_reinspection(self):
        deck=self.svc.deck_view("task")["deck"]; delivered=self.svc.confirm_delivery("task",deck["hash"])["delivery"]; root=self.store.delivery_root("task",delivered["delivery_id"]); before=(root/"deck.html").read_bytes()
        candidate=self.svc.derive_from_delivery("task",delivered["hash"],"统一使用蓝色主题")["deck"]
        self.assertNotEqual(candidate["hash"],deck["hash"]); self.assertEqual((root/"deck.html").read_bytes(),before); self.assertEqual(self.svc.get("task")["status"],"ready")
        with self.assertRaises(ConflictError): self.svc.confirm_delivery("task",candidate["hash"])

    def test_pause_stops_new_work_and_resume_preserves_last_version(self):
        before=self.svc.deck_view("task")["deck"]["hash"]
        self.svc.command("task","pause-1","pause","user")
        with self.assertRaises(ConflictError): self.svc.run_inspection("task",0)
        self.assertEqual(self.svc.deck_view("task")["deck"]["hash"],before)
        self.svc.command("task","resume-1","resume","user")
        self.assertEqual(self.svc.run_inspection("task",0)["deck"]["hash"],before)


class DeliveryFaultTests(unittest.TestCase):
    def test_package_fault_publishes_no_partial_directory(self):
        def fault(point):
            if point=="before_delivery_publish": raise RuntimeError("injected")
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkspaceStore(tmp,fault=fault); store.create("task",{"task_id":"task","stage":"created","status":"ready","mode":"manual","sample_confirmed":False,"blockers_resolved":False,"delivery_confirmed":False,"revision":0,"waiting_reason":None,"required_action":None})
            with self.assertRaises(RuntimeError): store.publish_delivery("task","delivery-1",{"deck.html":b"ok"})
            self.assertEqual(list((store._task("task")/"deliveries").iterdir()),[])

    def test_post_publish_breakpoints_are_idempotently_recoverable(self):
        for breakpoint in ("after_delivery_publish","after_delivery_fact","after_delivery_completed"):
            with self.subTest(breakpoint=breakpoint), tempfile.TemporaryDirectory() as tmp:
                armed={"value":True}
                def fault(point):
                    if armed["value"] and point==breakpoint: raise RuntimeError("injected")
                store=WorkspaceStore(tmp,fault=fault); svc=TaskService(store,inspector=PassingInspector()); svc.create("task","manual")
                svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
                svc.generate_narrative("task"); svc.confirm_narrative("task"); svc.generate_outline("task"); svc.confirm_outline("task")
                svc.generate_sample("task"); svc.confirm_sample("task"); svc.generate_deck("task"); svc.run_inspection("task",0)
                deck_hash=svc.deck_view("task")["deck"]["hash"]
                with self.assertRaises(RuntimeError): svc.confirm_delivery("task",deck_hash)
                armed["value"]=False
                recovered=svc.confirm_delivery("task",deck_hash)
                self.assertEqual(recovered["state"]["status"],"completed")
                self.assertEqual(len(svc.versions("task","delivery")),1)
                self.assertEqual(len(list((store._task("task")/"deliveries").iterdir())),1)


if __name__=="__main__": unittest.main()
