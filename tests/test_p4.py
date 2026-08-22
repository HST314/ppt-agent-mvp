import base64, io, tempfile, unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.p4 import recommend, required_sample_targets, validate_html

PNG=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"

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
    def test_dedicated_period_and_budget_pages_are_required_sample_targets(self):
        markdown="""# 大纲
## [slide-1] 决策封面
- 总周期 12 周，总预算 80 万元
## [slide-2] 背景
- 现状
## [slide-3] 实施周期与阶段划分
- 专门说明 12 周实施节奏
## [slide-4] 投资预算
- 专门说明 80 万元预算
"""
        targets=required_sample_targets(markdown,2)
        selected,_=recommend(markdown,2,required_targets=targets)
        self.assertEqual(targets,[
            {"slide_id":"slide-3","role":"period","basis":"dedicated_outline_title:实施周期与阶段划分"},
            {"slide_id":"slide-4","role":"budget","basis":"dedicated_outline_title:投资预算"},
        ])
        self.assertEqual(selected,["slide-3","slide-4"])
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
    def test_prompt_scope_inference_and_ambiguity(self):
        self.s.generate_sample("p4"); sid=self.s.sample_view("p4")["selection"]["slide_ids"][0]
        page=self.s.modify_sample("p4","当前页增加留白",slide_id=sid)["sample"]
        self.assertEqual(page["metadata"]["scope"],"page"); self.assertEqual(page["metadata"]["scope_understanding"]["basis"],"prompt_semantics")
        element=self.s.modify_sample("p4","标题改成蓝色",slide_id=sid,element_id="title")["sample"]
        self.assertEqual(element["metadata"]["scope"],"element")
        with self.assertRaisesRegex(ValidationError,"歧义"):
            self.s.modify_sample("p4","统一所有页，但只改当前页",slide_id=sid)
        with self.assertRaisesRegex(ValidationError,"冲突"):
            self.s.modify_sample("p4","统一所有页背景","page",sid)
    def test_frozen_resource_is_embedded_and_tamper_is_rejected(self):
        self.s.create("asset"); self.s.store.put_resource("asset","hero.png",PNG)
        self.s.import_input("asset",{"goal":"发布","audience":"客户","topic":"方案","页数":1})
        self.s.generate_narrative("asset"); self.s.confirm_narrative("asset"); self.s.generate_outline("asset"); self.s.confirm_outline("asset")
        sample=self.s.generate_sample("asset")["sample"]
        self.assertIn('<img data-element-id="resource" src="data:image/png;base64,',sample["html"])
        (self.s.store.resource_root("asset")/"hero.png").write_bytes(PNG+b"tampered")
        with self.assertRaisesRegex(ValidationError,"内容已变化"):
            self.s.generate_sample("asset")
    def test_confirmation_binds_exact_versions_and_enters_deck_atomically(self):
        self.s.generate_sample("p4")
        view=self.s.confirm_sample("p4"); fact=view["confirmation"]
        self.assertEqual(fact["confirmed_outline_hash"],view["outline_hash"]); self.assertEqual(fact["confirmed_sample_hash"],view["sample"]["hash"])
        self.assertTrue(view["state"]["sample_confirmed"]); self.assertEqual(view["state"]["stage"],"deck")
        revision=view["state"]["revision"]
        replay=self.s.confirm_sample("p4")
        self.assertEqual(replay["state"]["revision"],revision)
        self.assertEqual(replay["confirmation"],fact)
        with self.assertRaisesRegex(ConflictError,"历史样品只读"):
            self.s.modify_sample("p4","统一加深背景","global")
        self.s.create("auto",mode="auto"); self.s.import_input("auto",{"goal":"发布","audience":"客户","topic":"方案","页数":2})
        with self.assertRaises(Exception): self.s.command("auto","skip-sample","advance")
    def test_last_success_survives_invalid_modification_and_ui_is_sandboxed(self):
        good=self.s.generate_sample("p4")["sample"]["hash"]
        with self.assertRaises(ValidationError): self.s.modify_sample("p4","x","page","foreign")
        self.assertEqual(self.s.sample_view("p4")["sample"]["hash"],good)
        status=[]; body=b"".join(App(self.s)({"REQUEST_METHOD":"GET","PATH_INFO":"/tasks/p4/samples","CONTENT_LENGTH":"0","wsgi.input":io.BytesIO()},lambda s,h:status.append((s,h)))).decode()
        self.assertIn('type="module"',body); self.assertTrue(status[0][0].startswith("200"))
        module=Path("frontend/static/js/stages/sample.js").read_text(); components=Path("frontend/static/js/components/index.js").read_text()
        self.assertIn("确认当前样品并进入全稿",module); self.assertIn('sandbox: allowInspection ? "allow-same-origin" : ""',components)
    def test_selection_and_outline_changes_invalidate_confirmation_and_block_advance(self):
        self.s.generate_sample("p4"); self.s.confirm_sample("p4")
        outline=self.s.planning_view("p4")["outline"]["markdown"]
        changed=self.s.edit_outline("p4",outline+"\n<!-- changed -->\n")
        self.assertFalse(changed["state"]["sample_confirmed"])
        self.assertEqual(changed["state"]["stage"],"outline")
        with self.assertRaises(ConflictError): self.s.command("p4","advance-after-outline","advance")
    def test_html_security_negative_matrix(self):
        base='<!doctype html><html><body><section data-slide-id="slide-1">x</section></body></html>'
        self.assertEqual(validate_html(base,["slide-1"]),base)
        encoded=base64.b64encode(PNG).decode()
        allowed=[
            '<img src="https://images.example/hero.png">',
            '<img src="http://images.example/hero.png">',
            f'<img src="data:image/png;base64,{encoded}">',
            '<img src="resources/hero.png">',
            '<style>body{background-image:url("https://images.example/hero.png")}</style>',
            '<div style="background:url(resources/hero.png)">placeholder / 占位 / lorem ipsum</div>',
        ]
        for fragment in allowed:
            with self.subTest(allowed=fragment):
                value=base.replace('</body>',fragment+'</body>')
                assets={"resources://hero.png":f"data:image/png;base64,{encoded}"} if "resources/hero.png" in fragment else {}
                self.assertEqual(validate_html(value,["slide-1"],assets),value)
        with self.assertRaisesRegex(ValidationError,"冻结资源清单"):
            validate_html(base.replace('</body>','<img src="resources/missing.png"></body>'),["slide-1"],{"resources://hero.png":f"data:image/png;base64,{encoded}"})
        attacks=[
            '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
            '<style>@import "https://evil.test/x.css"</style>', '<style>body{background:url(//evil.test/x)}</style>',
            '<img srcset="https://evil.test/a 1x">', '<meta http-equiv="refresh" content="0;url=https://evil.test">',
            '<iframe src="https://evil.test"></iframe>', '<a href="../../secret">x</a>',
            '<img src="file:///etc/passwd">', '<img src="resources://other-task/secret">',
            '<img src="data:image/png;base64,AAAA">',
            '<table background="&#104;&#116;&#116;&#112;&#115;&#58;&#47;&#47;evil.test/a"><tr><td>x</td></tr></table>',
            '<style>body{background-image:\\75rl(\\68ttps\\3a\\2f\\2fevil.test\\2fa)}</style>',
            '<style>body{background-image:image-set("&#104;&#116;&#116;&#112;&#115;&#58;&#47;&#47;evil.test/a" 1x)}</style>',
            '<div style="background-image:cross-fade(red, blue)">x</div>',
            '<style>body{color:\\FFFFFF}</style>',
            '<style>body{color:\\110000}</style>',
            '<style>body{color:\\0}</style>',
            '<style>body{color:\\D800}</style>',
            '<style>body{color:red\\</style>',
        ]
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValidationError):
                validate_html(base.replace('</body>',attack+'</body>'),["slide-1"])

if __name__ == "__main__": unittest.main()
