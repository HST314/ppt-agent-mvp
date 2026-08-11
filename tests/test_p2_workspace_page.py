"""P2-05 任务/资料页验收：资源/默认值/阻断展示与完整答题交互 E2E。

E2E 通过 WSGI 应用驱动，模拟页面内 JS 的真实调用序列：
GET 页面解析问题与选项 -> POST 回答 API -> GET 页面确认状态回显。
"""
import io, json, re, tempfile, unittest

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"


class WorkspacePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.tmp.name)
        self.svc = TaskService(self.store)
        self.svc.create("task")
        self.app = App(self.svc)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        status = []
        out = b"".join(self.app({"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}, lambda s, h: status.append(s)))
        return status[0], out

    def page(self):
        status, raw = self.call("GET", "/tasks/task")
        self.assertTrue(status.startswith("200"))
        return raw.decode()

    def import_card(self, source, fmt="json"):
        status, _ = self.call("POST", "/v1/tasks/task/input", {"source": source, "source_format": fmt})
        self.assertTrue(status.startswith("200"))

    def answer(self, qid, body):
        return self.call("POST", f"/v1/tasks/task/clarifications/{qid}/answer", body)

    def test_empty_state_shows_preconditions(self):
        page = self.page()
        self.assertIn("尚未导入任务卡", page)
        self.assertIn("尚未导入", page)  # 输入冻结状态
        self.assertIn("请先导入任务卡", page)  # 主操作
        self.assertIn("未到达", page)  # 后续阶段只展示前置条件
        self.assertIn("前置条件：完成任务创建与资料导入", page)

    def test_page_shows_resources_defaults_and_blockers(self):
        self.store.put_resource("task", "hero.png", PNG)
        self.store.put_resource("task", "hero.md", "主视觉说明".encode())
        self.store.put_resource("task", "broken.png", b"not-an-image")
        self.import_card({"goal": "销售汇报"})  # 缺受众、核心主题 -> 阻断
        page = self.page()
        # 资源清单与诊断
        self.assertIn("resources://hero.png", page)
        self.assertIn("主视觉说明", page)
        self.assertIn("图片内容无效或已损坏", page)
        self.assertIn("broken.png", page)
        # 可见默认值
        self.assertIn("zh-CN", page)
        self.assertIn("16:9", page)
        # 阻断缺失项与运行状态
        self.assertIn("阻断", page)
        self.assertIn("受众", page)
        self.assertIn("核心主题", page)
        self.assertIn("等待人工", page)
        self.assertIn("缺少必填信息", page)
        self.assertIn("回答澄清问题", page)
        self.assertIn('href="#clarification"', page)  # 直达入口

    def test_keyboard_friendly_markup(self):
        self.import_card({"goal": "g"})
        page = self.page()
        for needle in ("<fieldset", "<legend", 'type="radio"', 'type="submit"', "<label", 'aria-label="任务卡"', 'aria-live="polite"', 'aria-current="step"'):
            self.assertIn(needle, page)

    def test_full_answer_flow_e2e(self):
        self.import_card({"goal": "新品介绍"})  # 触发 missing-audience / missing-topic
        page = self.page()
        qids = re.findall(r'data-qid="([^"]+)"', page)
        self.assertEqual(len(qids), 2)
        # 每题都有可提交的选项与 Other 输入
        for qid in qids:
            self.assertIn(f'data-qid="{qid}"', page)
        self.assertEqual(page.count('value="Other"'), 2)
        self.assertIn("提交回答", page)
        # 选项回答
        status, raw = self.answer(qids[0], {"option": "稍后补充"})
        self.assertTrue(status.startswith("200"))
        self.assertFalse(json.loads(raw)["confirmed"])
        # Other 回答
        status, raw = self.answer(qids[1], {"option": "Other", "other": "管理层"})
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(raw)["confirmed"])
        # 状态回写：任务恢复 ready，页面回显回答与确认态
        _, view = self.call("GET", "/v1/tasks/task/input")
        view = json.loads(view)
        self.assertEqual(view["state"]["status"], "ready")
        self.assertEqual(view["clarification"]["answers"], {qids[0]: "稍后补充", qids[1]: "管理层"})
        page = self.page()
        self.assertIn("澄清已确认", page)
        self.assertIn("管理层", page)
        self.assertIn("修改回答", page)
        self.assertIn("资料已可用于下一阶段", page)
        # 改答产生新回答并标记下游失效
        status, raw = self.answer(qids[1], {"option": "Other", "other": "经销商"})
        self.assertTrue(status.startswith("200"))
        self.assertIn("outline", json.loads(raw)["invalidated"])
        page = self.page()
        self.assertIn("经销商", page)

    def test_other_without_text_rejected(self):
        self.import_card({"goal": "g"})
        qids = re.findall(r'data-qid="([^"]+)"', self.page())
        status, raw = self.answer(qids[0], {"option": "Other"})
        self.assertTrue(status.startswith("400"))
        self.assertEqual(json.loads(raw)["error"]["code"], "validation_error")

    def test_markdown_card_same_page_closure(self):
        self.store.put_resource("task", "hero.png", PNG)
        self.store.put_resource("task", "hero.md", "主视觉".encode())
        self.import_card("演示目标：销售\n受众：客户\n核心主题：新品", "markdown")
        page = self.page()
        self.assertIn("销售", page)
        self.assertIn("resources://hero.png", page)
        self.assertIn("无缺失项", page)
        self.assertIn("资料已可用于下一阶段", page)


if __name__ == "__main__":
    unittest.main()
