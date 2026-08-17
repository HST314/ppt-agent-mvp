import io, json, tempfile, unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.gateways import FakeSkillLoader
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class P3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.service=TaskService(WorkspaceStore(self.tmp.name),skills=FakeSkillLoader("skill-v3")); self.service.create("task-1")
        self.service.import_input("task-1",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
    def tearDown(self): self.tmp.cleanup()

    def test_skill_slices_and_manual_gate(self):
        view=self.service.generate_narrative("task-1")
        self.assertEqual(view["state"]["stage"],"narrative"); self.assertEqual(view["state"]["status"],"waiting_for_user")
        meta=view["narrative"]["metadata"]["skill"]
        self.assertEqual(meta["included"],["narrative"]); self.assertNotIn("outline",meta["included"]); self.assertEqual(len(meta["hash"]),64)
        with self.assertRaises(ConflictError): self.service.generate_outline("task-1")
        self.service.confirm_narrative("task-1"); view=self.service.generate_outline("task-1")
        self.assertEqual(len(view["outline"]["slide_ids"]),3); self.assertEqual(view["outline"]["metadata"]["skill"]["included"],["outline"])

    def test_direct_edit_is_authoritative_version(self):
        one=self.service.generate_narrative("task-1")["narrative"]
        two=self.service.edit_narrative("task-1",one["markdown"]+"\n人工结论\n")["narrative"]
        self.assertEqual(two["version"],2); self.assertIn("人工结论",two["markdown"]); self.assertTrue(two["metadata"]["authoritative"])
        self.assertEqual(two["metadata"]["parent"],one["hash"])

    def test_manual_direct_edit_cannot_bypass_narrative_confirmation(self):
        first=self.service.generate_narrative("task-1")["narrative"]
        edited=self.service.edit_narrative("task-1",first["markdown"]+"\n人工结论\n")
        self.assertEqual(edited["state"]["stage"],"narrative")
        self.assertEqual(edited["state"]["status"],"waiting_for_user")
        with self.assertRaises(ConflictError): self.service.generate_outline("task-1")

    def test_manual_edit_after_outline_invalidates_confirmation_and_outline(self):
        first=self.service.generate_narrative("task-1")["narrative"]
        self.service.confirm_narrative("task-1")
        self.service.generate_outline("task-1")
        edited=self.service.edit_narrative("task-1",first["markdown"]+"\n确认后修改\n")
        self.assertEqual(edited["state"]["stage"],"narrative")
        self.assertEqual(edited["state"]["status"],"waiting_for_user")
        self.assertIsNone(edited["outline"])
        with self.assertRaises(ConflictError): self.service.generate_outline("task-1")

    def test_outline_scope_resource_and_page_validation(self):
        self.service.generate_narrative("task-1"); self.service.confirm_narrative("task-1"); first=self.service.generate_outline("task-1")["outline"]
        changed=first["markdown"].replace("推进第 2 个叙事节点","只修改第二页")
        second=self.service.edit_outline("task-1",changed)["outline"]
        self.assertEqual(second["metadata"]["affected"],["slide-2"]); self.assertEqual(set(second["metadata"]["unchanged"]),{"slide-1","slide-3"})
        scoped=self.service.generate_outline("task-1","强化证据",["slide-2"])["outline"]
        self.assertEqual(scoped["metadata"]["affected"],["slide-2"]); self.assertIn("强化证据",scoped["markdown"])
        with self.assertRaises(ValidationError): self.service.edit_outline("task-1",changed.replace("resources位","resources位" ).replace("待补资源位","![x](resources://foreign.png)",1))
        with self.assertRaises(ValidationError): self.service.edit_outline("task-1",changed.split("## [slide-3]")[0])

    def test_human_markdown_headings_are_normalized_to_stable_page_ids(self):
        self.service.generate_narrative("task-1"); self.service.confirm_narrative("task-1")
        markdown=("# 逐页大纲\n\n## 开场\n- 建立目标\n\n"
                  "### 第 2 页｜核心方案\n- 展开方案\n\n## 3. 行动建议\n- 明确行动\n")
        outline=self.service.edit_outline("task-1",markdown)["outline"]
        self.assertEqual(outline["slide_ids"],["slide-1","slide-2","slide-3"])
        self.assertIn("## [slide-1] 开场",outline["markdown"])
        self.assertIn("## [slide-2] 核心方案",outline["markdown"])
        self.assertIn("## [slide-3] 行动建议",outline["markdown"])

    def test_non_destructive_rollback(self):
        first=self.service.generate_narrative("task-1")["narrative"]
        self.service.edit_narrative("task-1",first["markdown"]+"\n新版")
        rolled=self.service.rollback_planning("task-1","narrative",first["hash"])["narrative"]
        self.assertEqual(rolled["version"],3); self.assertEqual(rolled["markdown"],first["markdown"]); self.assertEqual(len(self.service.versions("task-1","narrative")),3)

    def test_narrative_rollback_resets_manual_confirmation(self):
        first=self.service.generate_narrative("task-1")["narrative"]
        self.service.confirm_narrative("task-1"); self.service.generate_outline("task-1")
        rolled=self.service.rollback_planning("task-1","narrative",first["hash"])
        self.assertEqual(rolled["state"]["stage"],"narrative"); self.assertIsNone(rolled["outline"])
        with self.assertRaises(ConflictError): self.service.generate_outline("task-1")

    def test_auto_advances_without_hiding_versions(self):
        self.service.create("auto",mode="auto"); self.service.import_input("auto",{"goal":"发布","audience":"客户","topic":"方案","页数":2})
        view=self.service.planning_view("auto")
        self.assertEqual(view["state"]["stage"],"sample"); self.assertEqual(view["state"]["status"],"ready"); self.assertIsNotNone(view["narrative"]); self.assertIsNotNone(view["outline"])

    def test_quick_mode_requires_and_enforces_final_slide_count(self):
        with self.assertRaisesRegex(ValidationError,"必须明确"):
            self.service.create("quick-missing",mode="quick")
        self.service.create("quick",mode="quick",target_slide_count=2)
        self.service.import_input("quick",{"goal":"发布","audience":"客户","topic":"方案"})
        view=self.service.planning_view("quick")
        self.assertEqual(view["state"]["mode"],"quick")
        self.assertEqual(view["state"]["target_slide_count"],2)
        self.assertEqual(view["outline"]["slide_ids"],["slide-1","slide-2"])
        self.assertEqual(view["state"]["stage"],"sample")
        self.assertIsNotNone(view["narrative"])
        self.assertIsNotNone(self.service.sample_view("quick")["sample"])

        self.service.create("quick-conflict",mode="quick",target_slide_count=2)
        with self.assertRaisesRegex(ValidationError,"不一致"):
            self.service.import_input("quick-conflict",{"goal":"发布","audience":"客户","topic":"方案","页数":3})

    def request(self,path):
        status=[]
        body=b"".join(App(self.service)({"REQUEST_METHOD":"GET","PATH_INFO":path,"CONTENT_LENGTH":"0","wsgi.input":io.BytesIO()},lambda s,h:status.append(s)))
        return status[0],body.decode()
    def test_workspace_has_dual_editors_no_preview(self):
        status,page=self.request("/tasks/task-1/outline")
        self.assertTrue(status.startswith("200")); self.assertIn('type="module"',page)
        module=Path("frontend/static/js/stages/planning.js").read_text()
        self.assertIn("Markdown 内容",module); self.assertIn("非破坏回退",Path("frontend/static/js/components/index.js").read_text())
        self.assertNotIn("previewFrame",module)


if __name__ == "__main__": unittest.main()
