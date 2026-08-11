import io, tempfile, unittest

from ppt_agent.api import App
from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.p4 import validate_html

class P4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=TaskService(WorkspaceStore(self.tmp.name)); self.s.create("p4")
        self.s.import_input("p4",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
        self.s.generate_narrative("p4"); self.s.confirm_narrative("p4"); self.s.generate_outline("p4"); self.s.confirm_outline("p4")
    def tearDown(self): self.tmp.cleanup()
    def test_default_recommendation_and_selection_validation(self):
        view=self.s.select_samples("p4")
        self.assertEqual(len(view["selection"]["slide_ids"]),2); self.assertEqual(view["selection"]["outline_hash"],view["outline_hash"])
        with self.assertRaises(ValidationError): self.s.select_samples("p4",["slide-1","slide-1"])
        with self.assertRaises(ValidationError): self.s.select_samples("p4",["foreign"])
    def test_real_html_versions_and_scopes(self):
        first=self.s.generate_sample("p4")["sample"]
        self.assertTrue(first["html"].startswith("<!doctype html>")); self.assertNotIn("<script",first["html"])
        sid=self.s.sample_view("p4")["selection"]["slide_ids"][0]
        page=self.s.modify_sample("p4","标题更醒目","page",sid)["sample"]
        self.assertEqual(page["metadata"]["scope"],"page"); self.assertIn(sid,page["metadata"]["local_exceptions"])
        element=self.s.modify_sample("p4","改为蓝色","element",sid,"title")["sample"]
        self.assertEqual(element["metadata"]["element_id"],"title")
        global_=self.s.modify_sample("p4","统一高对比度","global")["sample"]
        self.assertIn("统一高对比度",global_["metadata"]["global_rules"]); self.assertEqual(global_["version"],4)
    def test_confirmation_binds_exact_versions_and_never_auto_skips(self):
        self.s.generate_sample("p4")
        view=self.s.confirm_sample("p4"); fact=view["confirmation"]
        self.assertEqual(fact["confirmed_outline_hash"],view["outline_hash"]); self.assertEqual(fact["confirmed_sample_hash"],view["sample"]["hash"])
        self.assertTrue(view["state"]["sample_confirmed"]); self.assertEqual(view["state"]["stage"],"sample")
        changed=self.s.modify_sample("p4","统一加深背景","global")
        self.assertFalse(changed["state"]["sample_confirmed"]); self.assertIsNone(changed["confirmation"])
        self.s.create("auto",mode="auto"); self.s.import_input("auto",{"goal":"发布","audience":"客户","topic":"方案","页数":2}); self.s.generate_narrative("auto"); self.s.generate_outline("auto"); self.s.confirm_outline("auto"); self.s.generate_sample("auto")
        with self.assertRaises(Exception): self.s.command("auto","skip-sample","advance")
    def test_last_success_survives_invalid_modification_and_ui_is_sandboxed(self):
        good=self.s.generate_sample("p4")["sample"]["hash"]
        with self.assertRaises(ValidationError): self.s.modify_sample("p4","x","page","foreign")
        self.assertEqual(self.s.sample_view("p4")["sample"]["hash"],good)
        status=[]; body=b"".join(App(self.s)({"REQUEST_METHOD":"GET","PATH_INFO":"/tasks/p4/samples","CONTENT_LENGTH":"0","wsgi.input":io.BytesIO()},lambda s,h:status.append((s,h)))).decode()
        self.assertIn('<iframe sandbox=""',body); self.assertIn("确认样品并生成全稿",body); self.assertTrue(status[0][0].startswith("200"))
    def test_selection_and_outline_changes_invalidate_confirmation_and_block_advance(self):
        first=self.s.generate_sample("p4"); self.s.confirm_sample("p4")
        ids=list(first["selection"]["slide_ids"]); ids.reverse()
        changed=self.s.select_samples("p4",ids)
        self.assertFalse(changed["state"]["sample_confirmed"]); self.assertIsNone(changed["confirmation"])
        with self.assertRaises(ConflictError): self.s.command("p4","advance-after-selection","advance")
        self.s.generate_sample("p4"); self.s.confirm_sample("p4")
        outline=self.s.planning_view("p4")["outline"]["markdown"]
        changed=self.s.edit_outline("p4",outline+"\n<!-- changed -->\n")
        self.assertFalse(changed["state"]["sample_confirmed"])
        with self.assertRaises(ConflictError): self.s.command("p4","advance-after-outline","advance")
    def test_html_security_negative_matrix(self):
        base='<!doctype html><html><body><section data-slide-id="slide-1">x</section></body></html>'
        self.assertEqual(validate_html(base,["slide-1"]),base)
        attacks=[
            '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
            '<style>@import "https://evil.test/x.css"</style>', '<style>body{background:url(//evil.test/x)}</style>',
            '<div style="background:url(https://evil.test/x)">x</div>',
            '<img srcset="https://evil.test/a 1x">', '<meta http-equiv="refresh" content="0;url=https://evil.test">',
            '<iframe src="https://evil.test"></iframe>', '<a href="../../secret">x</a>',
            '<img src="file:///etc/passwd">', '<img src="resources://other-task/secret">',
            '<table background="&#104;&#116;&#116;&#112;&#115;&#58;&#47;&#47;evil.test/a"><tr><td>x</td></tr></table>',
            '<style>body{background-image:\\75rl(\\68ttps\\3a\\2f\\2fevil.test\\2fa)}</style>',
            '<style>body{background-image:image-set("&#104;&#116;&#116;&#112;&#115;&#58;&#47;&#47;evil.test/a" 1x)}</style>',
            '<div style="background-image:cross-fade(red, blue)">x</div>',
        ]
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValidationError):
                validate_html(base.replace('</body>',attack+'</body>'),["slide-1"])

if __name__ == "__main__": unittest.main()
