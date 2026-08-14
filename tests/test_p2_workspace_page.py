"""P2 task/input regressions for the unified FastAPI application shell."""

import io
import json
from pathlib import Path
import tempfile
import unittest

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
ROOT = Path(__file__).resolve().parents[1]


class WorkspacePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.tmp.name)
        self.svc = TaskService(self.store)
        self.svc.create("task")
        self.app = App(self.svc)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        status = []
        result = b"".join(self.app({
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }, lambda value, _headers: status.append(value)))
        return int(status[0].split()[0]), json.loads(result) if result.startswith((b"{", b"[")) else result.decode()

    def import_card(self, source, fmt="json"):
        status, result = self.call("POST", "/v1/tasks/task/input", {"source": source, "source_format": fmt})
        self.assertEqual(status, 200)
        return result

    def test_empty_state_and_preconditions_come_from_shell_and_stage_module(self):
        status, shell = self.call("GET", "/v1/tasks/task/shell")
        self.assertEqual(status, 200)
        self.assertEqual(len(shell["stages"]), 8)
        self.assertIn("前置条件", shell["stages"][1]["lock_reason"])
        module = (ROOT / "frontend/static/js/stages/input.js").read_text()
        self.assertIn("尚未导入任务卡", module)
        self.assertIn("请先导入任务卡", module)

    def test_resources_defaults_and_blockers_are_returned_by_authoritative_api(self):
        self.store.put_resource("task", "hero.png", PNG)
        self.store.put_resource("task", "hero.md", "主视觉说明".encode())
        self.store.put_resource("task", "broken.png", b"not-an-image")
        view = self.import_card({"goal": "销售汇报"})
        self.assertEqual(view["state"]["status"], "waiting_for_user")
        self.assertEqual(view["task_card"]["defaults"]["language"], "zh-CN")
        self.assertEqual(view["task_card"]["defaults"]["aspect_ratio"], "16:9")
        self.assertEqual(view["task_card"]["missing"], ["audience", "topic"])
        self.assertEqual(view["manifest"]["resources"][0]["uri"], "resources://hero.png")
        self.assertEqual(view["manifest"]["resources"][0]["description"], "主视觉说明")
        self.assertIn("invalid_image_content", [item["code"] for item in view["manifest"]["warnings"]])

    def test_accessible_dynamic_controls_are_external_module_code(self):
        index = (ROOT / "frontend/index.html").read_text()
        module = (ROOT / "frontend/static/js/stages/input.js").read_text()
        components = (ROOT / "frontend/static/js/components/index.js").read_text()
        self.assertIn('class="skip-link"', index)
        self.assertIn('aria-live="polite"', index)
        self.assertIn('type="module"', index)
        self.assertIn('element("form"', module)
        self.assertIn('element("fieldset"', module)
        self.assertIn("提交答案并继续", module)
        self.assertIn('element("label"', components)

    def test_full_answer_flow_uses_json_api_and_invalidates_changed_answers(self):
        view = self.import_card({"goal": "新品介绍"})
        questions = view["clarification"]["details"]
        first, second = questions
        status, partial = self.call("POST", f'/v1/tasks/task/clarifications/{first["question_id"]}/answer', {"option": "稍后补充"})
        self.assertEqual(status, 200)
        self.assertFalse(partial["confirmed"])
        status, done = self.call("POST", f'/v1/tasks/task/clarifications/{second["question_id"]}/answer', {"option": "Other", "other": "管理层"})
        self.assertEqual(status, 200)
        self.assertTrue(done["confirmed"])
        status, changed = self.call("POST", f'/v1/tasks/task/clarifications/{second["question_id"]}/answer', {"option": "Other", "other": "经销商"})
        self.assertEqual(status, 200)
        self.assertIn("outline", changed["invalidated"])

    def test_other_without_text_rejected(self):
        view = self.import_card({"goal": "g"})
        question_id = view["clarification"]["details"][0]["question_id"]
        status, result = self.call("POST", f"/v1/tasks/task/clarifications/{question_id}/answer", {"option": "Other"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_markdown_card_closes_input_stage_without_server_rendered_page(self):
        self.store.put_resource("task", "hero.png", PNG)
        self.store.put_resource("task", "hero.md", "主视觉".encode())
        view = self.import_card("演示目标：销售\n受众：客户\n核心主题：新品", "markdown")
        self.assertEqual(view["task_card"]["goal"], "销售")
        self.assertEqual(view["task_card"]["missing"], [])
        self.assertEqual(view["manifest"]["resources"][0]["uri"], "resources://hero.png")
        status, page = self.call("GET", "/tasks/task")
        self.assertEqual(status, 200)
        self.assertIn('type="module"', page)
        self.assertNotIn("销售", page)


if __name__ == "__main__":
    unittest.main()
