"""P4 样品页真实浏览器交互测试（Playwright + headless Chromium）。

与 WSGI 级测试不同，本模块把样品页加载进真实浏览器并执行页面 JavaScript，
覆盖第二轮独立复验要求的四条交互：
1. 默认自动识别提交不带 scope（页面 JS 真实组包）；
2. 理解依据在刷新后仍展示；
3. 歧义提示留在当前页且版本不增长；
4. “确认样品并生成全稿”按钮实际触发确认门禁。

运行前提（标准库套件不需要该依赖；缺失时本模块整体跳过，不影响
`python3 -m unittest discover -s tests`）：

    pip install playwright
    python3 -m playwright install chromium
"""
import json
import tempfile
import threading
import unittest
from wsgiref.simple_server import WSGIRequestHandler, make_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # 标准库环境无 playwright：整模块跳过
    sync_playwright = None

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class SamplePageBrowserTests(unittest.TestCase):
    """在真实 Chromium 中点击样品页控件，验证页面 JS 的真实行为。"""

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright 未安装：pip install playwright && python3 -m playwright install chromium")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:
            cls.pw.stop()
            raise unittest.SkipTest(f"chromium 不可用：{exc}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "pw", None):
            cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = TaskService(WorkspaceStore(self.tmp.name))
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task")
        self.svc.confirm_narrative("task")
        self.svc.generate_outline("task")
        self.svc.confirm_outline("task")
        self.server = make_server("127.0.0.1", 0, App(self.svc), handler_class=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.page = self.__class__.browser.new_page()
        self.posts = []
        self.page.on("request", lambda req: self.posts.append(req) if req.method == "POST" else None)

    def tearDown(self):
        self.page.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def open_samples(self):
        self.page.goto(f"{self.base}/tasks/task/samples")

    def click_and_wait_reload(self, selector):
        with self.page.expect_navigation():
            self.page.click(selector)

    def generate_via_ui(self):
        """通过页面按钮真实生成样品（隐式绑定下 #generate 读不到 Prompt）。"""
        self.open_samples()
        self.page.fill("#prompt", "首屏主视觉")
        self.click_and_wait_reload("#generate")
        view = self.svc.sample_view("task")
        self.assertTrue(view["sample"], "点击生成后应产生样品版本")
        return view["selection"]["slide_ids"][0]

    def last_modify_request(self):
        posts = [r for r in self.posts if r.url.endswith("/samples/modify")]
        self.assertTrue(posts, "应捕获到页面发起的 modify 请求")
        return json.loads(posts[-1].post_data)

    def test_auto_scope_submit_omits_scope_and_uses_prompt_semantics(self):
        sid = self.generate_via_ui()
        # 默认“自动识别”：填写 Prompt 与页面 ID，不改动作用域下拉
        self.page.fill("#slide", sid)
        self.page.fill("#prompt", "当前页标题更醒目")
        self.click_and_wait_reload("#modify")
        # ①页面 JS 真实组包：auto 路径提交体不带 scope
        body = self.last_modify_request()
        self.assertNotIn("scope", body)
        self.assertEqual(body["prompt"], "当前页标题更醒目")
        self.assertEqual(body["slide_id"], sid)
        # 后端按 Prompt 语义推断为 page 并回写理解依据
        understanding = self.svc.sample_view("task")["sample"]["metadata"]["scope_understanding"]
        self.assertEqual(understanding["scope"], "page")
        self.assertEqual(understanding["basis"], "prompt_semantics")

    def test_understanding_panel_visible_after_reload(self):
        sid = self.generate_via_ui()
        self.page.fill("#slide", sid)
        self.page.fill("#prompt", "当前页标题更醒目")
        self.click_and_wait_reload("#modify")
        # ②提交后整页刷新，理解面板由服务端渲染并立即可见
        panel = self.page.text_content(".understanding")
        self.assertIn("作用域：页面", panel)
        self.assertIn("依据：Prompt 语义", panel)
        self.assertIn(f"目标：页面 {sid}", panel)
        # 再次整页刷新（重新 GET）后面板仍然展示
        self.page.reload()
        panel = self.page.text_content(".understanding")
        self.assertIn("依据：Prompt 语义", panel)
        timeline = self.page.text_content(".timeline")
        self.assertIn("依据：Prompt 语义", timeline)

    def test_ambiguity_hint_stays_on_page_and_version_unchanged(self):
        sid = self.generate_via_ui()
        before = len(self.svc.sample_view("task")["versions"])
        self.page.fill("#slide", sid)
        self.page.fill("#prompt", "统一所有页，但只改当前页")
        self.page.click("#modify")
        # ③歧义澄清：不刷新页面，提示写入 modifyHint（用选择器等待而非 evaluate，避免页面 CSP 禁止 eval）
        self.page.wait_for_selector("#modifyHint:not(:empty)")
        hint = self.page.text_content("#modifyHint")
        self.assertIn("歧义", hint)
        self.assertIn("请明确是全局、页面还是元素", hint)
        # 仍停留在当前编辑现场：输入未被清空（若整页刷新 textarea 会被重置）
        self.assertEqual(self.page.input_value("#prompt"), "统一所有页，但只改当前页")
        self.assertIn("正在预览：当前版本 v1", self.page.text_content("#previewLabel"))
        # 失败修改不产生新版本
        self.assertEqual(len(self.svc.sample_view("task")["versions"]), before)

    def test_confirm_button_triggers_gate_and_modify_rearms_it(self):
        self.generate_via_ui()
        # ④确认按钮真实触发门禁（隐式绑定下 confirm 解析为 window.confirm，不会发起请求）
        self.click_and_wait_reload("#confirm")
        self.assertIn("已绑定确认", self.page.text_content("body"))
        self.assertTrue(self.svc.sample_view("task")["confirmation"], "确认事实应已绑定当前样品")
        confirms = [r for r in self.posts if r.url.endswith("/samples/confirm")]
        self.assertTrue(confirms, "点击确认应真实发起 confirm 请求")
        # 确认后再修改：门禁重新挂起，页面回到待确认
        self.page.select_option("#scope", "global")
        self.page.fill("#prompt", "统一加深背景")
        self.click_and_wait_reload("#modify")
        self.assertIn("待人工确认", self.page.text_content("body"))
        self.assertFalse(self.svc.sample_view("task")["confirmation"])


if __name__ == "__main__":
    unittest.main()
