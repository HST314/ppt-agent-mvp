import hashlib, json, tempfile, threading, unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ppt_agent.errors import ConflictError, GateError, ValidationError
from ppt_agent.gateways import FakeHtmlBuilder
from ppt_agent.offline import verify_delivery
from ppt_agent.p4 import render
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    def inspect(self, outline, html): return {"passed": True, "issues": [], "model": "fixture"}


class BlockingInspector:
    def inspect(self, outline, html):
        return {"passed":False,"issues":[{"issue_id":"overflow","severity":"blocker","level":"element","code":"overflow","message":"元素溢出","slide_id":"slide-1","element_id":"title","evidence":"超出边界","suggestion":"缩小字号"}],"model":"fixture"}


class TechnicalBlockingBrowserInspector:
    enforce_on_generation=False
    def inspect(self,html,slide_ids):
        return {"available":True,"passed":False,"engine":"chromium","engine_version":"test","viewport":{"width":1280,"height":720},"slides":[],"issues":[{"issue_id":"browser-overflow","severity":"blocker","level":"element","code":"content_out_of_bounds","message":"元素溢出","slide_id":slide_ids[0],"element_id":"title","evidence":"DOM geometry exceeds slide by 20px","suggestion":"收紧内容"}]}


class BlockingBuilder:
    def __init__(self):
        self.started=threading.Event(); self.release=threading.Event()
    def build(self,outline,**context):
        self.started.set()
        if not self.release.wait(5): raise RuntimeError("blocking builder was not released")
        return render(outline,context["slide_ids"],context.get("rules"),context.get("exceptions"),context.get("assets"))


class WarningInspector:
    def inspect(self, outline, html):
        return {"passed":False,"issues":[{"issue_id":"small-text","severity":"warning","level":"element","code":"text_too_small","message":"正文字号偏小","slide_id":"slide-1","element_id":"body","evidence":"computed font-size=12.0px","suggestion":"调大到至少 16px"}],"model":"fixture"}


class DeliveryGateTests(unittest.TestCase):
    """发布门禁：无新鲜报告或阻断问题未清零时禁止首次写包；重放保持幂等。"""

    def _drive_to_deck(self, inspector, browser_inspector=None):
        tmp=tempfile.TemporaryDirectory(); store=WorkspaceStore(tmp.name); svc=TaskService(store,inspector=inspector,browser_inspector=browser_inspector)
        self.addCleanup(tmp.cleanup)
        svc.create("task","manual")
        svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
        svc.generate_narrative("task"); svc.confirm_narrative("task"); svc.generate_outline("task"); svc.confirm_outline("task")
        svc.generate_sample("task"); svc.confirm_sample("task"); svc.generate_deck("task")
        return svc

    def test_publish_blocked_without_inspection_report(self):
        svc=self._drive_to_deck(PassingInspector())
        deck=svc.deck_view("task")["deck"]; svc.finalize_deck("task",deck["hash"],"deck")
        with self.assertRaises(ConflictError): svc.publish_delivery("task")
        self.assertEqual(svc.versions("task","delivery"),[])

    def test_publish_blocked_with_unresolved_blocker_then_allowed_after_disposition(self):
        svc=self._drive_to_deck(PassingInspector(),TechnicalBlockingBrowserInspector())
        svc.run_inspection("task",0)
        with self.assertRaises(ConflictError): svc.assert_delivery_gate("task")
        issue=svc.inspection_view("task")["blocking_issues"][0]
        svc.dispose_issue("task",issue["issue_id"],"waive","演示口径接受该越界",actor="user")
        deck=svc.deck_view("task")["deck"]; svc.finalize_deck("task",deck["hash"],"review")
        result=svc.publish_delivery("task")
        self.assertEqual(result["state"]["status"],"completed")

    def test_warnings_do_not_block_publish(self):
        svc=self._drive_to_deck(WarningInspector())
        svc.run_inspection("task",0)
        self.assertEqual(svc.inspection_view("task")["unresolved"][0]["severity"],"warning")
        deck=svc.deck_view("task")["deck"]; svc.finalize_deck("task",deck["hash"],"review")
        result=svc.publish_delivery("task")
        self.assertEqual(result["state"]["status"],"completed")

    def test_replay_after_gate_pass_stays_idempotent(self):
        svc=self._drive_to_deck(PassingInspector())
        svc.run_inspection("task",0)
        deck=svc.deck_view("task")["deck"]; svc.finalize_deck("task",deck["hash"],"review")
        first=svc.publish_delivery("task"); second=svc.publish_delivery("task")
        self.assertEqual(second["delivery"]["hash"],first["delivery"]["hash"])
        self.assertEqual(len(svc.versions("task","delivery")),1)


class FinalizeGateTests(unittest.TestCase):
    """定稿门禁：阻断问题未清零时禁止默认定稿；带风险定稿必须显式选择、留痕，且不放松发布门禁。"""

    def _drive_to_review_with_blocker(self):
        tmp=tempfile.TemporaryDirectory(); store=WorkspaceStore(tmp.name); svc=TaskService(store,inspector=PassingInspector(),browser_inspector=TechnicalBlockingBrowserInspector())
        self.addCleanup(tmp.cleanup)
        svc.create("task","manual")
        svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
        svc.generate_narrative("task"); svc.confirm_narrative("task"); svc.generate_outline("task"); svc.confirm_outline("task")
        svc.generate_sample("task"); svc.confirm_sample("task"); svc.generate_deck("task"); svc.run_inspection("task",0)
        return svc

    def test_default_finalize_is_blocked_by_unresolved_blockers(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        with self.assertRaisesRegex(ConflictError,"阻断问题"):
            svc.finalize_deck("task",deck["hash"],"review")
        self.assertEqual(svc.get("task")["stage"],"review")
        self.assertIsNone(svc.finalization_view("task")["current"])
        self.assertEqual(svc.versions("task","final-deck"),[])

    def test_finalize_rejects_malformed_risk_parameters(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        with self.assertRaises(ValidationError): svc.finalize_deck("task",deck["hash"],"review",allow_risk="yes")
        with self.assertRaises(ValidationError): svc.finalize_deck("task",deck["hash"],"review",allow_risk=True,risk_rationale=123)

    def test_risk_finalize_requires_a_traceable_rationale(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        for rationale in ("","   "):
            with self.subTest(rationale=rationale), self.assertRaises(ValidationError):
                svc.finalize_deck("task",deck["hash"],"review",allow_risk=True,risk_rationale=rationale)
        self.assertIsNone(svc.finalization_view("task")["current"])

    def test_risk_finalize_records_mode_and_rationale_on_the_fact_and_views(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        result=svc.finalize_deck("task",deck["hash"],"review",allow_risk=True,risk_rationale=" 客户确认接受该越界风险 ")
        finalization=result["finalization"]
        self.assertEqual(result["state"]["stage"],"delivery")
        self.assertEqual(finalization["finalization_mode"],"risk_accepted")
        self.assertEqual(finalization["risk_rationale"],"客户确认接受该越界风险")
        self.assertEqual(finalization["blocking_issue_count"],1)
        self.assertEqual(finalization["inspection_status"],"issues_remaining")
        current=svc.finalization_view("task")["current"]
        self.assertEqual(current["finalization_mode"],"risk_accepted")
        self.assertEqual(current["risk_rationale"],"客户确认接受该越界风险")
        # 交付视图必须携带同等标注，交付页据此展示“带风险终稿”。
        delivery=svc.delivery_view("task")
        self.assertEqual(delivery["finalization"]["finalization_mode"],"risk_accepted")
        self.assertEqual(delivery["finalization"]["risk_rationale"],"客户确认接受该越界风险")

    def test_risk_finalize_freezes_the_candidate_and_replay_is_rejected(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        first=svc.finalize_deck("task",deck["hash"],"review",allow_risk=True,risk_rationale="客户确认接受")["finalization"]
        # 终稿事实冻结、阶段进入交付后：重复定稿（无论是否带风险）都被拒绝且不产生新事实。
        for kwargs in ({"allow_risk":True,"risk_rationale":"客户确认接受"},{}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConflictError):
                svc.finalize_deck("task",deck["hash"],"review",**kwargs)
        self.assertEqual(len(svc.versions("task","final-deck")),1)
        self.assertEqual(svc.finalization_view("task")["current"]["hash"],first["hash"])

    def test_publish_stays_gated_after_risk_finalize(self):
        svc=self._drive_to_review_with_blocker()
        deck=svc.deck_view("task")["deck"]
        svc.finalize_deck("task",deck["hash"],"review",allow_risk=True,risk_rationale="客户确认接受")
        with self.assertRaises(ConflictError): svc.publish_delivery("task")
        self.assertEqual(svc.versions("task","delivery"),[])

    def test_standard_finalize_after_disposition_is_not_labeled_risk(self):
        svc=self._drive_to_review_with_blocker()
        issue=svc.inspection_view("task")["blocking_issues"][0]
        svc.dispose_issue("task",issue["issue_id"],"waive","演示口径接受该越界")
        deck=svc.deck_view("task")["deck"]
        finalization=svc.finalize_deck("task",deck["hash"],"review")["finalization"]
        self.assertEqual(finalization["finalization_mode"],"standard")
        self.assertEqual(finalization["risk_rationale"],"")
        self.assertEqual(finalization["inspection_status"],"issues_disposed")


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

    def test_ac17_delivery_is_immutable_and_new_candidate_requires_fresh_precheck(self):
        deck=self.svc.deck_view("task")["deck"]; delivered=self.svc.confirm_delivery("task",deck["hash"])["delivery"]; root=self.store.delivery_root("task",delivered["delivery_id"]); before=(root/"deck.html").read_bytes()
        candidate=self.svc.derive_from_delivery("task",delivered["hash"],"统一使用蓝色主题")["deck"]
        self.assertNotEqual(candidate["hash"],deck["hash"]); self.assertEqual((root/"deck.html").read_bytes(),before); self.assertEqual(self.svc.get("task")["status"],"ready")
        # 派生候选使既有报告过期：发布门禁必须阻断，而不是沿用旧证据。
        with self.assertRaises(ConflictError): self.svc.confirm_delivery("task",candidate["hash"])
        self.assertEqual(len(self.svc.versions("task","delivery")),1)
        # 逃生通道：返回自检重新预检、重新确定终稿后放行。
        self.svc.reopen_review("task")
        self.svc.run_inspection("task",0)
        refinalized=self.svc.finalize_deck("task",candidate["hash"],"review")["finalization"]
        self.assertEqual(refinalized["inspection_status"],"passed")
        result=self.svc.publish_delivery("task")
        self.assertEqual(result["state"]["status"],"completed")
        self.assertEqual(self.svc.finalization_view("task")["current"]["inspection_status"],"passed")

    def test_finalization_is_distinct_from_offline_publish(self):
        deck=self.svc.deck_view("task")["deck"]
        finalized=self.svc.finalize_deck("task",deck["hash"],"review")
        self.assertEqual(finalized["state"]["stage"],"delivery")
        self.assertNotEqual(finalized["state"]["status"],"completed")
        self.assertFalse(self.svc.versions("task","delivery"))
        delivered=self.svc.publish_delivery("task")
        self.assertEqual(delivered["state"]["status"],"completed")

    def test_finalization_fact_and_stage_advance_are_transactional(self):
        deck=self.svc.deck_view("task")["deck"]
        before=self.svc.get("task")
        armed={"value":True}
        def fault(point):
            if armed["value"] and point=="after_prepare": raise RuntimeError("injected")
        self.store.fault=fault
        with self.assertRaises(RuntimeError):
            self.svc.finalize_deck("task",deck["hash"],"review")
        self.assertEqual(self.svc.get("task"),before)
        self.assertEqual(self.svc.versions("task","final-deck"),[])
        armed["value"]=False
        finalized=self.svc.finalize_deck("task",deck["hash"],"review")
        self.assertEqual(finalized["state"]["stage"],"delivery")
        self.assertEqual(len(self.svc.versions("task","final-deck")),1)

    def test_blocked_builder_cannot_publish_after_concurrent_finalization(self):
        deck=self.svc.deck_view("task")["deck"]
        before_versions=self.svc.versions("task","deck")
        builder=BlockingBuilder(); self.svc.builder=builder
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.svc.modify_deck,"task","统一使用蓝色主题")
            self.assertTrue(builder.started.wait(2))
            peer=TaskService(WorkspaceStore(self.tmp.name),inspector=PassingInspector())
            finalized=peer.finalize_deck("task",deck["hash"],"review")["finalization"]
            builder.release.set()
            with self.assertRaises(ConflictError): pending.result(timeout=3)
        self.assertEqual(self.svc.get("task")["stage"],"delivery")
        self.assertEqual(self.svc.versions("task","deck"),before_versions)
        self.assertEqual(self.svc.deck_view("task")["deck"]["hash"],deck["hash"])
        self.assertEqual(self.svc.finalization_view("task")["current"]["hash"],finalized["hash"])

    def test_candidate_cas_rejects_a_stale_parent_while_stage_stays_mutable(self):
        parent=self.svc.deck_view("task")["deck"]
        before=len(self.svc.versions("task","deck"))
        builder=BlockingBuilder(); self.svc.builder=builder
        with ThreadPoolExecutor(max_workers=1) as pool:
            stale=pool.submit(self.svc.modify_deck,"task","旧请求：改为蓝色")
            self.assertTrue(builder.started.wait(2))
            self.svc.builder=FakeHtmlBuilder()
            winner=self.svc.modify_deck("task","新请求：改为红色")["deck"]
            builder.release.set()
            with self.assertRaises(ConflictError): stale.result(timeout=3)
        self.assertEqual(len(self.svc.versions("task","deck")),before+1)
        self.assertEqual(winner["metadata"]["parent"],parent["hash"])
        self.assertEqual(self.svc.deck_view("task")["deck"]["hash"],winner["hash"])

    def test_delivery_stage_rejects_every_candidate_write_and_preserves_finalization(self):
        original=self.svc.deck_view("task")["deck"]
        candidate=self.svc.modify_deck("task","统一使用蓝色主题")["deck"]
        finalized=self.svc.finalize_deck("task",candidate["hash"],"review")["finalization"]
        before_versions=self.svc.versions("task","deck")

        for action in (
            lambda:self.svc.rollback_deck("task",original["hash"]),
            lambda:self.svc.modify_deck("task","改为红色主题"),
            lambda:self.svc.run_inspection("task",0),
            lambda:self.svc.switch_inspection_mode("task","auto"),
        ):
            with self.subTest(action=action), self.assertRaises(ConflictError): action()

        self.assertEqual(self.svc.versions("task","deck"),before_versions)
        self.assertEqual(self.svc.deck_view("task")["deck"]["hash"],candidate["hash"])
        self.assertEqual(self.svc.finalization_view("task")["current"]["hash"],finalized["hash"])

    def test_disposed_report_is_not_recorded_as_having_remaining_issues(self):
        self.svc.inspector=BlockingInspector()
        self.svc.run_inspection("task",0)
        disposed=self.svc.dispose_issue("task","overflow","waive","用户接受该版式风险")
        self.assertEqual(disposed["unresolved"],[])
        finalization=self.svc.finalize_deck("task",disposed["deck"]["hash"],"review")["finalization"]
        self.assertEqual(finalization["inspection_status"],"issues_disposed")
        self.assertEqual(finalization["unresolved_issue_count"],0)
        self.assertEqual(finalization["blocking_issue_count"],0)

    def test_publish_replay_returns_the_same_domain_fact(self):
        deck=self.svc.deck_view("task")["deck"]
        self.svc.finalize_deck("task",deck["hash"],"review")
        first=self.svc.publish_delivery("task")
        second=self.svc.publish_delivery("task")
        for field in ("hash","delivery_id","deck_hash","confirmed_at","file_hashes"):
            self.assertEqual(second["delivery"][field],first["delivery"][field])
        self.assertEqual(second["state"],first["state"])
        self.assertEqual(len(self.svc.versions("task","delivery")),1)

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

    def test_staging_verification_failure_publishes_no_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkspaceStore(tmp); store.create("task",{"task_id":"task","stage":"created","status":"ready","mode":"manual","sample_confirmed":False,"blockers_resolved":False,"delivery_confirmed":False,"revision":0,"waiting_reason":None,"required_action":None})
            html=b'<html><script src="https://cdn.example/app.js"></script></html>'
            manifest=json.dumps({"files":{"deck.html":hashlib.sha256(html).hexdigest()}},sort_keys=True,separators=(",",":")).encode()
            with self.assertRaisesRegex(ValueError,"external URLs"):
                store.publish_delivery("task","delivery-1",{"deck.html":html,"manifest.json":manifest},verifier=verify_delivery)
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

    def test_retry_reuses_published_remote_bytes_without_fetching_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            armed={"value":True}
            def fault(point):
                if armed["value"] and point=="after_delivery_publish": raise RuntimeError("injected")
            store=WorkspaceStore(tmp,fault=fault); svc=TaskService(store,inspector=PassingInspector()); svc.create("task","manual")
            svc.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":2})
            svc.generate_narrative("task"); svc.confirm_narrative("task"); svc.generate_outline("task"); svc.confirm_outline("task")
            svc.generate_sample("task"); svc.confirm_sample("task"); svc.generate_deck("task"); svc.run_inspection("task",0)
            deck_hash=svc.deck_view("task")["deck"]["hash"]
            localized_calls=[]
            def localized(html,_manifest,_root):
                localized_calls.append(True)
                if len(localized_calls)>1: raise AssertionError("published retry must not fetch again")
                return html,{"resources/remote-fixed.png":b"first-download"},[{"source":"remote","path":"resources/remote-fixed.png"}]
            with patch("ppt_agent.service.localize_delivery_html",side_effect=localized):
                with self.assertRaises(RuntimeError): svc.confirm_delivery("task",deck_hash)
                armed["value"]=False
                recovered=svc.confirm_delivery("task",deck_hash)
            self.assertEqual(recovered["state"]["status"],"completed")
            self.assertEqual(len(localized_calls),1)


if __name__=="__main__": unittest.main()
